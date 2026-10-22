"""跨进程限流状态：两个 coordinator 共享同一个 JSON 文件。"""

from __future__ import annotations

import time

import pytest

from coding_agent.llm.ratelimit import RateLimitCoordinator


def _pair(tmp_path) -> tuple[RateLimitCoordinator, RateLimitCoordinator]:
    path = tmp_path / "rate_limits.json"
    return (
        RateLimitCoordinator(path=path),
        RateLimitCoordinator(path=path),
    )


def test_concurrency_limit_is_shared_across_processes(tmp_path) -> None:
    first, second = _pair(tmp_path)

    assert first.record_concurrency("provider-a", 2) == 2
    assert second.concurrency_limit("provider-a") == 2

    # 另一个进程进一步降并发，第一个进程也能看到。
    second.record_concurrency("provider-a", 1)
    assert first.concurrency_limit("provider-a") == 1


def test_provider_cooldown_is_visible_across_processes(tmp_path) -> None:
    first, second = _pair(tmp_path)

    first.record_rate_limit("provider-a", retry_after=5)

    assert second.remaining("provider-a") > 4
    time.sleep(0.02)
    assert first.remaining("provider-a") > 4


def test_success_clears_the_shared_cooldown(tmp_path) -> None:
    first, second = _pair(tmp_path)
    first.record_rate_limit("provider-a", retry_after=5)
    time.sleep(0.02)

    second.record_success("provider-a")

    assert first.remaining("provider-a") == 0


def test_global_block_is_shared(tmp_path) -> None:
    first, second = _pair(tmp_path)

    first.block_all(5)

    assert second.global_remaining() > 4


def test_usage_is_accumulated_across_processes(tmp_path) -> None:
    first, second = _pair(tmp_path)

    first.record_usage(
        "provider-a", input_tokens=10, output_tokens=5, cost_usd=0.01
    )
    second.record_usage(
        "provider-a", input_tokens=20, output_tokens=5, cost_usd=0.02
    )

    usage = first.usage("provider-a")
    assert usage["input_tokens"] == 30
    assert usage["output_tokens"] == 10
    assert usage["cost_usd"] == pytest.approx(0.03)


def test_start_run_can_reset_the_previous_run(tmp_path) -> None:
    """同一 thread 的第二轮 run 不能继承上一轮的用量。"""
    first, second = _pair(tmp_path)

    first.start_run("thread-1")
    first.record_usage(
        "provider-a", input_tokens=100, output_tokens=10, cost_usd=0.01,
        run_id="thread-1",
    )
    first.start_run("thread-1", reset=True)

    usage = second.run_usage("thread-1")
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cost_usd"] == 0.0
    assert usage["started_at"] > 0

    first.record_usage(
        "provider-a", input_tokens=7, output_tokens=3, cost_usd=0.001,
        run_id="thread-1",
    )
    assert first.run_usage("thread-1")["input_tokens"] == 7


def test_resume_without_reset_keeps_the_usage(tmp_path) -> None:
    first, _second = _pair(tmp_path)
    first.start_run("thread-1")
    first.record_usage(
        "provider-a", input_tokens=100, output_tokens=10, run_id="thread-1"
    )

    first.start_run("thread-1")  # resume

    assert first.run_usage("thread-1")["input_tokens"] == 100


def test_old_runs_are_pruned(tmp_path, monkeypatch) -> None:
    import coding_agent.llm.ratelimit as module

    monkeypatch.setattr(module, "MAX_RUNS", 3)
    coordinator = RateLimitCoordinator(path=tmp_path / "rate.json")
    for index in range(6):
        coordinator.start_run(f"run-{index}")

    assert len(coordinator._runs) <= 3
    assert "run-5" in coordinator._runs


def test_run_usage_is_reconciled_per_provider(tmp_path) -> None:
    first, second = _pair(tmp_path)

    first.record_usage(
        "provider-a",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.01,
        run_id="run-1",
    )
    second.record_usage(
        "provider-b",
        input_tokens=20,
        output_tokens=10,
        cost_usd=0.02,
        run_id="run-1",
    )

    usage = first.run_usage("run-1")
    assert usage["input_tokens"] == 30
    assert usage["output_tokens"] == 15
    assert usage["cost_usd"] == pytest.approx(0.03)
    assert set(usage["providers"]) == {"provider-a", "provider-b"}


def test_reset_clears_the_shared_file(tmp_path) -> None:
    first, second = _pair(tmp_path)
    first.record_rate_limit("provider-a", retry_after=5)
    first.block_all(5)
    time.sleep(0.02)

    first.reset()

    assert second.remaining("provider-a") == 0
    assert second.global_remaining() == 0
