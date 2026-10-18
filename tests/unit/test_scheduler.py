"""事件驱动 DAG 调度器：依赖即时解锁、失败短路、动态扩展与重试。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from coding_agent.config import Settings
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.scheduler import make_scheduler_node
from coding_agent.steering import SteeringController


def _settings(**overrides: Any) -> Settings:
    base = {
        "orchestrator_max_concurrent_workers": 2,
        "orchestrator_task_retries": 0,
        "orchestrator_checkpoint_every": 20,
        "orchestrator_run_deadline_s": 0,
        "orchestrator_max_tasks": 6,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _task(task_id: str, *, deps: list[str] | None = None, role: str = "explore") -> dict[str, Any]:
    return {
        "id": task_id,
        "role": role,
        "goal": task_id,
        "inputs": [],
        "acceptance": [],
        "depends_on": deps or [],
        "depends_mode": "full",
        "write_scope": [],
    }


def _state(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "request": "做事",
        "cwd": "/ws",
        "tasks": tasks,
        "task_status": {str(task["id"]): "pending" for task in tasks},
        "task_attempts": {},
        "worker_results": [],
        "orchestrator_round": 0,
        "task_extensions_seen": 0,
    }


def _result(task: dict[str, Any], *, status: str = "success") -> WorkerResult:
    return WorkerResult(
        task_id=str(task["id"]),
        role=str(task["role"]),
        worker_id=f"{task['role']}:{task['id']}",
        goal=str(task["goal"]),
        status=status,  # type: ignore[arg-type]
        summary=status,
    )


def test_dependent_starts_as_soon_as_its_predecessor_finishes() -> None:
    timeline: list[tuple[str, str, float]] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        task_id = str(task["id"])
        timeline.append(("start", task_id, time.monotonic()))
        if task_id == "a":
            await asyncio.sleep(0.05)
        elif task_id == "c":
            await asyncio.sleep(0.25)
        timeline.append(("end", task_id, time.monotonic()))
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b", deps=["a"]), _task("c")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    starts = {task_id: at for kind, task_id, at in timeline if kind == "start"}
    ends = {task_id: at for kind, task_id, at in timeline if kind == "end"}
    # 旧实现会等 c 结束才启动 b；事件驱动下 b 应在 a 结束后立即启动。
    assert starts["b"] < ends["c"]
    assert out["task_status"] == {"a": "success", "b": "success", "c": "success"}


def test_failure_blocks_dependents_without_waiting_for_the_whole_batch() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        if task["id"] == "a":
            return _result(task, status="failed")
        await asyncio.sleep(0.05)
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=0),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b", deps=["a"]), _task("c")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["task_status"]["a"] == "failed"
    assert out["task_status"]["b"] == "blocked"
    assert out["task_status"]["c"] == "success"


def test_dynamic_readonly_proposal_is_scheduled() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        if task["id"] == "a":
            result.proposed_tasks = [
                {"role": "review", "goal": "补一轮审查", "inputs": []}
            ]
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert [r["task_id"] for r in out["worker_results"]] == ["a", "ext-1"]
    assert out["task_extensions_applied"][0]["id"] == "ext-1"


def test_retry_carries_the_previous_failure_context() -> None:
    seen: list[dict[str, Any]] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.append(dict(task))
        if len(seen) == 1:
            result = _result(task, status="failed")
            result.summary = "第一次失败"
            return result
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=1),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert len(seen) == 2
    assert seen[1]["retry_context"]["summary"] == "第一次失败"
    assert out["task_attempts"]["a"] == 2
    assert out["task_status"]["a"] == "success"


def test_checkpoint_boundary_returns_continue_for_remaining_tasks() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_checkpoint_every=2),
        steering=SteeringController(),
    )
    first = asyncio.run(
        node(
            _state([_task("a"), _task("b"), _task("c")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert first["scheduler_continue"] is True
    assert len(first["worker_results"]) == 2

    second = asyncio.run(
        node(
            {**_state([_task("a"), _task("b"), _task("c")]), **first},
            {"configurable": {"thread_id": "t"}},
        )
    )
    assert second["scheduler_done"] is True
    assert len(second["worker_results"]) == 3
