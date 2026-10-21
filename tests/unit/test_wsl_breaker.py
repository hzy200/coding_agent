"""WSL 健康检查与熔断：连续传输失败时 fail fast，恢复后自动闭合。"""

from __future__ import annotations

import time
from typing import Any

import pytest

from coding_agent.concurrency import SyncLeasePool
from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import (
    ExecResult,
    WslSandbox,
    WslUnavailableError,
)


def _sandbox(**overrides: Any) -> WslSandbox:
    base = {
        "persistent_shell": False,
        "shell_timeout": 5,
        "wsl_breaker_failures": 3,
        "wsl_breaker_cooldown_s": 0.05,
        "wsl_breaker_max_cooldown_s": 0.1,
        "wsl_health_check_enabled": True,
        "wsl_health_ttl_s": 0,
        "wsl_health_timeout_s": 1,
    }
    base.update(overrides)
    return WslSandbox(Settings(_env_file=None, **base))


def _ok(stdout: str = "ok") -> ExecResult:
    return ExecResult(command="x", exit_code=0, stdout=stdout, stderr="", duration_ms=1)


def _failed(stderr: str = "boom") -> ExecResult:
    return ExecResult(command="x", exit_code=1, stdout="", stderr=stderr, duration_ms=1)


def _raise(*_args: Any, **_kwargs: Any) -> ExecResult:
    raise WslUnavailableError("wsl.exe 不可用")


def test_transport_failures_open_the_breaker(monkeypatch) -> None:
    sandbox = _sandbox()
    calls = 0

    def boom(*args: Any, **kwargs: Any) -> ExecResult:
        nonlocal calls
        calls += 1
        raise WslUnavailableError("wsl.exe 不可用")

    monkeypatch.setattr(sandbox, "_exec", boom)

    for _ in range(3):
        with pytest.raises(WslUnavailableError):
            sandbox.run("true")

    assert sandbox.circuit_state() == "open"
    with pytest.raises(WslUnavailableError, match="熔断"):
        sandbox.run("true")
    # 熔断后不再启动新的 wsl.exe。
    assert calls == 3


def test_normal_command_failures_do_not_open_the_breaker(monkeypatch) -> None:
    sandbox = _sandbox()
    monkeypatch.setattr(sandbox, "_exec", lambda *a, **k: _failed("test failed"))

    for _ in range(10):
        assert sandbox.run("false").ok is False

    assert sandbox.circuit_state() == "closed"


def test_wsl_error_output_counts_as_a_transport_failure(monkeypatch) -> None:
    sandbox = _sandbox()
    monkeypatch.setattr(
        sandbox,
        "_exec",
        lambda *a, **k: _failed("WSL_E_DISTRO_NOT_FOUND"),
    )

    for _ in range(3):
        sandbox.run("true")

    assert sandbox.circuit_state() == "open"


def test_timeout_is_a_command_failure_not_a_transport_failure(monkeypatch) -> None:
    sandbox = _sandbox()
    monkeypatch.setattr(
        sandbox,
        "_exec",
        lambda *a, **k: ExecResult(
            command="x",
            exit_code=124,
            stdout="",
            stderr="",
            duration_ms=1,
            timed_out=True,
        ),
    )

    for _ in range(5):
        sandbox.run("sleep 10")

    assert sandbox.circuit_state() == "closed"


def test_half_open_probe_closes_the_breaker(monkeypatch) -> None:
    sandbox = _sandbox()
    calls = 0

    def flaky(*args: Any, **kwargs: Any) -> ExecResult:
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise WslUnavailableError("wsl down")
        return _ok()

    monkeypatch.setattr(sandbox, "_exec", flaky)
    for _ in range(3):
        with pytest.raises(WslUnavailableError):
            sandbox.run("true")
    assert sandbox.circuit_state() == "open"

    time.sleep(0.2)
    assert sandbox.run("true").ok is True
    assert sandbox.circuit_state() == "closed"


def test_health_is_cached_and_force_reexecutes(monkeypatch) -> None:
    sandbox = _sandbox(wsl_health_ttl_s=60)
    calls = 0

    def healthy(*args: Any, **kwargs: Any) -> ExecResult:
        nonlocal calls
        calls += 1
        return _ok("__AGENT_HEALTH_OK__")

    monkeypatch.setattr(sandbox, "_exec", healthy)

    assert sandbox.health() is True
    assert sandbox.health() is True
    assert calls == 1

    monkeypatch.setattr(sandbox, "_exec", _raise)
    assert sandbox.health(force=True) is False


def test_saturation_with_high_latency_lowers_the_limit() -> None:
    settings = Settings(
        _env_file=None,
        concurrency_wsl_adaptive=True,
        concurrency_wsl_min=2,
        concurrency_wsl_max=4,
        concurrency_wsl_target_ms=100,
        concurrency_wsl_latency_window=5,
    )
    pool = SyncLeasePool("wsl", 4)
    sandbox = WslSandbox(settings, slots=pool)
    leases = [pool.acquire(timeout=0.1) for _ in range(4)]
    slow = ExecResult(
        command="x", exit_code=0, stdout="", stderr="", duration_ms=1000
    )
    for _ in range(6):
        sandbox._observe_latency(slow)
    assert pool.limit == 3
    for lease in leases:
        lease.release()


def test_low_latency_recovers_toward_the_ceiling() -> None:
    settings = Settings(
        _env_file=None,
        concurrency_wsl_adaptive=True,
        concurrency_wsl_min=2,
        concurrency_wsl_max=4,
        concurrency_wsl_target_ms=100,
        concurrency_wsl_latency_window=5,
    )
    pool = SyncLeasePool("wsl", 3)
    sandbox = WslSandbox(settings, slots=pool)
    sandbox._latencies = [10] * 5
    sandbox._latency_steps = 1
    leases = [pool.acquire(timeout=0.1) for _ in range(3)]
    fast = ExecResult(
        command="x", exit_code=0, stdout="", stderr="", duration_ms=10
    )
    sandbox._observe_latency(fast)
    assert pool.limit == 4
    for lease in leases:
        lease.release()


def test_health_disabled_never_runs_a_probe(monkeypatch) -> None:
    sandbox = _sandbox(wsl_health_check_enabled=False)

    def boom(*args: Any, **kwargs: Any) -> ExecResult:
        raise AssertionError("不应该探测")

    monkeypatch.setattr(sandbox, "_exec", boom)
    assert sandbox.health(force=True) is True
