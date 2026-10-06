"""AgentRuntime 的事件翻译循环。

这是整个架构的中枢：把 LangGraph 的原始流翻成前端认识的领域事件。
TUI 测试用的是假 runtime，所以这一层此前几乎没被覆盖。

做法是注入一张**脚本化的假图**——不碰 LLM、不碰沙箱，
只喂给 runtime 与真实图结构一致的 `(mode, data)` 序列，
断言它翻译出来的事件与审计记录。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from coding_agent.audit import AuditLogger, read_records
from coding_agent.config import Settings
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    Event,
    FileChanged,
    PlanCreated,
    RepairStarted,
    RunFailed,
    RunFinished,
    RunStarted,
    StepFinished,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
    Verification,
)
from coding_agent.runtime import AgentRuntime
from coding_agent.tools.artifacts import CallArtifact, FileArtifact, ShellArtifact

THREAD = "t-1"


class _NullMemory:
    """`run()` 开头会读 `.agent/memory.md`（走 WSL）。

    这些是**无沙箱**的单元测试：图已被 FakeGraph 顶替，记忆也一并屏蔽，
    否则在 Linux CI（无 wsl.exe）上会因读记忆文件而失败。
    """

    def load(self) -> list[str]:
        return []


def _no_wsl(runtime: AgentRuntime) -> AgentRuntime:
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    return runtime


class FakeGraph:
    """按脚本回放 LangGraph 的流；可选在某个位置抛异常。"""

    def __init__(self, script: list[tuple[str, Any]], *, error: Exception | None = None):
        self.script = script
        self.error = error
        self.payloads: list[Any] = []
        self.configs: list[dict] = []

    async def astream(self, payload, config=None, stream_mode=None):  # noqa: ANN001
        self.payloads.append(payload)
        self.configs.append(config or {})
        for mode, data in self.script:
            if self.error is not None and data is self.error:
                raise self.error
            yield mode, data


def _token(text: str, node: str) -> tuple[str, tuple]:
    return ("messages", (AIMessageChunk(content=text), {"langgraph_node": node}))


def _updates(**kwargs) -> tuple[str, dict]:
    return ("updates", kwargs)


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _shell_result(call_id: str, *, ok: bool = True, rejected: bool = False,
                  decision: str = "auto") -> ToolMessage:
    artifact = ShellArtifact(
        command="ls", ok=ok, rejected=rejected, decision=decision,
        exit_code=0 if ok else 1, duration_ms=12, level=0, level_label="L0 只读",
    )
    return ToolMessage(
        content=f"ok={ok}", tool_call_id=call_id, name="shell_exec",
        artifact=artifact.model_dump(),
    )


def _runtime(tmp_path, script: list, *, error: Exception | None = None, **overrides):
    settings = Settings(_env_file=None, audit_dir=str(tmp_path), **overrides)
    runtime = AgentRuntime(
        settings, workspace="/mnt/d/proj", audit=AuditLogger(tmp_path)
    )
    graph = FakeGraph(script, error=error)
    runtime._graph = graph  # 绕开建图（建图需要 API Key）
    return _no_wsl(runtime), graph


async def _collect(runtime: AgentRuntime, prompt: str = "干活") -> list[Event]:
    return [event async for event in runtime.run(prompt, thread_id=THREAD)]


async def _collect_resume(runtime: AgentRuntime, decisions: dict[str, bool]) -> list[Event]:
    return [event async for event in runtime.resume(THREAD, decisions)]


def _run(coro) -> Any:
    return asyncio.run(coro)


def _kinds(events: list[Event]) -> list[str]:
    return [e.type for e in events]


# ---------------- 完整一轮 ----------------

def test_full_run_event_sequence(tmp_path) -> None:
    script = [
        _updates(planner={"plan": ["看目录", "改文件"]}),
        ("messages", (AIMessageChunk(content="先看看"), {"langgraph_node": "act"})),
        _updates(act={"messages": [_tool_call("shell_exec", {"command": "ls"}, "c1")]}),
        _updates(tools={"messages": [_shell_result("c1")]}),
        ("messages", (AIMessageChunk(content="目录是空的"), {"langgraph_node": "act"})),
        _updates(act={"messages": [AIMessage(content="目录是空的")]}),
        _updates(verify={"verification": {
            "status": "ok", "command": "pytest", "summary": "1 passed",
        }}),
        _updates(advance={"step_idx": 1}),
        ("messages", (AIMessageChunk(content="最终结论"), {"langgraph_node": "respond"})),
        _updates(respond={"messages": [AIMessage(content="最终结论")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    assert _kinds(events) == [
        "run_started",
        "plan_created",
        "step_started",
        "assistant_token",
        "tool_call_started",
        "tool_call_finished",
        "assistant_token",
        "step_finished",
        "verification",
        "step_started",
        "assistant_token",
        # 第 2 步开了头却直接收尾（真实图里由 replan 砍掉剩余步骤触发），
        # 收尾前必须补一个 step_finished —— 否则前端进度条永远停在「进行中」
        "step_finished",
        "run_finished",
    ]


def test_run_finished_carries_the_final_answer(tmp_path) -> None:
    script = [
        ("messages", (AIMessageChunk(content="答案"), {"langgraph_node": "respond"})),
        _updates(respond={"messages": [AIMessage(content="完整答案")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    finished = events[-1]
    assert isinstance(finished, RunFinished)
    assert finished.answer == "完整答案"
    assert finished.thread_id == THREAD


def test_answer_falls_back_to_streamed_tokens(tmp_path) -> None:
    """respond 没产出完整消息时，用流式攒下来的 token 兜底。"""
    script = [
        ("messages", (AIMessageChunk(content="流式"), {"langgraph_node": "respond"})),
        ("messages", (AIMessageChunk(content="答案"), {"langgraph_node": "respond"})),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))
    assert events[-1].answer == "流式答案"


def test_planner_emits_plan_then_first_step(tmp_path) -> None:
    runtime, _ = _runtime(tmp_path, [_updates(planner={"plan": ["甲", "乙"]})])
    events = _run(_collect(runtime))

    plan = next(e for e in events if isinstance(e, PlanCreated))
    started = next(e for e in events if isinstance(e, StepStarted))
    assert plan.steps == ["甲", "乙"]
    assert (started.index, started.total, started.text) == (0, 2, "甲")


def test_advance_emits_step_with_plan_text(tmp_path) -> None:
    script = [_updates(planner={"plan": ["甲", "乙"]}), _updates(advance={"step_idx": 1})]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    steps = [e for e in events if isinstance(e, StepStarted)]
    assert (steps[-1].index, steps[-1].total, steps[-1].text) == (1, 2, "乙")


def test_only_act_and_respond_tokens_are_forwarded(tmp_path) -> None:
    """planner 的结构化调用不该被当成给用户看的文本流出去。"""
    script = [
        ("messages", (AIMessageChunk(content="内部推理"), {"langgraph_node": "planner"})),
        ("messages", (AIMessageChunk(content="给用户看"), {"langgraph_node": "act"})),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    texts = [e.text for e in events if isinstance(e, AssistantToken)]
    assert texts == ["给用户看"]


def test_empty_token_chunks_are_skipped(tmp_path) -> None:
    script = [
        ("messages", (AIMessageChunk(content=""), {"langgraph_node": "act"})),
        ("messages", (AIMessageChunk(content="有内容"), {"langgraph_node": "act"})),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))
    assert [e.text for e in events if isinstance(e, AssistantToken)] == ["有内容"]


# ---------------- 工具调用 ----------------

def test_tool_call_started_is_annotated_with_level(tmp_path) -> None:
    script = [_updates(act={"messages": [
        _tool_call("shell_exec", {"command": "git commit -m x"}, "c1")
    ]})]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    started = next(e for e in events if isinstance(e, ToolCallStarted))
    assert started.call_id == "c1"
    assert started.level == "L2 变更性"
    assert started.summary == "git commit -m x"


def test_tool_pairing_uses_call_id_not_order(tmp_path) -> None:
    """同一轮可能有多个并行调用，只能靠 call_id 配对。"""
    script = [
        _updates(act={"messages": [
            _tool_call("shell_exec", {"command": "ls"}, "id-1"),
            _tool_call("shell_exec", {"command": "pwd"}, "id-2"),
        ]}),
        _updates(tools={"messages": [_shell_result("id-2"), _shell_result("id-1")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert [e.call_id for e in finished] == ["id-2", "id-1"]


def test_denied_tool_call_is_marked(tmp_path) -> None:
    script = [
        _updates(act={"messages": [_tool_call("shell_exec", {"command": "rm -rf /"}, "c1")]}),
        _updates(tools={"messages": [_shell_result("c1", ok=False, rejected=True,
                                                   decision="denied")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.rejected is True
    assert finished.decision == "denied"


def test_denied_non_file_tool_keeps_its_own_level(tmp_path) -> None:
    """git_commit / deps_install / run_tests 被拒时，level 应是工具自身的等级。

    早先这些调用一律产出 FileArtifact，事件层于是把它们标成「文件工具」，
    审计里的 level 也跟着错。
    """
    script = [
        _updates(act={"messages": [_tool_call("git_commit", {"message": "x"}, "c1")]}),
        _updates(tools={"messages": [ToolMessage(
            content="用户拒绝了这条命令",
            tool_call_id="c1",
            name="git_commit",
            artifact=CallArtifact(
                tool="git_commit", ok=False, rejected=True, decision="denied",
                level=2, level_label="L2 变更性",
            ).model_dump(),
        )]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.level == "L2 变更性"
    assert finished.rejected is True
    assert not [e for e in events if isinstance(e, FileChanged)]


def test_run_started_opens_the_stream_but_resume_does_not(tmp_path) -> None:
    """事件流的起点标记只属于全新 run；resume 是同一次运行的延续，不重复发。"""
    script = [_updates(respond={"messages": [AIMessage(content="好了")]})]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    assert isinstance(events[0], RunStarted)
    assert len([e for e in events if isinstance(e, RunStarted)]) == 1

    runtime._graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="继续")]})])
    resumed = _run(_collect_resume(runtime, {}))
    assert not [e for e in resumed if isinstance(e, RunStarted)]


def test_file_change_is_emitted_only_for_mutations(tmp_path) -> None:
    def _file_msg(action: str) -> ToolMessage:
        artifact = FileArtifact(path="a.py", action=action, ok=True, added=1, removed=1,
                                diff="--- a\n+++ b\n", snapshot_id="s1")
        return ToolMessage(content="x", tool_call_id="c1", name="file_edit",
                           artifact=artifact.model_dump())

    script = [
        _updates(act={"messages": [_tool_call("file_read", {"path": "a.py"}, "c1")]}),
        _updates(tools={"messages": [_file_msg("read")]}),
        _updates(act={"messages": [_tool_call("file_edit", {"path": "a.py"}, "c2")]}),
        _updates(tools={"messages": [_file_msg("edit")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    changes = [e for e in events if isinstance(e, FileChanged)]
    assert len(changes) == 1
    assert changes[0].path == "a.py"
    assert changes[0].snapshot_id == "s1"


def test_step_finished_flags_budget_exhaustion(tmp_path) -> None:
    script = [_updates(act={"messages": [AIMessage(content="停下了")],
                            "budget_exhausted": True})]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    finished = next(e for e in events if isinstance(e, StepFinished))
    assert finished.budget_exhausted is True
    assert finished.text == "停下了"


# ---------------- 验证与修复 ----------------

def test_verification_failure_is_forwarded_with_issues(tmp_path) -> None:
    script = [_updates(verify={"verification": {
        "status": "failed", "command": "pytest -q", "summary": "1 failed",
        "issues": [{"location": "a.py:3", "message": "boom"}],
    }})]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    verification = next(e for e in events if isinstance(e, Verification))
    assert verification.status == "failed"
    assert verification.ok is False
    assert verification.issues == ["a.py:3 boom"]


def test_empty_verification_update_is_ignored(tmp_path) -> None:
    """没跑验证时节点返回空 dict，不该发一个「跳过」事件出去。"""
    runtime, _ = _runtime(tmp_path, [_updates(verify={"verification": {}})])
    events = _run(_collect(runtime))
    assert not [e for e in events if isinstance(e, Verification)]


def test_repair_event_carries_the_failure_context(tmp_path) -> None:
    script = [
        _updates(verify={"verification": {
            "status": "failed", "summary": "1 failed",
            "issues": [{"location": "a.py:3", "message": "boom"}],
        }}),
        _updates(repair={"retry": 1}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    repair = next(e for e in events if isinstance(e, RepairStarted))
    assert repair.attempt == 1
    assert repair.summary == "1 failed"
    assert repair.issues == ["a.py:3 boom"]


# ---------------- 挂起与恢复 ----------------

class _Interrupt:
    def __init__(self, value: dict) -> None:
        self.value = value


def test_interrupt_emits_approval_and_stops_without_run_finished(tmp_path) -> None:
    request = {"call_id": "c1", "tool": "shell_exec", "command": "pip install x",
               "level": "L2 变更性", "reason": "装依赖"}
    script = [
        _updates(act={"messages": [_tool_call("shell_exec", {"command": "pip install x"}, "c1")]}),
        _updates(__interrupt__=(_Interrupt({"requests": [request]}),)),
        _updates(respond={"messages": [AIMessage(content="不该到这")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    kinds = _kinds(events)
    assert "approval_requested" in kinds
    # 挂起时不能发 RunFinished —— 前端据此判断要 resume 而不是收工
    assert "run_finished" not in kinds

    approval = next(e for e in events if isinstance(e, ApprovalRequested))
    assert approval.request_id == "c1"
    assert approval.level == "L2 变更性"
    assert approval.tool == "shell_exec"


def test_resume_sends_a_command_payload(tmp_path) -> None:
    runtime, graph = _runtime(
        tmp_path, [_updates(respond={"messages": [AIMessage(content="好了")]})]
    )
    events = _run(_collect_resume(runtime, {"c1": True}))

    payload = graph.payloads[-1]
    assert type(payload).__name__ == "Command"
    assert payload.resume == {"c1": True}
    assert events[-1].type == "run_finished"


def test_resume_does_not_emit_run_start_audit(tmp_path) -> None:
    runtime, _ = _runtime(tmp_path, [_updates(respond={"messages": [AIMessage(content="x")]})])
    _run(_collect(runtime))
    _run(_collect_resume(runtime, {"c1": True}))

    kinds = [r.kind for r in read_records(runtime.audit_path)]
    assert kinds.count("run_start") == 1


def test_run_end_audit_records_token_usage(tmp_path) -> None:
    """把模型回报的 token 用量累计进 run_end 审计（不引入 tokenizer）。"""
    script = [
        _updates(act={"messages": [AIMessage(
            content="",
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        )]}),
        _updates(respond={"messages": [AIMessage(
            content="done",
            usage_metadata={"input_tokens": 30, "output_tokens": 10, "total_tokens": 40},
        )]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    _run(_collect(runtime))

    end = next(r for r in read_records(runtime.audit_path) if r.kind == "run_end")
    assert (end.input_tokens, end.output_tokens) == (130, 30)


def test_run_end_has_no_token_fields_when_provider_omits_usage(tmp_path) -> None:
    runtime, _ = _runtime(tmp_path, [_updates(respond={"messages": [AIMessage(content="x")]})])
    _run(_collect(runtime))
    end = next(r for r in read_records(runtime.audit_path) if r.kind == "run_end")
    assert end.input_tokens is None and end.output_tokens is None


def test_token_usage_accumulates_across_suspend_and_resume(tmp_path) -> None:
    """挂起前那一段的用量不能丢。

    两个计数器曾经是 `_stream` 的局部变量，挂起时 `return` 把它们一起丢掉，
    resume 后的收尾只记恢复段。而挂起最常见于 L2/L3 审批 —— 恰恰是最费 token
    的路径，于是成本口径会系统性偏低。
    """
    request = {"call_id": "c1", "tool": "shell_exec", "command": "pip install x",
               "level": "L2 变更性", "reason": "装依赖"}
    runtime, graph = _runtime(tmp_path, [
        _updates(act={"messages": [AIMessage(
            content="",
            tool_calls=[{"name": "shell_exec", "args": {"command": "pip install x"}, "id": "c1"}],
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        )]}),
        _updates(__interrupt__=(_Interrupt({"requests": [request]}),)),
    ])
    first = _run(_collect(runtime))
    assert "run_finished" not in _kinds(first)  # 确实挂起了

    # 恢复段：模型给出最终答复，也回报了用量
    graph.script = [_updates(respond={"messages": [AIMessage(
        content="好了",
        usage_metadata={"input_tokens": 30, "output_tokens": 10, "total_tokens": 40},
    )]})]
    second = _run(_collect_resume(runtime, {"c1": True}))

    end = next(e for e in second if isinstance(e, RunFinished))
    assert (end.input_tokens, end.output_tokens) == (130, 30)

    # 审计与事件同源：两边都应是 130/30
    audit_end = next(r for r in read_records(runtime.audit_path) if r.kind == "run_end")
    assert (audit_end.input_tokens, audit_end.output_tokens) == (130, 30)


def test_run_failed_carries_token_usage(tmp_path) -> None:
    """失败的那一轮花费可能最多（反复重试、长上下文）。

    把它排除在成本口径外，均值就只由成功的任务贡献 —— 越难的任务系统性地
    越"便宜"，这正是评测最需要避免的偏差。
    """
    boom = RuntimeError("模型炸了")
    script = [
        _updates(act={"messages": [AIMessage(
            content="",
            usage_metadata={"input_tokens": 70, "output_tokens": 5, "total_tokens": 75},
        )]}),
        ("updates", boom),
    ]
    runtime, _ = _runtime(tmp_path, script, error=boom)

    failed = _run(_collect(runtime))[-1]
    assert isinstance(failed, RunFailed)
    assert (failed.input_tokens, failed.output_tokens) == (70, 5)


def test_resume_is_capped_per_thread(tmp_path) -> None:
    """恢复次数有上限，防止前端反复 resume 绕开 recursion_limit。"""
    runtime, _ = _runtime(
        tmp_path, [_updates(respond={"messages": [AIMessage(content="x")]})], max_resumes=1
    )
    first = _run(_collect_resume(runtime, {"c1": True}))
    assert not any(isinstance(e, RunFailed) for e in first)

    second = _run(_collect_resume(runtime, {"c1": True}))
    assert isinstance(second[0], RunFailed)
    assert "上限" in second[0].message


def test_fresh_run_resets_resume_counter(tmp_path) -> None:
    runtime, _ = _runtime(
        tmp_path, [_updates(respond={"messages": [AIMessage(content="x")]})], max_resumes=1
    )
    _run(_collect_resume(runtime, {"c1": True}))  # 用掉一次
    _run(_collect(runtime))                        # 新一轮应把计数清零

    again = _run(_collect_resume(runtime, {"c1": True}))
    assert not any(isinstance(e, RunFailed) for e in again)


def test_plan_survives_resume_for_step_events(tmp_path) -> None:
    """挂起恢复后，advance 发出的 StepStarted 仍要带上计划文案。

    否则恢复后的步进事件会退化成 total=0、空文案（前端看不到"第几步、做什么"）。
    """
    request = {"call_id": "c1", "tool": "shell_exec", "command": "pip install x",
               "level": "L2 变更性", "reason": "装依赖"}
    run_script = [
        _updates(planner={"plan": ["第一步", "第二步"]}),
        _updates(act={"messages": [
            _tool_call("shell_exec", {"command": "pip install x"}, "c1")
        ]}),
        _updates(__interrupt__=(_Interrupt({"requests": [request]}),)),
    ]
    resume_script = [
        _updates(tools={"messages": [_shell_result("c1", decision="approved")]}),
        _updates(verify={"verification": {"status": "ok", "command": "pytest", "summary": "OK"}}),
        _updates(advance={"step_idx": 1}),
        _updates(respond={"messages": [AIMessage(content="完成")]}),
    ]
    runtime, _ = _runtime(tmp_path, run_script)
    _run(_collect(runtime))

    runtime._graph = FakeGraph(resume_script)
    events = _run(_collect_resume(runtime, {"c1": True}))

    steps = [e for e in events if isinstance(e, StepStarted)]
    assert steps, "恢复后应仍发出 StepStarted"
    assert (steps[-1].index, steps[-1].total, steps[-1].text) == (1, 2, "第二步")


def test_approved_tool_call_keeps_args_across_resume(tmp_path) -> None:
    """需要审批的调用跨 run/resume 后，审计仍要记到命令参数。

    这类调用在挂起时结束一次 `_stream`、resume 时才执行，恰恰是风险最高的一批；
    配对表若只活在 `_stream` 局部，恢复后 args 会退化成空 dict。
    """
    request = {"call_id": "c1", "tool": "shell_exec", "command": "pip install x",
               "level": "L2 变更性", "reason": "装依赖"}
    run_script = [
        _updates(act={"messages": [
            _tool_call("shell_exec", {"command": "pip install x", "reason": "装依赖"}, "c1")
        ]}),
        _updates(__interrupt__=(_Interrupt({"requests": [request]}),)),
    ]
    resume_script = [
        _updates(tools={"messages": [ToolMessage(
            content="installed",
            tool_call_id="c1",
            name="shell_exec",
            artifact=ShellArtifact(
                command="pip install x", ok=True, decision="approved",
                exit_code=0, level=2, level_label="L2 变更性",
            ).model_dump(),
        )]}),
        _updates(respond={"messages": [AIMessage(content="好了")]}),
    ]
    runtime, _ = _runtime(tmp_path, run_script)
    _run(_collect(runtime))

    runtime._graph = FakeGraph(resume_script)  # 模拟 resume 进入另一次 _stream
    _run(_collect_resume(runtime, {"c1": True}))

    call = next(r for r in read_records(runtime.audit_path) if r.kind == "tool_call")
    assert call.decision == "approved"
    assert call.call_id == "c1"
    assert call.args["command"] == "pip install x"
    assert call.args["reason"] == "装依赖"
    assert call.level == "L2 变更性"


# ---------------- 异常 ----------------

class _BoomMemory:
    def load(self) -> list[str]:
        raise RuntimeError("记忆文件读不了")


def test_thread_state_is_released_when_the_run_finishes(tmp_path) -> None:
    """收尾后必须清掉按 thread_id 累积的状态。

    `_pending_calls` / `_progress` 只为「挂起与恢复是两次 `_stream`」而存在，
    跑完就再无用处。TUI 一个进程能跑几十轮，不清只增不减。
    """
    runtime, _ = _runtime(tmp_path, [_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._pending_calls[THREAD] = {"stale": None}  # type: ignore[dict-item]
    runtime._resume_counts[THREAD] = 1

    _run(_collect(runtime))

    assert runtime._pending_calls == {}
    assert runtime._progress == {}
    # 恢复次数**不清**：它护栏的是「跑完还继续 resume」，清了就形同虚设
    assert runtime._resume_counts == {THREAD: 0}


def test_thread_state_is_released_when_the_run_fails(tmp_path) -> None:
    boom = RuntimeError("模型炸了")
    script = [_updates(act={"messages": [AIMessage(content="x")]})]
    script.append(("updates", boom))
    runtime, _ = _runtime(tmp_path, script, error=boom)
    runtime._pending_calls[THREAD] = {"stale": None}  # type: ignore[dict-item]

    events = _run(_collect(runtime))

    assert isinstance(events[-1], RunFailed)
    assert runtime._pending_calls == {}
    assert runtime._progress == {}


def test_thread_state_survives_a_suspension(tmp_path) -> None:
    """反过来：挂起时**不能**清 —— resume 是另一次 _stream，全靠这些状态续上。"""
    request = {"call_id": "c1", "tool": "shell_exec", "command": "pip install x",
               "level": "L2 变更性", "reason": "装依赖"}
    script = [
        _updates(act={"messages": [
            _tool_call("shell_exec", {"command": "pip install x"}, "c1")
        ]}),
        _updates(__interrupt__=(_Interrupt({"requests": [request]}),)),
    ]
    runtime, _ = _runtime(tmp_path, script)

    _run(_collect(runtime))

    assert THREAD in runtime._pending_calls
    assert THREAD in runtime._progress


def test_memory_read_failure_becomes_run_failed(tmp_path) -> None:
    """启动即失败（读记忆需要 WSL）也要以事件收尾，而不是裸异常外泄给前端。"""
    runtime, _ = _runtime(tmp_path, [])
    runtime._memory = _BoomMemory()  # type: ignore[assignment]

    events = _run(_collect(runtime))

    assert isinstance(events[0], RunFailed)
    assert "RuntimeError" in events[0].message


def test_graph_failure_becomes_run_failed(tmp_path) -> None:
    boom = RuntimeError("模型炸了")
    script = [_updates(act={"messages": [AIMessage(content="x")]})]
    script.append(("updates", boom))
    runtime, _ = _runtime(tmp_path, script, error=boom)

    events = _run(_collect(runtime))
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert "RuntimeError" in failed.message and "模型炸了" in failed.message


def test_audit_write_failure_surfaces_as_run_failed(tmp_path) -> None:
    """审计有缺口是这个项目不能接受的失败模式：宁可显式失败也不静默丢记录。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("我是个文件，不是目录", encoding="utf-8")
    settings = Settings(_env_file=None)
    runtime = AgentRuntime(
        settings, workspace="/mnt/d/proj", audit=AuditLogger(blocker / "audit")
    )
    runtime._graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="x")]})])
    _no_wsl(runtime)

    events = _run(_collect(runtime))
    assert isinstance(events[-1], RunFailed)
    assert "审计日志写入失败" in events[-1].message


# ---------------- trace 配置 ----------------

def test_run_config_carries_trace_metadata(tmp_path) -> None:
    runtime, graph = _runtime(tmp_path, [])
    _run(_collect(runtime))

    config = graph.configs[-1]
    assert config["run_name"] == f"agent:{THREAD}"
    assert "coding-agent" in config["tags"]
    assert config["metadata"]["thread_id"] == THREAD
    assert config["configurable"]["thread_id"] == THREAD


# ---------------- 审计完整性 ----------------

def test_audit_records_the_whole_run(tmp_path) -> None:
    script = [
        _updates(planner={"plan": ["一步"]}),
        _updates(act={"messages": [_tool_call("shell_exec", {"command": "ls"}, "c1")]}),
        _updates(tools={"messages": [_shell_result("c1")]}),
        _updates(verify={"verification": {"status": "ok", "command": "pytest", "summary": "OK"}}),
        _updates(respond={"messages": [AIMessage(content="完成")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    _run(_collect(runtime, "看看目录"))

    records = read_records(runtime.audit_path)
    kinds = [r.kind for r in records]
    assert kinds[0] == "run_start"
    assert kinds[-1] == "run_end"
    assert {"plan", "tool_call", "verify"} <= set(kinds)

    start = records[0]
    assert start.thread_id == THREAD
    assert start.workspace == "/mnt/d/proj"
    assert start.detail == "看看目录"


def test_audit_records_approved_decision(tmp_path) -> None:
    script = [
        _updates(act={"messages": [_tool_call("shell_exec", {"command": "pip install x"}, "c1")]}),
        _updates(tools={"messages": [_shell_result("c1", decision="approved")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    _run(_collect(runtime))

    call = next(r for r in read_records(runtime.audit_path) if r.kind == "tool_call")
    assert call.decision == "approved"
    assert call.call_id == "c1"
    assert call.args["command"] == "pip install x"


def test_audit_record_for_unknown_tool_has_no_level(tmp_path) -> None:
    script = [
        _updates(act={"messages": [_tool_call("nope", {}, "c1")]}),
        _updates(tools={"messages": [
            ToolMessage(content="没有这个工具", tool_call_id="c1", name="nope")
        ]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    _run(_collect(runtime))

    call = next(r for r in read_records(runtime.audit_path) if r.kind == "tool_call")
    assert call.tool == "nope"
    assert call.level == ""


@pytest.mark.parametrize("prompt", ["短", "很长" * 500])
def test_prompt_is_recorded_within_limits(tmp_path, prompt: str) -> None:
    runtime, _ = _runtime(tmp_path, [])
    _run(_collect(runtime, prompt))

    start = read_records(runtime.audit_path)[0]
    assert start.detail
    assert len(start.detail) <= 2100


# ---------------- 步骤事件配对（B5） ----------------


def _step_pairs(events: list[Event]) -> list[tuple[str, int]]:
    """把步骤事件压成 [("start"|"finish", index)]，用于断言配对关系。"""
    pairs: list[tuple[str, int]] = []
    for event in events:
        if isinstance(event, StepStarted):
            pairs.append(("start", event.index))
        elif isinstance(event, StepFinished):
            pairs.append(("finish", event.index))
    return pairs


def test_replan_that_cancels_the_current_step_closes_it(tmp_path) -> None:
    """计划被砍到当前这步之前时 act 再也不跑 —— 就地收口，别让进度条卡住。

    这条路径是「反复修复都做不成 → 重规划判定放弃」，真实存在：
    replan 返回 plan[:step_idx]，路由直接送 respond。
    """
    script = [
        _updates(planner={"plan": ["甲", "乙", "丙"]}),
        _updates(act={"messages": [AIMessage(content="甲做完了")]}),
        _updates(advance={"step_idx": 1}),
        _updates(act={"messages": [_tool_call("shell_exec", {"command": "ls"}, "c1")]}),
        _updates(tools={"messages": [_shell_result("c1")]}),
        _updates(verify={"verification": {"status": "failed", "summary": "1 failed"}}),
        _updates(repair={"retry": 1}),
        _updates(replan={"plan": ["甲"]}),
        _updates(respond={"messages": [AIMessage(content="收尾")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    # 每个 StepStarted 都有配对，且第 2 步是被放弃的
    assert _step_pairs(events) == [("start", 0), ("finish", 0), ("start", 1), ("finish", 1)]
    cancelled = [e for e in events if isinstance(e, StepFinished) and e.cancelled]
    assert len(cancelled) == 1
    assert cancelled[0].index == 1
    # 被放弃 ≠ 工具预算耗尽：它根本没试过，不能说成"试过但用完了"
    assert cancelled[0].budget_exhausted is False


def test_open_step_is_closed_before_run_finished(tmp_path) -> None:
    """兜底：任何让 act 没能收口的路径，都要在收尾前补上 StepFinished。"""
    script = [
        _updates(planner={"plan": ["甲", "乙"]}),
        _updates(respond={"messages": [AIMessage(content="收尾")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    assert _step_pairs(events) == [("start", 0), ("finish", 0)]
    assert isinstance(events[-1], RunFinished)


def test_open_step_is_closed_before_run_failed(tmp_path) -> None:
    """异常路径同样要收口 —— 否则前端会把它显示成「仍在运行」。"""
    boom = RuntimeError("模型炸了")
    script = [
        _updates(planner={"plan": ["甲", "乙"]}),
        _updates(act={"messages": [_tool_call("shell_exec", {"command": "ls"}, "c1")]}),
        ("updates", boom),
    ]
    runtime, _ = _runtime(tmp_path, script, error=boom)
    events = _run(_collect(runtime))

    assert _step_pairs(events) == [("start", 0), ("finish", 0)]
    assert isinstance(events[-1], RunFailed)


def test_a_completed_run_emits_no_extra_step_finished(tmp_path) -> None:
    """正常路径不能因为加了兜底就多冒出一个 StepFinished。"""
    script = [
        _updates(planner={"plan": ["甲", "乙"]}),
        _updates(act={"messages": [AIMessage(content="甲做完了")]}),
        _updates(advance={"step_idx": 1}),
        _updates(act={"messages": [AIMessage(content="乙做完了")]}),
        _updates(respond={"messages": [AIMessage(content="收尾")]}),
    ]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    assert _step_pairs(events) == [
        ("start", 0), ("finish", 0), ("start", 1), ("finish", 1),
    ]
    assert not [e for e in events if isinstance(e, StepFinished) and e.cancelled]


# ---------------- 验证「ok」口径（B7） ----------------


@pytest.mark.parametrize("status", ["ok", "skipped", "not_configured", "failed"])
def test_verification_ok_agrees_with_the_audit_record(tmp_path, status: str) -> None:
    """事件与审计必须对同一次验证给出同样的结论 —— 审计是可信来源。

    曾经事件按 `status in (ok, skipped, not_configured)` 判、审计按 `== "ok"` 判，
    于是「没跑验证」在两边结论相反，事后对账对不上。
    """
    script = [_updates(verify={"verification": {
        "status": status, "command": "pytest -q", "summary": "s",
    }})]
    runtime, _ = _runtime(tmp_path, script)
    events = _run(_collect(runtime))

    event = next(e for e in events if isinstance(e, Verification))
    record = next(r for r in read_records(runtime.audit_path) if r.kind == "verify")
    assert event.ok == record.ok
    assert event.ok is (status != "failed")


def test_not_running_verification_is_not_a_failure(tmp_path) -> None:
    """skipped / not_configured 是「没验证」，不能记成验证失败。"""
    runtime, _ = _runtime(tmp_path, [
        _updates(verify={"verification": {"status": "skipped", "summary": "没有改动"}}),
    ])
    _run(_collect(runtime))

    record = next(r for r in read_records(runtime.audit_path) if r.kind == "verify")
    assert record.ok is True
