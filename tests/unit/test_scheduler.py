"""事件驱动 DAG 调度器：依赖即时解锁、失败短路、动态扩展与重试。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from coding_agent.config import Settings
from coding_agent.llm.ratelimit import GLOBAL_LIMITER
from coding_agent.orchestrator import scheduler as scheduler_module
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.scheduler import make_scheduler_node
from coding_agent.steering import SteeringController


@pytest.fixture(autouse=True)
def _reset_global_limiter():
    """scheduler 的 429 会写进程级冷却；不让跨用例污染。"""
    GLOBAL_LIMITER.configure(None)
    GLOBAL_LIMITER.reset()
    yield
    GLOBAL_LIMITER.reset()


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


def test_completed_implement_is_not_relaunched_while_waiting_for_merge(
    monkeypatch,
) -> None:
    """一个 implement 完成、另一个还在跑时，前者只能等合并，不能被重排。"""
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {
            record["task_id"]: (True, "", ["snap"]) for record in records
        }

    monkeypatch.setattr(
        "coding_agent.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )
    calls: dict[str, int] = {}

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        task_id = str(task["id"])
        calls[task_id] = calls.get(task_id, 0) + 1
        await asyncio.sleep(0.01 if task_id == "a" else 0.08)
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_max_concurrent_workers=2),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state(
                [
                    _task("a", role="implement"),
                    _task("b", role="implement"),
                ]
            ),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert calls == {"a": 1, "b": 1}
    assert out["task_status"] == {"a": "success", "b": "success"}


def test_merge_backpressure_limits_concurrent_implement(monkeypatch) -> None:
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {
            record["task_id"]: (True, "", ["snap"]) for record in records
        }

    monkeypatch.setattr(
        "coding_agent.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )
    active = 0
    max_active = 0

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        nonlocal active, max_active
        if task["role"] == "implement":
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=4,
            orchestrator_max_pending_merges=1,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state(
                [
                    _task("i1", role="implement"),
                    _task("i2", role="implement"),
                ]
            ),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert max_active == 1
    assert out["task_status"] == {"i1": "success", "i2": "success"}


def test_checkpoint_boundary_does_not_cap_launches() -> None:
    """边界只控制“何时返回”，不再限制“这一边界能启动几个”。"""
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=4,
            orchestrator_checkpoint_every=2,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b"), _task("c"), _task("d")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    # 旧实现每边界最多启动 checkpoint_every 个，只会跑 2 个。
    assert len(out["worker_results"]) == 4
    assert out["scheduler_done"] is True


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


def test_total_token_budget_blocks_tasks_that_really_do_not_fit() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        result.input_tokens = 2_000
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_total_tokens=2_200,
            orchestrator_max_concurrent_workers=1,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    # 第一个 explore 预留 2000 并跑完（实际 2000）；第二个的 EWMA 估算
    # 已经是 2400，放不进剩余预算。
    assert out["task_status"]["a"] == "success"
    assert out["task_status"]["b"] == "blocked"
    assert out["token_budget_exhausted"] is True
    assert out["token_budget_limit"] == 2_200
    assert out["token_budget_spent"] == 2_000
    assert out["token_budget_reserved"] == 0


def test_observed_usage_lets_a_small_task_fit_where_static_reserve_would_not() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        result.input_tokens = 100
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_total_tokens=2_100,
            orchestrator_max_concurrent_workers=1,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    # 静态 2000 的预留会把第二个卡死；用实际用量（100 → 估算 500）后两者都能跑。
    assert out["task_status"] == {"a": "success", "b": "success"}
    assert out["token_budget_exhausted"] is False
    assert out["token_budget_spent"] == 200


def test_token_budget_is_unlimited_by_default() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["task_status"] == {"a": "success", "b": "success"}
    assert out["token_budget_exhausted"] is False


def test_rate_limit_backoff_requeues_the_task() -> None:
    attempts: dict[str, int] = {}

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        task_id = str(task["id"])
        attempts[task_id] = attempts.get(task_id, 0) + 1
        if attempts[task_id] == 1:
            result = _result(task, status="failed")
            result.summary = "429 Too Many Requests"
            return result
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_task_retries=1,
            orchestrator_rate_limit_backoff_s=0.01,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert attempts["a"] == 2
    assert out["task_status"]["a"] == "success"


def test_deadline_does_not_finish_a_worker_that_never_started(
    monkeypatch,
) -> None:
    """排队等 semaphore 的任务不能只发 finished、不发 started。"""
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scheduler_module, "get_stream_writer", lambda: events.append
    )

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        # 第一个 worker 持住唯一的并发槽，直到 deadline 把它 cancel。
        await asyncio.sleep(5)
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=1,
            orchestrator_run_deadline_s=1,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    started = {
        str(event.get("task_id"))
        for event in events
        if event.get("type") == "worker_started"
    }
    finished = {
        str(event.get("result", {}).get("task_id"))
        for event in events
        if event.get("type") == "worker_finished"
    }
    # 关键不变量：finished 必须是 started 的子集，不能出现“只结束没开始”。
    assert finished <= started
    assert started == {"a"}
    assert out["task_status"]["b"] in {"blocked", "failed"}
