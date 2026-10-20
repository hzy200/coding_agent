"""planner 与 respond 节点：真正调用模型的那部分。

这两个节点的正文此前没被覆盖 —— act 的测试用 EchoLLM 绕过了结构化输出，
而 planner 的**规划失败降级**路径（模型反复给不出合法 JSON）尤其需要守住：
它必须退化成「整个请求当一步」，而不是让整张图崩掉。

降级还必须**可见**：早先的实现只降级不标记，于是多步工作流悄悄退化成单步，
而 `PlanCreated` 看上去完全正常 —— 只有翻审计才发现每次的计划都是请求原文。
下面的 `plan_degraded` 用例守的就是这一点。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent.graph.nodes.act import make_act_node
from coding_agent.graph.nodes.planner import TaskPlan, make_planner_node
from coding_agent.graph.nodes.respond import (
    BUDGET_EXHAUSTED_NOTE,
    NO_CHANGES_NOTE,
    REPAIRS_EXHAUSTED,
    VERIFICATION_FAILED_NOTE,
    make_respond_node,
)
from coding_agent.graph.plan import PlanStepModel
from coding_agent.llm.context import ContextBudget
from coding_agent.llm.prompts import (
    DELEGATION_NOTE,
    DELEGATION_NOTE_DISABLED,
    DELEGATION_NOTE_NOT_EXPLORE,
)


class _FakeStructured:
    """模拟 with_structured_output(...).with_retry(...) 这条链。"""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[Any] = []

    def with_retry(self, **kwargs: Any) -> _FakeStructured:
        self.retry_kwargs = kwargs
        return self

    def invoke(self, messages: Any, config: Any = None) -> Any:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.result

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        return self.invoke(messages, config)


class _FakeStructuredLLM:
    def __init__(self, structured: _FakeStructured) -> None:
        self._structured = structured

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> _FakeStructured:
        # **_kwargs 吃掉 method=... ：planner 走的是 with_json_output()
        self.schema = schema
        return self._structured


class _FakeLLM:
    def __init__(self, reply: str = "答复") -> None:
        self.reply = reply
        self.seen: list[Any] = []

    def invoke(self, messages: Any, config: Any = None) -> AIMessage:
        self.seen = messages
        return AIMessage(content=self.reply)

    async def ainvoke(self, messages: Any, config: Any = None) -> AIMessage:
        return self.invoke(messages, config)


class _AsyncOnlyLLM:
    """只实现 `ainvoke`：一旦节点退回同步调用，这里立刻炸。"""

    def __init__(self, reply: Any = None) -> None:
        self.reply = reply if reply is not None else AIMessage(content="答复")
        self.calls = 0

    def invoke(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("不该走同步 invoke —— 那样 Ctrl-C 取消不了")

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> _AsyncOnlyLLM:
        self.schema = schema
        return self

    def with_retry(self, **_kwargs: Any) -> _AsyncOnlyLLM:
        return self

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        self.calls += 1
        return self.reply


def _sync(node):  # noqa: ANN001
    """planner / respond 是 async 节点（走 ainvoke 才能取消），单测里同步驱动。"""
    def wrapper(state, config=None):
        return asyncio.run(node(state, config))
    return wrapper


# ---------------- act 的委派提示 ----------------

def _delegate_state(**extra) -> dict:
    return {
        "messages": [HumanMessage(content="探索一下")],
        "plan": ["探索", "实现"],
        "plan_kinds": ["explore", "implement"],
        "step_idx": 0,
        "delegation_allowed": True,
        "delegation_budget": 2,
        **extra,
    }


def test_act_injects_the_delegation_budget() -> None:
    llm = _FakeLLM()
    _sync(make_act_node(llm, max_tool_rounds=5, delegate_enabled=True))(
        _delegate_state(), None
    )
    assert DELEGATION_NOTE.format(max_calls=2) in llm.seen[0].content


def test_act_disables_delegation_for_small_repos() -> None:
    llm = _FakeLLM()
    _sync(make_act_node(llm, max_tool_rounds=5, delegate_enabled=True))(
        _delegate_state(delegation_allowed=False), None
    )
    assert DELEGATION_NOTE_DISABLED in llm.seen[0].content


def test_act_disables_delegation_outside_an_explore_step() -> None:
    llm = _FakeLLM()
    _sync(make_act_node(llm, max_tool_rounds=5, delegate_enabled=True))(
        _delegate_state(plan_kinds=["implement", "verify"]), None
    )
    assert DELEGATION_NOTE_NOT_EXPLORE in llm.seen[0].content


def test_act_tells_implement_steps_to_read_then_edit() -> None:
    """上下文被委派隔离后，主 agent 需要先读目标文件才能精确编辑。"""
    llm = _FakeLLM()
    _sync(make_act_node(llm, max_tool_rounds=5))(
        _delegate_state(step_idx=1, plan_kinds=["explore", "implement"]), None
    )
    system = llm.seen[0].content
    assert "先对本步骤涉及的目标文件各做一次 file_read" in system
    assert "不要再做大范围探索" in system
    assert "只给结论都不算完成" in system


class _CountingLLM(_FakeLLM):
    def __init__(self, reply: str = "答复", *, raises: bool = False) -> None:
        super().__init__(reply)
        self.calls = 0
        self.raises = raises

    async def ainvoke(self, messages: Any, config: Any = None) -> AIMessage:
        self.calls += 1
        if self.raises:
            raise RuntimeError("forced tool_choice unsupported")
        return await super().ainvoke(messages, config)


def test_nudged_implement_step_forces_one_tool_call() -> None:
    normal = _CountingLLM()
    forced = _CountingLLM()
    node = make_act_node(normal, max_tool_rounds=5, forced_llm=forced)
    _sync(node)(
        _delegate_state(
            step_idx=1,
            plan_kinds=["explore", "implement"],
            empty_step_nudged=True,
            dirty=False,
            tool_rounds=0,
        ),
        None,
    )
    assert forced.calls == 1
    assert normal.calls == 0


def test_forced_tool_choice_is_not_repeated_after_the_first_round() -> None:
    normal = _CountingLLM()
    forced = _CountingLLM()
    node = make_act_node(normal, max_tool_rounds=5, forced_llm=forced)
    _sync(node)(
        _delegate_state(
            step_idx=1,
            plan_kinds=["explore", "implement"],
            empty_step_nudged=True,
            dirty=False,
            tool_rounds=1,
        ),
        None,
    )
    assert normal.calls == 1
    assert forced.calls == 0


def test_forced_tool_choice_falls_back_when_the_provider_rejects_it() -> None:
    normal = _CountingLLM()
    forced = _CountingLLM(raises=True)
    node = make_act_node(normal, max_tool_rounds=5, forced_llm=forced)
    _sync(node)(
        _delegate_state(
            step_idx=1,
            plan_kinds=["explore", "implement"],
            empty_step_nudged=True,
            dirty=False,
            tool_rounds=0,
        ),
        None,
    )
    assert forced.calls == 1
    assert normal.calls == 1


def test_implement_nudge_asks_for_a_read_then_edit() -> None:
    llm = _FakeLLM()
    _sync(make_act_node(llm, max_tool_rounds=5))(
        _delegate_state(
            step_idx=1,
            plan_kinds=["explore", "implement"],
            empty_step_nudged=True,
            dirty=False,
        ),
        None,
    )
    assert "先 file_read **本步骤要改的目标文件**" in llm.seen[0].content


# ---------------- planner ----------------

def _plan_state(prompt: str = "把计算器加个除法") -> dict:
    return {"messages": [HumanMessage(content=prompt)]}


def test_planner_uses_model_steps() -> None:
    structured = _FakeStructured(TaskPlan(steps=["读代码", "加函数", "跑测试"]))
    node = _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False))

    out = node(_plan_state(), None)
    assert out["plan"] == ["读代码", "加函数", "跑测试"]
    assert out["step_idx"] == 0
    assert out["retry"] == 0
    assert out["dirty"] is False


def test_planner_injects_the_worker_context() -> None:
    structured = _FakeStructured(TaskPlan(steps=["实现"]))
    node = _sync(
        make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False)
    )
    node(
        {
            "messages": [HumanMessage(content="改功能")],
            "worker_context": "[编排器汇总]\n入口在 main.py",
        },
        None,
    )
    request = structured.calls[-1][-1].content
    assert "入口在 main.py" in request


def test_act_injects_the_worker_context() -> None:
    llm = _FakeLLM()
    _sync(make_act_node(llm, max_tool_rounds=5))(
        {
            "messages": [HumanMessage(content="实现")],
            "worker_context": "[编排器汇总]\n入口在 main.py",
        },
        None,
    )
    assert "入口在 main.py" in llm.seen[0].content


def test_planner_preserves_step_kinds() -> None:
    structured = _FakeStructured(
        TaskPlan(
            steps=[
                PlanStepModel(text="读代码", kind="explore"),
                PlanStepModel(text="加函数", kind="implement"),
                PlanStepModel(text="跑测试", kind="verify"),
            ]
        )
    )
    out = _sync(
        make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False)
    )(_plan_state(), None)

    assert out["plan"] == ["读代码", "加函数", "跑测试"]
    assert out["plan_kinds"] == ["explore", "implement", "verify"]


def test_planner_asks_with_pydantic_schema() -> None:
    structured = _FakeStructured(TaskPlan(steps=["一步"]))
    llm = _FakeStructuredLLM(structured)
    _sync(make_planner_node(llm, max_steps=5, allow_write=False))(_plan_state(), None)
    assert llm.schema is TaskPlan


def test_planner_retries_structured_output() -> None:
    structured = _FakeStructured(TaskPlan(steps=["一步"]))
    _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False))
    assert structured.retry_kwargs.get("stop_after_attempt") == 3


def test_planner_caps_step_count() -> None:
    structured = _FakeStructured(TaskPlan(steps=[f"步骤{i}" for i in range(20)]))
    node = _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=3, allow_write=False))
    assert len(node(_plan_state(), None)["plan"]) == 3


def test_planner_strips_blank_steps() -> None:
    structured = _FakeStructured(TaskPlan(steps=["  有效  ", "", "   ", "另一个"]))
    node = _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False))
    assert node(_plan_state(), None)["plan"] == ["有效", "另一个"]


def test_planner_falls_back_when_model_returns_nothing() -> None:
    structured = _FakeStructured(TaskPlan(steps=[]))
    node = _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False))
    out = node(_plan_state("把除法加上"), None)
    assert out["plan"] == ["把除法加上"]
    # 降级计划不知道类型：unknown 保留原有的宽松路由
    assert out["plan_kinds"] == ["unknown"]
    # 合法 JSON 但 steps 为空，同样是「没规划出来」
    assert out["plan_degraded"] is True


def test_planner_falls_back_when_structured_output_keeps_failing() -> None:
    """规划失败不能拖垮整张图 —— 降级成「整个请求当一步」。"""
    structured = _FakeStructured(error=ValueError("模型给不出合法 JSON"))
    node = _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False))

    out = node(_plan_state("重构 parser"), None)
    assert out["plan"] == ["重构 parser"]
    assert out["plan_kinds"] == ["unknown"]
    assert out["plan_degraded"] is True


def test_planner_does_not_mark_a_real_plan_as_degraded() -> None:
    """正常规划必须写 False —— 否则上一轮的降级标记会串到这一轮。

    只写 True 不写 False 的话，一次降级之后所有后续运行都会被误报成降级。
    """
    structured = _FakeStructured(TaskPlan(steps=["读代码", "加函数"]))
    node = _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False))
    assert node(_plan_state(), None)["plan_degraded"] is False


def test_planner_clears_both_replan_budgets() -> None:
    """两个重规划额度都是任务级的，开局必须一起归零。"""
    structured = _FakeStructured(TaskPlan(steps=["一步"]))
    node = _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False))
    out = node(_plan_state(), None)
    assert out["replan_count"] == 0
    assert out["tweak_count"] == 0


def test_planner_fallback_survives_empty_request() -> None:
    structured = _FakeStructured(error=RuntimeError("boom"))
    node = _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False))
    out = node({"messages": []}, None)
    assert out["plan"] == ["完成用户请求"]


def test_planner_prompt_mentions_write_permission() -> None:
    structured = _FakeStructured(TaskPlan(steps=["一步"]))
    node = _sync(make_planner_node(
        _FakeStructuredLLM(structured), max_steps=5, allow_write=True
    ))
    node(_plan_state(), None)
    assert "允许修改文件" in structured.calls[0][0].content


def test_planner_prompt_states_readonly_restriction() -> None:
    structured = _FakeStructured(TaskPlan(steps=["一步"]))
    node = _sync(make_planner_node(
        _FakeStructuredLLM(structured), max_steps=5, allow_write=False
    ))
    node(_plan_state(), None)
    assert "只读权限" in structured.calls[0][0].content


def test_planner_sends_the_latest_human_message() -> None:
    """多轮会话里取最后一条，不能取第一条。"""
    structured = _FakeStructured(TaskPlan(steps=["一步"]))
    node = _sync(make_planner_node(_FakeStructuredLLM(structured), max_steps=5, allow_write=False))
    node(
        {
            "messages": [
                HumanMessage(content="第一轮的问题"),
                AIMessage(content="第一轮的回答"),
                HumanMessage(content="第二轮的问题"),
            ]
        },
        None,
    )
    assert "第二轮的问题" in structured.calls[0][-1].content


# ---------------- respond ----------------

def _respond_state(**extra) -> dict:
    return {"messages": [HumanMessage(content="干活"), AIMessage(content="干完了")], **extra}


def test_respond_goes_through_the_async_llm_api() -> None:
    """respond 是收尾那一次调用，往往最长 —— 必须可取消（走 ainvoke）。"""
    llm = _AsyncOnlyLLM()
    out = _sync(make_respond_node(llm))(_respond_state(), None)

    assert llm.calls == 1
    assert out["messages"][0].content == "答复"


def test_planner_goes_through_the_async_llm_api() -> None:
    """planner 同理：它是每次运行的第一件事，取消不了同样难受。"""
    llm = _AsyncOnlyLLM(TaskPlan(steps=["一步"]))
    out = _sync(make_planner_node(llm, max_steps=5, allow_write=False))(_plan_state(), None)

    assert llm.calls == 1
    assert out["plan"] == ["一步"]


def test_respond_returns_model_reply() -> None:
    llm = _FakeLLM("最终答复")
    out = _sync(make_respond_node(llm))(_respond_state(), None)
    assert out["messages"][0].content == "最终答复"


def test_respond_appends_the_instruction_last() -> None:
    llm = _FakeLLM()
    _sync(make_respond_node(llm))(_respond_state(), None)
    assert llm.seen[-1].content == "请给出最终答复。"


def test_respond_without_verification_has_plain_prompt() -> None:
    llm = _FakeLLM()
    _sync(make_respond_node(llm))(_respond_state(), None)
    assert VERIFICATION_FAILED_NOTE.split("{")[0].strip() not in llm.seen[0].content


def test_respond_reports_verification_failure() -> None:
    llm = _FakeLLM()
    _sync(make_respond_node(llm))(
        _respond_state(verification={
            "status": "failed", "command": "pytest -q",
            "issues": [{"location": "a.py:3", "message": "assert 1 == 2"}],
        }),
        None,
    )
    system = llm.seen[0].content
    assert "pytest -q" in system
    assert "a.py:3 assert 1 == 2" in system
    assert "不要声称已完成" in system


def test_respond_handles_failure_without_structured_issues() -> None:
    llm = _FakeLLM()
    _sync(make_respond_node(llm))(_respond_state(verification={"status": "failed"}), None)
    assert "无结构化信息" in llm.seen[0].content


def test_respond_does_not_flag_successful_verification() -> None:
    llm = _FakeLLM()
    _sync(make_respond_node(llm))(_respond_state(verification={"status": "ok"}), None)
    assert "不要声称已完成" not in llm.seen[0].content


def test_respond_escalates_when_repairs_are_exhausted() -> None:
    llm = _FakeLLM()
    _sync(make_respond_node(llm, max_repair_rounds=2))(
        _respond_state(verification={"status": "failed"}, retry=2), None
    )
    system = llm.seen[0].content
    assert REPAIRS_EXHAUSTED.split("{")[0].strip() in system
    assert "停止再试" in system


def test_respond_does_not_escalate_below_the_limit() -> None:
    llm = _FakeLLM()
    _sync(make_respond_node(llm, max_repair_rounds=3))(
        _respond_state(verification={"status": "failed"}, retry=1), None
    )
    assert "停止再试" not in llm.seen[0].content


def test_respond_caps_the_issue_list() -> None:
    llm = _FakeLLM()
    issues = [{"location": f"f{i}.py:1", "message": f"boom{i}"} for i in range(30)]
    _sync(make_respond_node(llm))(
        _respond_state(verification={"status": "failed", "issues": issues}), None
    )
    system = llm.seen[0].content
    assert "f9.py:1" in system
    assert "f10.py:1" not in system


def test_respond_flags_budget_exhaustion_without_changes() -> None:
    llm = _FakeLLM()
    _sync(make_respond_node(llm))(_respond_state(budget_exhausted=True, dirty=False), None)
    assert BUDGET_EXHAUSTED_NOTE in llm.seen[0].content


def test_respond_does_not_flag_budget_exhaustion_when_changes_were_made() -> None:
    """有改动时预算标记不代表没做成，不该制造噪音。"""
    llm = _FakeLLM()
    _sync(make_respond_node(llm))(_respond_state(budget_exhausted=True, dirty=True), None)
    assert BUDGET_EXHAUSTED_NOTE not in llm.seen[0].content


def test_respond_uses_the_run_level_verification_after_a_replan() -> None:
    """replan 清空 verification 后，收尾仍要看到最近一次真实失败。"""
    llm = _FakeLLM("任务已完成")
    out = _sync(make_respond_node(llm))(
        _respond_state(
            verification={},
            last_verification={
                "status": "failed",
                "command": "pytest -q",
                "summary": "3 failed",
            },
        ),
        None,
    )

    assert "3 failed" in llm.seen[0].content
    answer = out["messages"][0].content
    assert "系统提示" in answer
    assert "不能视为任务已完成" in answer


def test_respond_does_not_append_a_warning_when_the_last_verification_passed() -> None:
    llm = _FakeLLM("完成")
    out = _sync(make_respond_node(llm))(
        _respond_state(last_verification={"status": "ok", "summary": "5 passed"}),
        None,
    )
    assert "系统提示" not in out["messages"][0].content


def test_respond_warns_when_a_multistep_run_made_no_changes() -> None:
    """委派探索把读做完了、主 agent 一步没写，收尾不能让它编造完成。"""
    llm = _FakeLLM()
    _sync(make_respond_node(llm))(
        _respond_state(plan=["探索", "实现"], run_dirty=False), None
    )
    assert NO_CHANGES_NOTE in llm.seen[0].content


def test_respond_does_not_warn_no_changes_when_something_was_written() -> None:
    llm = _FakeLLM()
    _sync(make_respond_node(llm))(
        _respond_state(plan=["探索", "实现"], run_dirty=True), None
    )
    assert NO_CHANGES_NOTE not in llm.seen[0].content


@pytest.mark.parametrize("retry", [0, 1, 3])
def test_respond_never_loses_the_failure_note(retry: int) -> None:
    """无论重试到第几次，验证失败这件事都必须出现在提示里。"""
    llm = _FakeLLM()
    _sync(make_respond_node(llm, max_repair_rounds=3))(
        _respond_state(verification={"status": "failed"}, retry=retry), None
    )
    assert "失败了" in llm.seen[0].content


# ---------------- context-length 400 降档 ----------------


class _ContextBoomLLM:
    """第一次调用返回 context-length 400，之后正常。"""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def ainvoke(self, messages: Any, config: Any = None) -> AIMessage:
        self.seen.append(messages)
        if len(self.seen) == 1:
            raise RuntimeError("Error code: 400 - context_length_exceeded")
        return AIMessage(content="ok")


def _big_history() -> list[Any]:
    return [
        HumanMessage(content="干活"),
        AIMessage(
            content="",
            tool_calls=[{"name": "file_read", "args": {"path": "a.py"}, "id": "c1"}],
        ),
        ToolMessage(content="x" * 5_000, tool_call_id="c1", name="file_read"),
    ]


def _seen_chars(messages: list[Any]) -> int:
    return sum(len(str(getattr(m, "content", ""))) for m in messages)


def test_act_retries_with_tighter_budget_on_context_400() -> None:
    llm = _ContextBoomLLM()
    budget = ContextBudget(max_chars=6_000, keep_recent=12, tool_chars=100)
    out = _sync(make_act_node(llm, max_tool_rounds=5, budget=budget))(
        {
            "messages": _big_history(),
            "plan": ["干活"],
            "plan_kinds": ["implement"],
            "step_idx": 0,
            "tool_rounds": 0,
        },
        None,
    )

    assert out["messages"][0].content == "ok"
    assert len(llm.seen) == 2
    # 第二次请求必须比第一次更小 —— 否则「降档」只是重发同一个超长请求
    assert _seen_chars(llm.seen[1]) < _seen_chars(llm.seen[0])
    assert out["context_trim"]["after_chars"] <= 600


def test_respond_retries_with_tighter_budget_on_context_400() -> None:
    llm = _ContextBoomLLM()
    budget = ContextBudget(max_chars=6_000, keep_recent=12, tool_chars=100)
    out = _sync(make_respond_node(llm, budget=budget))(
        {"messages": _big_history(), "plan": ["干活"]}, None
    )

    assert out["messages"][0].content == "ok"
    assert len(llm.seen) == 2
    assert _seen_chars(llm.seen[1]) < _seen_chars(llm.seen[0])
    assert out["context_trim"]["after_chars"] <= 600
