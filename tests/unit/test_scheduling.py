"""scheduler 抽出的调度策略：限流、token 预算、合并队列。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from coding_agent.orchestrator import merge_queue as merge_queue_module
from coding_agent.orchestrator.merge_queue import MergeQueue
from coding_agent.orchestrator.scheduling import (
    RateLimitState,
    TokenBudget,
    aged_priority,
    task_priority,
    task_token_reserve,
)


def test_rate_limit_state_backs_off_and_recovers() -> None:
    state = RateLimitState(max_limit=3, backoff_base=0.5)
    limited = {"status": "failed", "summary": "429 Too Many Requests"}

    delay = state.on_result(limited, now=100.0)

    assert delay == 0.5
    assert state.current_limit == 2
    assert state.not_before == 100.5
    assert state.wait_remaining(now=100.1) == pytest.approx(0.4)

    second = state.on_result(limited, now=100.5)
    assert second == 1.0
    assert state.current_limit == 1
    # 退避时长翻倍，但并发下限是 1。
    assert state.on_result(limited, now=101.5) == 2.0
    assert state.current_limit == 1

    assert state.on_result({"status": "success"}, now=104.0) is None
    assert state.current_limit == 2


def test_global_wait_extends_the_cooldown() -> None:
    state = RateLimitState(max_limit=2, backoff_base=0.1)

    state.note_global_wait(2.0, now=10.0)

    assert state.not_before == 12.0
    assert state.can_launch(now=11.9) is False
    assert state.can_launch(now=12.0) is True


def test_token_budget_reserves_then_settles_actual_usage() -> None:
    budget = TokenBudget(100, [{"input_tokens": 40, "output_tokens": 0}])

    assert budget.spent == 40
    assert budget.reserve("t1", 60) is True
    assert budget.reserved == 60
    # 40 + 60 + 1 超过上限。
    assert budget.reserve("t2", 1) is False

    budget.settle("t1", {"input_tokens": 10, "output_tokens": 5})
    assert budget.reserved == 0
    assert budget.spent == 55
    assert budget.reserve("t2", 45) is True


def test_unlimited_token_budget_still_tracks_spending() -> None:
    budget = TokenBudget(0)

    assert budget.reserve("t1", 999_999) is True
    budget.settle("t1", {"input_tokens": 10, "output_tokens": 1})
    assert budget.spent == 11
    assert budget.enabled is False


def test_aging_raises_priority_but_does_not_beat_implement() -> None:
    assert aged_priority(10, 0) == 10
    assert aged_priority(10, 25) == 12
    assert aged_priority(10, 1000) == 30  # 封顶 +20
    # aging 后仍低于 implement 的基础优先级 30（同级封顶）。
    assert aged_priority(10, 1000) <= 30


def test_task_priority_and_reserve_can_be_overridden() -> None:
    assert task_priority({"role": "implement"}) == 30
    assert task_priority({"role": "implement", "priority": 99}) == 99
    assert task_priority({"role": "implement", "priority": "bad"}) == 30
    assert task_token_reserve({"role": "explore"}) == 2_000
    assert task_token_reserve({"role": "explore", "max_tokens": 123}) == 123


def test_merge_queue_discard_reports_cancelled() -> None:
    queue = MergeQueue()
    finalized: list[dict[str, Any]] = []
    queue.add({"task_id": "t1", "status": "success"})

    queue.discard("用户取消", finalized.append)

    assert not queue
    assert finalized[0]["status"] == "cancelled"
    assert finalized[0]["merged"] is False


def test_merge_queue_only_waits_for_in_flight_implement_workers() -> None:
    queue = MergeQueue()
    by_id = {"i1": {"role": "implement"}, "i2": {"role": "implement"}}
    status = {"i1": "success", "i2": "pending"}

    # 只有真正在跑的 implement 才阻止 flush；待启动的（可能正被合并背压）
    # 和已完成待合并的都不算。
    assert queue.has_active_implement(
        in_flight={"i1"}, status=status, by_id=by_id
    )
    assert not queue.has_active_implement(
        in_flight=set(), status=status, by_id=by_id
    )


def test_merge_queue_flush_marks_merged_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {
            record["task_id"]: (True, "", ["snap-1"]) for record in records
        }

    monkeypatch.setattr(
        merge_queue_module, "merge_implement_patches_atomic", fake_merge
    )
    queue = MergeQueue()
    finalized: list[dict[str, Any]] = []
    queue.add({"task_id": "i1", "status": "partial"})

    merged = asyncio.run(
        queue.flush(
            sandbox=object(),
            root="/ws",
            allow_overlap=False,
            finalize=finalized.append,
        )
    )

    assert merged is True
    assert finalized[0]["merged"] is True
    assert finalized[0]["snapshot_ids"] == ["snap-1"]
    assert not queue
