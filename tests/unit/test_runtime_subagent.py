"""runtime 对子智能体事件的翻译、token 累计与审计。

用脚本化假图回放 `(mode, data)`，不碰 LLM / 沙箱。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from coding_agent.audit import AuditLogger, read_records
from coding_agent.config import Settings
from coding_agent.events import RunFinished, SubAgentFinished, SubAgentStarted
from coding_agent.runtime import AgentRuntime
from coding_agent.tools.artifacts import CallArtifact, SubAgentArtifact, pack
from coding_agent.tools.delegate import DELEGATE_EXPLORE

THREAD = "t-sub"


class _NullMemory:
    def load(self) -> list[str]:
        return []


class FakeGraph:
    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = script

    async def astream(self, payload, config=None, stream_mode=None, subgraphs=None):  # noqa: ANN001
        for mode, data in self.script:
            yield mode, data


def _updates(**kwargs: Any) -> tuple[str, dict]:
    return ("updates", kwargs)


def _runtime(tmp_path) -> AgentRuntime:
    settings = Settings(_env_file=None, audit_dir=str(tmp_path))
    runtime = AgentRuntime(
        settings, workspace="/mnt/d/proj", audit=AuditLogger(tmp_path)
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    return runtime


def _artifact(
    call_id: str = "d1",
    *,
    goal: str = "找到入口",
    summary: str = "入口在 main.py",
    input_tokens: int = 300,
    output_tokens: int = 40,
) -> SubAgentArtifact:
    return SubAgentArtifact(
        agent_id=f"explore:{call_id}",
        parent_call_id=call_id,
        goal=goal,
        status="success",
        summary=summary,
        relevant_files=["main.py"],
        findings=["main() 在 main.py:3"],
        tool_calls=[
            {
                "call_id": f"child-{call_id}",
                "name": "file_read",
                "args": {"path": "main.py", "reason": "看入口"},
                "ok": True,
                "duration_ms": 7,
                "round": 1,
            }
        ],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=1200,
    )


def _delegate_call(call_id: str = "d1", goal: str = "找到入口") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": DELEGATE_EXPLORE,
                "args": {"goal": goal, "paths": [], "reason": "不熟"},
                "id": call_id,
            }
        ],
    )


def _delegate_result(
    artifact: SubAgentArtifact | None, *, call_id: str = "d1"
) -> ToolMessage:
    if artifact is None:
        return ToolMessage(
            content="工具执行异常：RuntimeError: boom",
            tool_call_id=call_id,
            name=DELEGATE_EXPLORE,
            artifact=None,
        )
    return ToolMessage(
        content=pack("摘要", artifact),
        tool_call_id=artifact.parent_call_id or call_id,
        name=DELEGATE_EXPLORE,
        artifact=artifact.model_dump(),
    )


def _collect(runtime: AgentRuntime) -> list[Any]:
    return asyncio.run(_collect_async(runtime))


async def _collect_async(runtime: AgentRuntime) -> list[Any]:
    return [event async for event in runtime.run("找入口", thread_id=THREAD)]


def test_subagent_events_are_paired_and_tokens_are_counted(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._graph = FakeGraph(
        [
            _updates(act={"messages": [_delegate_call()]}),
            _updates(tools={"messages": [_delegate_result(_artifact())]}),
            _updates(
                respond={
                    "messages": [
                        AIMessage(
                            content="完成",
                            usage_metadata={
                                "input_tokens": 50,
                                "output_tokens": 10,
                                "total_tokens": 60,
                            },
                        )
                    ]
                }
            ),
        ]
    )
    events = _collect(runtime)

    started = next(e for e in events if isinstance(e, SubAgentStarted))
    finished = next(e for e in events if isinstance(e, SubAgentFinished))
    assert (started.agent_id, started.parent_call_id) == ("d1", "d1")
    assert started.goal == "找到入口"
    assert (finished.agent_id, finished.status) == ("explore:d1", "success")
    assert finished.summary == "入口在 main.py"
    assert finished.relevant_files == ["main.py"]
    assert events.index(started) < events.index(finished)

    # 子智能体的 300/40 必须计入本轮总量
    run_end = next(e for e in events if isinstance(e, RunFinished))
    assert (run_end.input_tokens, run_end.output_tokens) == (350, 50)


def test_multiple_subagents_are_paired_by_call_id(tmp_path) -> None:
    """同一条 act 消息里的多个 delegate 并发执行后，事件仍按 call_id 配对。"""
    runtime = _runtime(tmp_path)
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": DELEGATE_EXPLORE,
                "args": {"goal": "甲", "paths": [], "reason": "并发"},
                "id": "d0",
            },
            {
                "name": DELEGATE_EXPLORE,
                "args": {"goal": "乙", "paths": [], "reason": "并发"},
                "id": "d1",
            },
        ],
    )
    runtime._graph = FakeGraph(
        [
            _updates(act={"messages": [call]}),
            _updates(
                tools={
                    "messages": [
                        _delegate_result(
                            _artifact("d0", goal="甲", input_tokens=100, output_tokens=10)
                        ),
                        _delegate_result(
                            _artifact("d1", goal="乙", input_tokens=200, output_tokens=20)
                        ),
                    ]
                }
            ),
            _updates(
                respond={
                    "messages": [
                        AIMessage(
                            content="完成",
                            usage_metadata={
                                "input_tokens": 50,
                                "output_tokens": 10,
                                "total_tokens": 60,
                            },
                        )
                    ]
                }
            ),
        ]
    )
    events = _collect(runtime)

    started = [e for e in events if isinstance(e, SubAgentStarted)]
    finished = [e for e in events if isinstance(e, SubAgentFinished)]
    assert [e.agent_id for e in started] == ["d0", "d1"]
    assert [e.agent_id for e in finished] == ["explore:d0", "explore:d1"]
    assert [e.status for e in finished] == ["success", "success"]

    run_end = next(e for e in events if isinstance(e, RunFinished))
    assert (run_end.input_tokens, run_end.output_tokens) == (350, 40)


def test_rejected_delegate_is_not_reported_as_a_subagent_failure(tmp_path) -> None:
    """预算/审批拒绝是宿主没让它跑，不能混进 failed。"""
    runtime = _runtime(tmp_path)
    runtime._graph = FakeGraph(
        [
            _updates(act={"messages": [_delegate_call()]}),
            _updates(
                tools={
                    "messages": [
                        ToolMessage(
                            content="本任务最多委派 6 次探索，已达到上限",
                            tool_call_id="d1",
                            name=DELEGATE_EXPLORE,
                            artifact=CallArtifact(
                                tool=DELEGATE_EXPLORE,
                                ok=False,
                                rejected=True,
                                decision="rejected",
                            ).model_dump(),
                        )
                    ]
                }
            ),
        ]
    )
    events = _collect(runtime)

    finished = next(e for e in events if isinstance(e, SubAgentFinished))
    assert finished.status == "rejected"


def test_subagent_actions_are_audited_with_agent_id(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._graph = FakeGraph(
        [
            _updates(act={"messages": [_delegate_call()]}),
            _updates(tools={"messages": [_delegate_result(_artifact())]}),
        ]
    )
    _collect(runtime)

    records = read_records(runtime.audit_path)
    subagent = next(r for r in records if r.kind == "subagent")
    assert subagent.agent_id == "explore:d1"
    assert subagent.parent_call_id == "d1"
    assert subagent.task == "找到入口"
    assert subagent.ok is True
    assert (subagent.input_tokens, subagent.output_tokens) == (300, 40)

    child = next(r for r in records if r.kind == "tool_call" and r.agent_id)
    assert child.agent_id == "explore:d1"
    assert child.parent_call_id == "d1"
    assert child.tool == "file_read"
    assert child.args["path"] == "main.py"
    assert child.ok is True


def test_failed_subagent_still_finishes_the_started_event(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._graph = FakeGraph(
        [
            _updates(act={"messages": [_delegate_call()]}),
            _updates(tools={"messages": [_delegate_result(None)]}),
        ]
    )
    events = _collect(runtime)

    started = next(e for e in events if isinstance(e, SubAgentStarted))
    finished = next(e for e in events if isinstance(e, SubAgentFinished))
    assert started.agent_id == finished.agent_id == "d1"
    assert finished.status == "failed"
    assert "boom" in finished.summary
