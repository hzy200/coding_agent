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
    StepFinished,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
    Verification,
)
from coding_agent.runtime import AgentRuntime
from coding_agent.tools.artifacts import FileArtifact, ShellArtifact

THREAD = "t-1"


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
    return runtime, graph


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


# ---------------- 异常 ----------------

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
