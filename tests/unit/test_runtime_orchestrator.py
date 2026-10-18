"""runtime 对 orchestrator 事件的翻译与开关关闭时的回归。"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage

from coding_agent.agents.policy import RepoProfile
from coding_agent.audit import AuditLogger, read_records
from coding_agent.config import Settings
from coding_agent.events import (
    OrchestrationFinished,
    RunFinished,
    TaskGraphCreated,
    TaskGraphExtended,
    WorkerFinished,
    WorkerStarted,
    WorkflowSelected,
)
from coding_agent.graph.build import estimate_recursion_limit
from coding_agent.runtime import AgentRuntime

THREAD = "t-orch"


class _NullMemory:
    def load(self) -> list[str]:
        return []


class FakeGraph:
    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = script
        self.payloads: list[Any] = []

    async def astream(self, payload, config=None, stream_mode=None):  # noqa: ANN001
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


def _worker_result() -> dict[str, Any]:
    return {
        "task_id": "t1",
        "role": "explore",
        "worker_id": "explore:t1",
        "goal": "定位实现",
        "status": "success",
        "summary": "入口在 main.py",
        "files": ["main.py"],
        "findings": [],
        "verification": {},
        "input_tokens": 100,
        "output_tokens": 10,
        "duration_ms": 50,
    }


def _collect(runtime: AgentRuntime) -> list[Any]:
    return asyncio.run(_collect_async(runtime))


async def _collect_async(runtime: AgentRuntime) -> list[Any]:
    return [event async for event in runtime.run("改功能", thread_id=THREAD)]


def test_orchestrator_events_are_translated_and_audited(tmp_path) -> None:
    runtime = _runtime(tmp_path, orchestrator_enabled=True, orchestrator_min_files=30)
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    graph = FakeGraph(
        [
            _updates(
                select_workflow={
                    "workflow": "parallel_readonly",
                    "workflow_reason": "多交付物",
                }
            ),
            _updates(
                plan_tasks={
                    "tasks": [
                        {
                            "id": "t1",
                            "role": "explore",
                            "goal": "定位实现",
                            "depends_on": [],
                        }
                    ],
                    "orchestrator_degraded": False,
                }
            ),
            _updates(
                dispatch={
                    "worker_results": [_worker_result()],
                    "task_status": {"t1": "success"},
                    "orchestrator_round": 1,
                }
            ),
            _updates(
                synthesize={
                    "orchestrator_completed": 1,
                    "orchestrator_failed": 0,
                    "orchestrator_summary": "成功 1，失败/阻塞 0",
                    "task_status": {"t1": "success"},
                }
            ),
            _updates(respond={"messages": [AIMessage(content="完成")]}),
        ]
    )
    runtime._graph = graph

    events = _collect(runtime)

    assert any(isinstance(e, WorkflowSelected) for e in events)
    assert any(isinstance(e, TaskGraphCreated) for e in events)
    assert any(isinstance(e, WorkerStarted) for e in events)
    assert any(isinstance(e, WorkerFinished) for e in events)
    assert any(isinstance(e, OrchestrationFinished) for e in events)
    assert isinstance(events[-1], RunFinished)

    kinds = [r.kind for r in read_records(runtime.audit_path)]
    assert {"workflow", "task_graph", "worker", "orchestration"} <= set(kinds)


def test_worker_tool_calls_and_merge_are_audited(tmp_path) -> None:
    runtime = _runtime(tmp_path, orchestrator_enabled=True, orchestrator_min_files=30)
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    result = _worker_result()
    result.update(
        {
            "role": "implement",
            "worker_id": "implement:t1",
            "status": "success",
            "merged": True,
            "tool_calls": [
                {
                    "call_id": "child-1",
                    "name": "file_edit",
                    "args": {"path": "app/a.py", "reason": "改"},
                    "ok": True,
                    "duration_ms": 3,
                }
            ],
        }
    )
    runtime._graph = FakeGraph(
        [
            _updates(
                select_workflow={
                    "workflow": "orchestrated_implement",
                    "workflow_reason": "多交付物",
                }
            ),
            _updates(
                plan_tasks={
                    "tasks": [
                        {
                            "id": "t1",
                            "role": "implement",
                            "goal": "改",
                            "write_scope": ["app/"],
                        }
                    ],
                    "orchestrator_degraded": False,
                }
            ),
            _updates(
                dispatch={
                    "worker_results": [result],
                    "task_status": {"t1": "success"},
                    "orchestrator_round": 1,
                }
            ),
            _updates(respond={"messages": [AIMessage(content="完成")]}),
        ]
    )

    events = _collect(runtime)
    finished = next(e for e in events if isinstance(e, WorkerFinished))
    assert finished.tool_calls == 1
    assert finished.merged is True

    records = read_records(runtime.audit_path)
    child = next(
        r for r in records if r.kind == "tool_call" and r.agent_id == "implement:t1"
    )
    assert child.task_id == "t1"
    assert child.tool == "file_edit"
    assert child.level == "L1 低风险写"
    assert child.args["path"] == "app/a.py"


def test_run_inputs_carry_orchestrator_mode_and_reset_state(tmp_path) -> None:
    runtime = _runtime(tmp_path, orchestrator_enabled=True, orchestrator_min_files=30)
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._graph = graph

    _collect(runtime)

    payload = graph.payloads[0]
    assert payload["execution_mode"] == "orchestrated"
    assert payload["tasks"] == []
    assert payload["worker_results"] == []
    assert payload["worker_context"] == ""


def test_orchestrator_disabled_stays_direct(tmp_path) -> None:
    runtime = _runtime(tmp_path, orchestrator_enabled=False)
    graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._graph = graph

    _collect(runtime)

    assert graph.payloads[0]["execution_mode"] == "direct"


def test_task_graph_extended_is_translated_and_audited(tmp_path) -> None:
    runtime = _runtime(
        tmp_path, orchestrator_enabled=True, orchestrator_min_files=30
    )
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    runtime._graph = FakeGraph(
        [
            _updates(
                extend_tasks={
                    "task_extensions_applied": [
                        {
                            "id": "ext-1",
                            "role": "review",
                            "goal": "检查边界",
                        }
                    ],
                    "task_extension_errors": ["重复任务提议已忽略：定位"],
                }
            ),
            _updates(respond={"messages": [AIMessage(content="完成")]}),
        ]
    )

    events = _collect(runtime)

    extended = [
        event for event in events if isinstance(event, TaskGraphExtended)
    ]
    assert extended
    assert extended[0].added[0]["id"] == "ext-1"
    assert extended[0].rejected
    kinds = [record.kind for record in read_records(runtime.audit_path)]
    assert "task_graph" in kinds


def test_custom_worker_events_are_not_duplicated_by_the_dispatch_update(
    tmp_path,
) -> None:
    """custom 流实时发过的事件，updates 分支不能再来一遍。"""
    result = {
        "task_id": "t1",
        "role": "explore",
        "worker_id": "explore:t1",
        "goal": "定位实现",
        "status": "success",
        "summary": "入口在 main.py",
        "input_tokens": 100,
        "output_tokens": 10,
        "duration_ms": 50,
        "attempt": 1,
    }
    runtime = _runtime(
        tmp_path, orchestrator_enabled=True, orchestrator_min_files=30
    )
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    runtime._graph = FakeGraph(
        [
            (
                "custom",
                {
                    "type": "worker_started",
                    "worker_id": "explore:t1",
                    "task_id": "t1",
                    "role": "explore",
                    "goal": "定位实现",
                    "attempt": 1,
                },
            ),
            ("custom", {"type": "worker_finished", "result": result}),
            _updates(
                dispatch={
                    "worker_results": [result],
                    "task_status": {"t1": "success"},
                    "orchestrator_round": 1,
                }
            ),
            _updates(respond={"messages": [AIMessage(content="完成")]}),
        ]
    )

    events = _collect(runtime)

    assert len([event for event in events if isinstance(event, WorkerStarted)]) == 1
    assert len([event for event in events if isinstance(event, WorkerFinished)]) == 1
    assert isinstance(events[-1], RunFinished)
    # 主 agent 的 token 与 worker token 分列，后者不会漏记
    assert events[-1].worker_input_tokens == 100
    assert events[-1].worker_output_tokens == 10


def test_estimate_recursion_limit_includes_the_orchestrator_path() -> None:
    base = estimate_recursion_limit(5, 12, 3, 2)
    orch = estimate_recursion_limit(
        5,
        12,
        3,
        2,
        orchestrator_enabled=True,
        orchestrator_max_rounds=3,
    )
    # survey/select/plan_tasks/steering_plan + 每轮
    # dispatch/extend_tasks/steering_dispatch；
    # 用户重规划会让轮次归零，按 (max_replans + 1) 乘一层。
    assert orch == base + (2 + 1) * (4 + 3 * 3)
