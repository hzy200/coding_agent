"""统一并发治理器：池上限、超时、优先级与配额回退。"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from coding_agent.concurrency import (
    AsyncLeasePool,
    ConcurrencyGovernor,
    ResourceTimeout,
    SyncLeasePool,
)
from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import ExecResult, WslSandbox

# conftest 的 autouse 守卫会把 _exec 换成「一碰就炸」；这个用例要测槽位租约，
# 必须恢复真实的 _exec，只替换真正执行的 _exec_locked。
_REAL_EXEC = WslSandbox._exec


def test_async_pool_limits_and_times_out() -> None:
    async def scenario() -> None:
        pool = AsyncLeasePool("w", 1)
        first = await pool.acquire(timeout=0.1)
        with pytest.raises(ResourceTimeout):
            await pool.acquire(timeout=0.01)
        first.release()
        second = await pool.acquire(timeout=0.1)
        second.release()

    asyncio.run(scenario())


def test_async_pool_serves_higher_priority_first() -> None:
    async def scenario() -> list[str]:
        pool = AsyncLeasePool("w", 1)
        held = await pool.acquire()
        order: list[str] = []

        async def waiter(priority: int, name: str) -> None:
            async with pool.lease(priority=priority, timeout=1):
                order.append(name)

        low = asyncio.create_task(waiter(1, "low"))
        await asyncio.sleep(0)
        high = asyncio.create_task(waiter(10, "high"))
        await asyncio.sleep(0)
        held.release()
        await asyncio.gather(low, high)
        return order

    assert asyncio.run(scenario()) == ["high", "low"]


def test_raising_the_limit_wakes_waiters() -> None:
    async def scenario() -> bool:
        pool = AsyncLeasePool("w", 1)
        held = await pool.acquire()
        waiter = asyncio.create_task(pool.acquire(timeout=1))
        await asyncio.sleep(0)
        pool.set_limit(2)
        lease = await waiter
        lease.release()
        held.release()
        return True

    assert asyncio.run(scenario()) is True


def test_sync_pool_times_out() -> None:
    pool = SyncLeasePool("wsl", 1)
    lease = pool.acquire(timeout=0.1)
    with pytest.raises(ResourceTimeout):
        pool.acquire(timeout=0.01)
    lease.release()
    again = pool.acquire(timeout=0.1)
    again.release()


def test_reserved_slot_stays_available_for_probes() -> None:
    pool = SyncLeasePool("wsl", 2, reserve=1)
    normal = pool.acquire(timeout=0.1)

    with pytest.raises(ResourceTimeout):
        pool.acquire(timeout=0.01)

    probe = pool.acquire(timeout=0.1, reserved=True)
    probe.release()
    normal.release()


def test_reserve_cannot_starve_normal_capacity() -> None:
    pool = SyncLeasePool("wsl", 1, reserve=5)
    assert pool.reserve == 0
    lease = pool.acquire(timeout=0.1)
    lease.release()


def test_release_is_idempotent() -> None:
    pool = SyncLeasePool("wsl", 1)
    lease = pool.acquire(timeout=0.1)
    lease.release()
    lease.release()
    again = pool.acquire(timeout=0.1)
    again.release()


def test_governor_uses_legacy_limits_when_new_ones_are_unset() -> None:
    settings = Settings(
        _env_file=None,
        concurrency_workers=0,
        orchestrator_max_concurrent_workers=3,
        concurrency_wsl=0,
        sandbox_max_concurrent_commands=5,
        concurrency_tools=0,
        max_concurrent_tools=2,
        concurrency_llm_per_provider=2,
        concurrency_llm_global=4,
    )
    governor = ConcurrencyGovernor(settings)

    assert governor.worker.limit == 3
    assert governor.wsl.limit == 5
    assert governor.tool.limit == 2
    assert governor.llm_pool("deepseek").limit == 2
    assert governor.llm_global.limit == 4


def test_governor_new_limits_win() -> None:
    settings = Settings(
        _env_file=None,
        concurrency_workers=6,
        orchestrator_max_concurrent_workers=3,
        concurrency_wsl=7,
    )
    governor = ConcurrencyGovernor(settings)

    assert governor.worker.limit == 6
    assert governor.wsl.limit == 7


def test_runtimes_can_share_one_process_governor() -> None:
    """Web 多会话必须共用一套池，否则 WSL/LLM 上限会按会话数相乘。"""
    from coding_agent.runtime import AgentRuntime

    settings = Settings(_env_file=None, concurrency_wsl=3)
    governor = ConcurrencyGovernor(settings)
    first = AgentRuntime(settings, workspace="/tmp/ws-a", governor=governor)
    second = AgentRuntime(settings, workspace="/tmp/ws-b", governor=governor)

    assert first.concurrency is second.concurrency
    assert first._sandbox._slots is governor.wsl
    assert second._sandbox._slots is governor.wsl


def test_shared_governor_caps_total_wsl_concurrency(monkeypatch) -> None:
    """两个 runtime/沙箱共享 governor 时，总并发不能翻倍。"""
    settings = Settings(
        _env_file=None,
        concurrency_wsl=3,
        concurrency_wsl_adaptive=False,
        persistent_shell=False,
        wsl_workspace="/ws",
    )
    governor = ConcurrencyGovernor(settings)
    first = WslSandbox(settings, slots=governor.wsl)
    second = WslSandbox(settings, slots=governor.wsl)

    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_exec_locked(
        self, script, *, wall, command="", capture_limit=0
    ):  # noqa: ANN001
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return ExecResult(
            command=command, exit_code=0, stdout="ok", stderr="", duration_ms=50
        )

    # 替换 _exec_locked 而不是 _exec：槽位租约在 _exec 里，必须保留。
    monkeypatch.setattr(WslSandbox, "_exec", _REAL_EXEC)
    monkeypatch.setattr(WslSandbox, "_exec_locked", fake_exec_locked)

    def one(index: int) -> ExecResult:
        sandbox = first if index % 2 else second
        return sandbox.run("echo", timeout=10)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(one, range(16)))

    assert len(results) == 16
    assert all(result.ok for result in results)
    assert 1 <= peak <= governor.wsl.limit
