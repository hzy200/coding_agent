"""错误分类与节点级重试。"""

from __future__ import annotations

import asyncio

import pytest

from coding_agent.recovery import (
    ENVIRONMENT,
    FATAL,
    SEMANTIC,
    TRANSIENT,
    classify_error,
    retry_async,
)


class _StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


def test_timeouts_and_connection_errors_are_transient() -> None:
    assert classify_error(TimeoutError("timed out")) == TRANSIENT
    assert classify_error(ConnectionError("reset")) == TRANSIENT


def test_http_429_and_5xx_are_transient() -> None:
    assert classify_error(_StatusError(429)) == TRANSIENT
    assert classify_error(_StatusError(503)) == TRANSIENT


def test_environment_errors_are_retryable_but_distinct() -> None:
    class WslUnavailableError(RuntimeError):
        pass

    assert classify_error(WslUnavailableError("wsl down")) == ENVIRONMENT


def test_context_length_is_semantic_and_value_errors_are_fatal() -> None:
    assert (
        classify_error(RuntimeError("maximum context length exceeded"))
        == SEMANTIC
    )
    assert classify_error(ValueError("bad config")) == FATAL


def test_retry_async_recovers_from_a_transient_failure() -> None:
    calls = 0
    retries: list[dict] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("timed out")
        return "ok"

    result = asyncio.run(
        retry_async(
            operation,
            attempts=3,
            base_delay=0,
            on_retry=retries.append,
            label="test",
        )
    )

    assert result == "ok"
    assert calls == 3
    assert [item["attempt"] for item in retries] == [1, 2]
    assert retries[0]["kind"] == TRANSIENT


def test_retry_async_does_not_retry_a_fatal_error() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("bad config")

    with pytest.raises(ValueError):
        asyncio.run(
            retry_async(operation, attempts=3, base_delay=0)
        )

    assert calls == 1


def test_retry_async_stops_at_the_total_wait_budget() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise TimeoutError("timed out")

    # base=0.01s，第二次退避 0.02s，但总预算只有 0.015s：第一次重试后放弃。
    with pytest.raises(TimeoutError):
        asyncio.run(
            retry_async(
                operation,
                attempts=5,
                base_delay=0.01,
                max_total_wait=0.015,
            )
        )

    assert calls == 2


def test_cancelled_error_is_never_retried() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(retry_async(operation, attempts=3, base_delay=0))

    assert calls == 1
