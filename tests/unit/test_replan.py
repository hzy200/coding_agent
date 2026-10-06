"""replan 节点：执行中重建剩余计划。

`planner` 只在开局跑一次，之后靠这个节点修正。它有三条约束必须守住：
**只改剩下的**（已完成的是既成事实）、**有次数上限**（边做边改没有收敛点）、
**失败就沿用原计划**（计划不完美也比任务中断强）。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from coding_agent.graph.nodes import advance
from coding_agent.graph.nodes.replan import (
    ReplanDecision,
    blocking_failure,
    make_replan_node,
)
from coding_agent.graph.routing import (
    ACT,
    REPAIR,
    REPLAN,
    RESPOND,
    make_route_after_verify,
    route_after_replan,
)
from coding_agent.graph.state import AgentState

# 与 `_node()` 的默认 max_replans 保持一致
MAX_REPLANS = 2


class _FakeLLM:
    """只实现 replan 用到的那几个方法。"""

    def __init__(self, decision: ReplanDecision | None = None, *, raises: bool = False) -> None:
        self.decision = decision
        self.raises = raises
        self.calls = 0
        self.seen: list[list] = []

    def with_structured_output(self, _schema, **_kwargs):
        return self

    def with_retry(self, **_kwargs):
        return self

    def invoke(self, messages, config=None):  # noqa: ARG002
        self.calls += 1
        self.seen.append(list(messages))
        if self.raises:
            raise RuntimeError("模型给不出结构化结果")
        return self.decision

    async def ainvoke(self, messages, config=None):  # noqa: ARG002
        return self.invoke(messages, config)


def _state(**overrides):
    state = {
        "messages": [
            HumanMessage(content="把三个文件里的函数改名"),
            AIMessage(content="读完了三个文件，调用点在第 2、3 个文件里"),
        ],
        "plan": ["读代码", "改调用点", "跑测试"],
        "step_idx": 1,
        "replan_count": 0,
        "tweak_count": 0,
    }
    state.update(overrides)
    return state


def test_replan_goes_through_the_async_llm_api() -> None:
    """replan 每次步进都可能问一次模型，同样必须走可取消的 ainvoke。"""
    llm = _AsyncOnlyLLM(ReplanDecision(revise=False))
    node, _ = _node_via(llm)
    assert node(_state(), {}) == {}
    assert llm.calls == 1


def _sync(node):  # noqa: ANN001
    """replan 是 async 节点（走 ainvoke 才能取消），单测里同步驱动。"""
    def wrapper(state, config=None):
        return asyncio.run(node(state, config))
    return wrapper


class _AsyncOnlyLLM:
    """只实现 `ainvoke`：一旦 replan 退回同步调用，这里立刻炸。"""

    def __init__(self, decision: ReplanDecision) -> None:
        self.decision = decision
        self.calls = 0

    def invoke(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("不该走同步 invoke —— 那样 Ctrl-C 取消不了")

    def with_structured_output(self, _schema, **_kwargs):
        return self

    def with_retry(self, **_kwargs):
        return self

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        return self.decision


def _node_via(llm, **kwargs):
    """直接用给定的 llm 构造节点（绕过 _FakeLLM）。"""
    node = _sync(
        make_replan_node(
            llm,
            max_replans=kwargs.pop("max_replans", 2),
            max_plan_steps=kwargs.pop("max_plan_steps", 5),
        )
    )
    return node, llm


def _node(decision=None, **kwargs):
    """replan 是 async 节点（走 ainvoke 才能取消），这里同步驱动。"""
    llm = _FakeLLM(decision, raises=kwargs.pop("raises", False))
    node = _sync(
        make_replan_node(
            llm,
            max_replans=kwargs.pop("max_replans", 2),
            max_plan_steps=kwargs.pop("max_plan_steps", 5),
        )
    )
    return node, llm


# --------------------------------------------------------------------------
# 不改 / 改
# --------------------------------------------------------------------------

def test_no_revision_returns_empty_update() -> None:
    """不改计划时不该产生任何状态更新 —— 否则会误发一个"计划已调整"事件。"""
    node, llm = _node(ReplanDecision(revise=False))
    assert node(_state(), {}) == {}
    assert llm.calls == 1


def test_revision_replaces_only_the_remaining_steps() -> None:
    node, _ = _node(ReplanDecision(revise=True, steps=["改调用点并补一个测试"]))
    out = node(_state(), {})
    # 已完成的第 1 步必须原样保留，否则 step_idx 与计划就错位了
    assert out["plan"] == ["读代码", "改调用点并补一个测试"]
    # 步进入口记在 tweak_count 上，不占用「失败换做法」的额度
    assert out["tweak_count"] == 1


def test_finished_work_clears_the_remaining_steps() -> None:
    """剩余工作其实已经做完 —— 允许把剩余步骤清空（随后直接去收尾）。"""
    node, _ = _node(ReplanDecision(revise=True, steps=[]))
    assert node(_state(), {})["plan"] == ["读代码"]


def test_revision_is_capped_by_the_total_step_budget() -> None:
    node, _ = _node(
        ReplanDecision(revise=True, steps=[f"第 {i} 步" for i in range(10)]),
        max_plan_steps=5,
    )
    out = node(_state(), {})  # step_idx=1 → 剩余预算 4
    assert out["plan"][0] == "读代码"
    assert len(out["plan"]) == 5


def test_blank_steps_are_dropped() -> None:
    node, _ = _node(ReplanDecision(revise=True, steps=["有用的", "   ", ""]))
    assert node(_state(), {})["plan"] == ["读代码", "有用的"]


# --------------------------------------------------------------------------
# 上限与降级
# --------------------------------------------------------------------------

def test_step_cap_stops_asking_the_model() -> None:
    """步进微调到达自己的上限后直接放行 —— 不再多花一次模型调用。"""
    node, llm = _node(ReplanDecision(revise=True, steps=["改了"]), max_replans=2)
    assert node(_state(tweak_count=2), {}) == {}
    assert llm.calls == 0


def test_only_the_last_step_of_the_budget_still_replans() -> None:
    node, llm = _node(ReplanDecision(revise=True, steps=["改了"]), max_replans=2)
    assert node(_state(tweak_count=1), {})["tweak_count"] == 2
    assert llm.calls == 1


# --------------------------------------------------------------------------
# 两个入口的额度互不挤占
# --------------------------------------------------------------------------

def test_step_tweaks_do_not_consume_the_failure_budget() -> None:
    """步进微调用光自己的额度后，失败入口必须还能「换做法」。

    这条守的是踩过的坑：两个入口曾共用 `replan_count`，于是前两步各做一次
    无害的微调就把额度耗光，等到第 3 步真做不成时，失败入口因额度耗尽而
    不再问模型、直接把剩余步骤全砍掉 —— 后面几步与那次失败毫无关系，
    却被一起放弃。
    """
    node, llm = _node(
        ReplanDecision(revise=True, steps=["换成另一种做法", "跑测试"]), max_replans=2
    )
    # tweak_count 已到上限，但失败入口的 replan_count 还是 0
    out = node(_failed_state(tweak_count=2), {})
    assert llm.calls == 1  # 仍然问了模型，而不是直接放弃
    assert out["plan"] == ["读代码", "换成另一种做法", "跑测试"]
    assert out["replan_count"] == 1


def test_failure_revisions_do_not_consume_the_tweak_budget() -> None:
    """反过来也一样：失败换做法不该挤掉步进微调的机会。"""
    node, _ = _node(ReplanDecision(revise=True, steps=["微调一下"]), max_replans=2)
    out = node(_state(replan_count=2), {})
    assert out["tweak_count"] == 1


def test_the_two_counters_are_independent() -> None:
    node, _ = _node(ReplanDecision(revise=True, steps=["改了"]))
    out = node(_failed_state(replan_count=1, tweak_count=1), {})
    assert out["replan_count"] == 2
    assert "tweak_count" not in out  # 失败入口不碰另一个计数器


def test_structured_failure_keeps_the_old_plan() -> None:
    """重规划出错不能拖垮整张图：计划不完美也比任务中断强。"""
    node, llm = _node(raises=True)
    assert node(_state(), {}) == {}
    assert llm.calls == 1


def test_no_remaining_steps_skips_the_model() -> None:
    node, llm = _node(ReplanDecision(revise=True, steps=["改了"]))
    assert node(_state(step_idx=3), {}) == {}
    assert llm.calls == 0


# --------------------------------------------------------------------------
# 提示词内容
# --------------------------------------------------------------------------

def test_prompt_carries_the_plan_and_the_last_summary() -> None:
    """模型要判断"后面还需不需要"，就得知道刚做完了什么。"""
    node, llm = _node(ReplanDecision(revise=False))
    node(_state(), {})
    body = llm.seen[0][-1].content
    assert "读代码（已完成）" in body
    assert "改调用点（下一步）" in body
    assert "跑测试（待办）" in body
    assert "读完了三个文件" in body  # 上一步的小结
    assert "把三个文件里的函数改名" in body  # 原始请求


# --------------------------------------------------------------------------
# 路由与图
# --------------------------------------------------------------------------

def test_route_after_replan() -> None:
    assert route_after_replan(_state()) == ACT
    # 剩余步骤被清空 → 当前步不存在了 → 直接收尾
    assert route_after_replan(_state(plan=["读代码"], step_idx=1)) == RESPOND


def test_exhausted_repair_routes_to_replan_instead_of_giving_up() -> None:
    """修复用尽不是直接收尾 —— 先给 replan 一次「换个做法」的机会。"""
    route = make_route_after_verify(max_repair_rounds=2)
    assert route({"verification": {"status": "failed"}, "retry": 1}) == REPAIR
    assert route({"verification": {"status": "failed"}, "retry": 2}) == REPLAN


# --------------------------------------------------------------------------
# 失败入口：修复用尽之后，先问「换个做法还能不能成」
# --------------------------------------------------------------------------

FAILED = {
    "status": "failed",
    "command": "python3 -m unittest discover",
    "summary": "FAILED (failures=1)",
    "issues": [{"location": "shop/pricing.py:6", "message": "AssertionError"}],
}


def _failed_state(**overrides):
    state = _state(**overrides)
    state["verification"] = dict(FAILED)
    state["retry"] = 3  # 修复预算已用尽
    return state


def test_failure_entry_offers_a_new_approach() -> None:
    node, llm = _node(ReplanDecision(revise=True, steps=["换用整数分做计算", "跑测试"]))
    out = node(_failed_state(), {})
    assert out["plan"] == ["读代码", "换用整数分做计算", "跑测试"]
    assert out["replan_count"] == 1
    assert llm.calls == 1


def test_failure_entry_resets_the_repair_budget() -> None:
    """换了新做法就该有新的修复预算与轮次 —— 否则 act 一看轮次用尽就直接放弃，
    新做法根本没机会被执行。"""
    node, _ = _node(ReplanDecision(revise=True, steps=["换个做法"]))
    out = node(_failed_state(tool_rounds=12, budget_exhausted=True), {})
    assert out["retry"] == 0
    assert out["tool_rounds"] == 0
    assert out["budget_exhausted"] is False
    assert out["verification"] == {}


def test_failure_entry_without_an_idea_drops_the_rest() -> None:
    """没辙了就把剩余步骤砍掉，让任务如实收尾 —— 而不是硬试或假装完成。"""
    node, _ = _node(ReplanDecision(revise=False))
    out = node(_failed_state(), {})
    assert out["plan"] == ["读代码"]
    assert out["replan_count"] == 1


def test_failure_entry_at_the_cap_gives_up_without_asking() -> None:
    node, llm = _node(ReplanDecision(revise=True, steps=["再试"]), max_replans=2)
    out = node(_failed_state(replan_count=2), {})
    assert out["plan"] == ["读代码"]
    assert llm.calls == 0


def test_failure_entry_returning_nothing_is_not_allowed_at_the_cap() -> None:
    """额度用尽时必须给出一个**改过的** plan。

    否则路由会把它送回 act 重试一个已经确定做不成的步骤 —— 死循环。
    """
    node, _ = _node(ReplanDecision(revise=False), max_replans=0)
    assert node(_failed_state(replan_count=0), {}) != {}


def test_failure_entry_survives_a_model_error_by_giving_up() -> None:
    node, _ = _node(raises=True)
    out = node(_failed_state(), {})
    assert out["plan"] == ["读代码"]


def test_failure_entry_prompt_carries_the_failure_detail() -> None:
    node, llm = _node(ReplanDecision(revise=False))
    node(_failed_state(), {})
    body = llm.seen[0][-1].content
    assert "shop/pricing.py:6" in body
    assert "FAILED (failures=1)" in body
    assert "已尝试修复 3 次" in body
    assert "改调用点（没做成）" in body


def test_step_entry_still_uses_the_summary_prompt() -> None:
    """两个入口的提示词不同：步进入口问"后面还需不需要"，失败入口问"换做法行不行"。"""
    node, llm = _node(ReplanDecision(revise=False))
    node(_state(), {})
    assert "刚刚做完" in llm.seen[0][0].content


def test_cleared_plan_skips_act_and_finishes() -> None:
    """把剩余步骤清空后必须**不进 act** —— 否则 act 会去做一个不存在的步骤。"""

    async def scenario() -> None:
        node, _ = _node(ReplanDecision(revise=True, steps=[]))
        reached: list[str] = []

        graph = StateGraph(AgentState)
        graph.add_node("advance", advance)
        graph.add_node(REPLAN, node)
        graph.add_node(ACT, lambda state: reached.append(ACT) or {})
        graph.add_node(RESPOND, lambda state: reached.append(RESPOND) or {})
        graph.add_edge(START, "advance")
        graph.add_edge("advance", REPLAN)
        graph.add_conditional_edges(
            REPLAN, route_after_replan, {ACT: ACT, RESPOND: RESPOND}
        )
        graph.add_edge(ACT, END)
        graph.add_edge(RESPOND, END)
        app = graph.compile(checkpointer=MemorySaver())

        result = await app.ainvoke(
            _state(step_idx=0), {"configurable": {"thread_id": "replan-cleared"}}
        )

        assert reached == [RESPOND]
        assert result["plan"] == ["读代码"]
        assert result["tweak_count"] == 1

    asyncio.run(scenario())


def test_revised_plan_continues_to_act() -> None:
    async def scenario() -> None:
        node, _ = _node(ReplanDecision(revise=True, steps=["新的第 2 步", "新的第 3 步"]))
        reached: list[str] = []

        graph = StateGraph(AgentState)
        graph.add_node("advance", advance)
        graph.add_node(REPLAN, node)
        graph.add_node(ACT, lambda state: reached.append(ACT) or {})
        graph.add_node(RESPOND, lambda state: reached.append(RESPOND) or {})
        graph.add_edge(START, "advance")
        graph.add_edge("advance", REPLAN)
        graph.add_conditional_edges(
            REPLAN, route_after_replan, {ACT: ACT, RESPOND: RESPOND}
        )
        graph.add_edge(ACT, END)
        graph.add_edge(RESPOND, END)
        app = graph.compile(checkpointer=MemorySaver())

        result = await app.ainvoke(
            _state(step_idx=0), {"configurable": {"thread_id": "replan-continue"}}
        )

        assert reached == [ACT]
        assert result["plan"] == ["读代码", "新的第 2 步", "新的第 3 步"]

    asyncio.run(scenario())


# ---------------- 代码审查的阻断也算「没做成」 ----------------
#
# 只认 verification 会让 review 的阻断落进「步进微调」入口 —— 那条路允许返回
# 「不用改」，路由于是把控制流送回 act 重试一个已经确定不合格的步骤，空转到额度耗尽。

def test_blocking_failure_recognizes_both_quality_gates() -> None:
    assert blocking_failure(_state(verification={"status": "failed"}))[0] == "verify"
    assert blocking_failure(_state(review={"status": "blocked"}))[0] == "review"
    assert blocking_failure(_state()) is None
    # 只有 blocked 算阻断：告警与"没跑起来"都不该把任务卡住
    assert blocking_failure(_state(review={"status": "warned"})) is None


def test_review_block_uses_the_failure_entry_not_the_tweak_entry() -> None:
    """审查阻断要走失败入口（换做法），不是步进微调。

    判据看它用的是哪个计数器：失败入口用 `replan_count`。
    """
    node, llm = _node(ReplanDecision(revise=True, steps=["换个做法"]))

    out = node(_state(review={"status": "blocked"}, step_idx=1), {})

    assert out["replan_count"] == 1
    assert "tweak_count" not in out
    assert out["plan"] == ["读代码", "换个做法"]


def test_review_block_at_the_cap_still_returns_a_changed_plan() -> None:
    """额度用尽时必须给一个改过的 plan（砍掉剩余步骤），不能让模型再试。

    返回 `{}` 会被路由送回 act —— 而那一步已经确定不合格，那就是死循环。
    这条正是"只认 verification"会踩到的坑：走微调入口时额度用尽是允许返回 {} 的。
    """
    node, llm = _node(ReplanDecision(revise=True, steps=["不该被问到"]))

    out = node(_state(review={"status": "blocked"}, step_idx=1, replan_count=MAX_REPLANS), {})

    assert out == {"plan": ["读代码"], "replan_count": MAX_REPLANS}
    assert llm.calls == 0  # 到顶之后不再问模型


def test_review_findings_reach_the_model_as_failure_context() -> None:
    """模型要看到「审查到底不满意什么」，否则只能瞎改。"""
    node, llm = _node(ReplanDecision(revise=True, steps=["换个做法"]))

    node(
        _state(
            step_idx=1,
            review={
                "status": "blocked",
                "summary": "审查 2 个改动文件：1 个阻断问题",
                "findings": [
                    {"severity": "blocking", "location": "a.py:3", "message": "新增了 breakpoint()"}
                ],
            },
        ),
        {},
    )

    body = llm.seen[0][-1].content
    assert "没通过代码审查" in body
    assert "a.py:3" in body


def test_review_block_clears_the_stale_conclusions_when_a_new_approach_is_given() -> None:
    """换了新做法，两道质量关的旧结论都作废 —— 它们是针对上一个做法的。"""
    node, _ = _node(ReplanDecision(revise=True, steps=["换个做法"]))

    out = node(_state(review={"status": "blocked"}, step_idx=1), {})

    assert out["review"] == {}
    assert out["verification"] == {}
    assert out["retry"] == 0
