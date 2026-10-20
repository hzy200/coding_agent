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
