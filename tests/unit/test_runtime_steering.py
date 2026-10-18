"""Phase 3D：runtime 的 steering API、事件翻译与暂停契约。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.types import Command

from coding_agent.audit import AuditLogger, read_records
from coding_agent.config import Settings
from coding_agent.events import (
    RunFailed,
    RunFinished,
    SteeringApplied,
    WorkflowRevised,
)
from coding_agent.runtime import AgentRuntime

THREAD = "t-steering"


class _NullMemory:
    def load(self) -> list[str]:
        return []


class FakeGraph:
    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = script
        self.payloads: list[Any] = []

    async def astream(self, payload, config=None, stream_mode=None, subgraphs=None):  # noqa: ANN001
        self.payloads.append(payload)
        for mode, data in self.script:
            yield mode, data


def _updates(**kwargs: Any) -> tuple[str, dict]:
    return ("updates", kwargs)


def _runtime(tmp_path, **overrides) -> AgentRuntime:
    settings = Settings(_env_file=None, audit_dir=str(tmp_path), **overrides)
    runtime = AgentRuntime(
        settings, workspace="/mnt/d/proj", audit=AuditLogger(tmp_path)
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    return runtime


def _collect(runtime: AgentRuntime) -> list[Any]:
    return asyncio.run(_collect_async(runtime))


async def _collect_async(runtime: AgentRuntime) -> list[Any]:
    return [event async for event in runtime.run("做事", thread_id=THREAD)]


def test_steer_audits_and_queues_a_command(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    runtime.steer(THREAD, "pause", source="test")

    records = read_records(runtime.audit_path)
    assert [record.kind for record in records] == ["steering"]
    assert records[0].source == "test"
    assert records[0].detail == "pause"
    queued = runtime.steering.peek(THREAD)
    assert len(queued) == 1
    assert queued[0].action == "pause"
    assert queued[0].source == "test"


def test_steering_update_becomes_an_event_and_closes_the_step(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._graph = FakeGraph(
        [
            _updates(
                steering_step={
                    "steering_applied": [
                        {
                            "action": "add",
                            "task_id": "user-1",
                            "detail": "已追加只读任务",
                        }
                    ],
                    "steering_cancelled": True,
                }
            ),
            _updates(respond={"messages": [AIMessage(content="已取消")]}),
        ]
    )

    events = _collect(runtime)

    applied = [event for event in events if isinstance(event, SteeringApplied)]
    assert applied and applied[0].action == "add"
    assert applied[0].task_id == "user-1"
    assert isinstance(events[-1], RunFinished)


def test_workflow_revised_is_emitted_after_task_graph_created(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._graph = FakeGraph(
        [
            _updates(
                plan_tasks={
                    "tasks": [{"id": "r1-t1", "role": "explore", "goal": "剩余"}],
                    "workflow_revised_reason": "用户要求重规划",
                }
            ),
            _updates(respond={"messages": [AIMessage(content="好了")]}),
        ]
    )

    events = _collect(runtime)

    revised = [event for event in events if isinstance(event, WorkflowRevised)]
    assert revised and revised[0].reason == "用户要求重规划"
    assert revised[0].tasks[0]["id"] == "r1-t1"


def test_steering_pause_interrupt_ends_the_stream_without_run_finished(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime._graph = FakeGraph(
        [
            (
                "updates",
                {
                    "__interrupt__": [
                        SimpleNamespace(
                            value={
                                "steering": "pause",
                                "message": "运行已暂停；/resume 继续",
                            }
                        )
                    ]
                },
            )
        ]
    )

    events = _collect(runtime)

    applied = [event for event in events if isinstance(event, SteeringApplied)]
    assert applied and applied[0].action == "pause"
    assert applied[0].detail.startswith("运行已暂停")
    assert not any(isinstance(event, RunFinished) for event in events)


def test_resume_steering_sends_the_resume_payload(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    graph = FakeGraph(
        [_updates(respond={"messages": [AIMessage(content="继续完成")]})]
    )
    runtime._graph = graph
    runtime.steering.mark_paused(THREAD)

    async def collect() -> list[Any]:
        return [event async for event in runtime.resume_steering(THREAD)]

    events = asyncio.run(collect())

    assert isinstance(events[0], SteeringApplied)
    assert events[0].action == "resume"
    assert isinstance(graph.payloads[0], Command)
    assert graph.payloads[0].resume == {"steering": "resume"}
    assert isinstance(events[-1], RunFinished)
    # 收尾会清掉暂停标记
    assert runtime.is_paused(THREAD) is False


def test_resume_steering_respects_the_resume_limit(tmp_path) -> None:
    runtime = _runtime(tmp_path, max_resumes=1)
    runtime.steering.mark_paused(THREAD)
    runtime._resume_counts[THREAD] = 1

    async def collect() -> list[Any]:
        return [event async for event in runtime.resume_steering(THREAD)]

    events = asyncio.run(collect())

    assert isinstance(events[-1], RunFailed)
    assert "上限" in events[-1].message
