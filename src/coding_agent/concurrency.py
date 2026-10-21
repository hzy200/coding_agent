"""统一并发治理。

之前 worker、WSL 命令、主 agent 工具、LLM 调用各自维护一套限制：
高层的任务优先级传不到下层，某一层饱和时上层仍在继续准入。

这里把限制收成命名资源池：

- `worker`：编排 worker；
- `tool`：主 agent 只读工具并发；
- `wsl`：所有 `wsl.exe` 进程（同步，供线程池中的沙箱调用）；
- `llm:<provider>` / `llm:global`：LLM 调用并发。

异步池用优先级堆（数字越大越先拿到槽位），所有 acquire 都要求调用方
自行传超时；同步池只做 FIFO + 超时（WSL 命令很短，优先级收益有限）。

设计约束：异步池的所有状态变更都发生在事件循环线程内，且临界区不含
`await`，因此不需要 `asyncio.Lock`。
"""

from __future__ import annotations

import asyncio
import heapq
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any


class ResourceTimeout(RuntimeError):
    """在给定时间内没有拿到资源槽位。"""

    def __init__(self, pool: str, wait_ms: int) -> None:
        super().__init__(
            f"等待 {pool} 资源槽位超时（已等待 {wait_ms}ms）"
        )
        self.pool = pool
        self.wait_ms = wait_ms


@dataclass(slots=True)
class PoolStats:
    name: str
    limit: int
    in_flight: int
    queue_depth: int
    total_acquired: int
    total_timeouts: int
    total_wait_ms: int
    max_wait_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "limit": self.limit,
            "in_flight": self.in_flight,
            "queue_depth": self.queue_depth,
            "total_acquired": self.total_acquired,
            "total_timeouts": self.total_timeouts,
            "avg_wait_ms": (
                int(self.total_wait_ms / self.total_acquired)
                if self.total_acquired
                else 0
            ),
            "max_wait_ms": self.max_wait_ms,
        }


class Lease:
    """一次已获得的资源槽位。重复 release 是幂等的。"""

    __slots__ = ("_pool", "_released", "wait_ms")

    def __init__(self, pool: AsyncLeasePool | SyncLeasePool, wait_ms: int) -> None:
        self._pool = pool
        self._released = False
        self.wait_ms = wait_ms

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._pool._release()

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    async def __aenter__(self) -> Lease:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.release()


@dataclass(order=True)
class _Waiter:
    sort_key: tuple[int, int]
    future: asyncio.Future = field(compare=False)
    granted: bool = field(default=False, compare=False)
    cancelled: bool = field(default=False, compare=False)


class AsyncLeasePool:
    """事件循环内的优先级资源池；`limit<=0` 表示不限。"""

    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.limit = max(0, int(limit or 0))
        self.in_flight = 0
        self._waiters: list[_Waiter] = []
        self._seq = 0
        self._total_acquired = 0
        self._total_timeouts = 0
        self._total_wait_ms = 0
        self._max_wait_ms = 0

    def _note_wait(self, wait_ms: int) -> None:
        self._total_acquired += 1
        self._total_wait_ms += wait_ms
        self._max_wait_ms = max(self._max_wait_ms, wait_ms)

    def _grant_waiters(self) -> None:
        while self._waiters:
            if self.limit > 0 and self.in_flight >= self.limit:
                return
            waiter = heapq.heappop(self._waiters)
            if waiter.cancelled or waiter.future.done():
                continue
            self.in_flight += 1
            waiter.granted = True
            waiter.future.set_result(None)

    async def acquire(
        self, *, priority: int = 0, timeout: float | None = None
    ) -> Lease:
        started = time.monotonic()
        if self.limit <= 0 or self.in_flight < self.limit:
            self.in_flight += 1
            wait_ms = int((time.monotonic() - started) * 1000)
            self._note_wait(wait_ms)
            return Lease(self, wait_ms)

        self._seq += 1
        waiter = _Waiter(
            sort_key=(-int(priority), self._seq),
            future=asyncio.get_running_loop().create_future(),
        )
        heapq.heappush(self._waiters, waiter)
        try:
            await asyncio.wait_for(waiter.future, timeout=timeout)
        except TimeoutError:
            waiter.cancelled = True
            if waiter.granted:
                # 授权与超时同时发生：槽位已经转给我们，必须立刻归还。
                self.in_flight = max(0, self.in_flight - 1)
                self._grant_waiters()
            wait_ms = int((time.monotonic() - started) * 1000)
            self._total_timeouts += 1
            raise ResourceTimeout(self.name, wait_ms) from None
        except asyncio.CancelledError:
            waiter.cancelled = True
            if waiter.granted:
                self.in_flight = max(0, self.in_flight - 1)
                self._grant_waiters()
            raise
        wait_ms = int((time.monotonic() - started) * 1000)
        self._note_wait(wait_ms)
        return Lease(self, wait_ms)

    def _release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self._grant_waiters()

    def set_limit(self, limit: int) -> None:
        """调整上限；调高时立即唤醒等待者，调低不抢占在飞任务。"""
        self.limit = max(0, int(limit or 0))
        self._grant_waiters()

    @asynccontextmanager
    async def lease(
        self, *, priority: int = 0, timeout: float | None = None
    ) -> AsyncIterator[Lease]:
        lease = await self.acquire(priority=priority, timeout=timeout)
        try:
            yield lease
        finally:
            lease.release()

    def snapshot(self) -> PoolStats:
        return PoolStats(
            name=self.name,
            limit=self.limit,
            in_flight=self.in_flight,
            queue_depth=len(self._waiters),
            total_acquired=self._total_acquired,
            total_timeouts=self._total_timeouts,
            total_wait_ms=self._total_wait_ms,
            max_wait_ms=self._max_wait_ms,
        )


class SyncLeasePool:
    """线程池里的资源池（WSL 命令）；`limit<=0` 表示不限。

    `reserve` 给探测/健康检查保留槽位：普通命令最多用 `limit-reserve` 个，
    保留槽只允许 `reserved=True` 的请求使用，避免长命令把探针一起堵死。
    """

    def __init__(self, name: str, limit: int, *, reserve: int = 0) -> None:
        self.name = name
        self.limit = max(0, int(limit or 0))
        # 保留槽不能把普通容量压到 0。
        self.reserve = (
            min(max(0, int(reserve or 0)), max(0, self.limit - 1))
            if self.limit > 0
            else 0
        )
        self.in_flight = 0
        self._waiters = 0
        self._total_acquired = 0
        self._total_timeouts = 0
        self._total_wait_ms = 0
        self._max_wait_ms = 0
        self._cond = threading.Condition()

    def acquire(
        self, *, timeout: float | None = None, reserved: bool = False
    ) -> Lease:
        started = time.monotonic()
        deadline = (
            started + timeout if timeout is not None else None
        )
        with self._cond:
            while True:
                capacity = self.limit - (0 if reserved else self.reserve)
                if self.limit <= 0 or self.in_flight < capacity:
                    self.in_flight += 1
                    wait_ms = int((time.monotonic() - started) * 1000)
                    self._total_acquired += 1
                    self._total_wait_ms += wait_ms
                    self._max_wait_ms = max(self._max_wait_ms, wait_ms)
                    return Lease(self, wait_ms)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        wait_ms = int((time.monotonic() - started) * 1000)
                        self._total_timeouts += 1
                        raise ResourceTimeout(self.name, wait_ms)
                    self._waiters += 1
                    try:
                        self._cond.wait(remaining)
                    finally:
                        self._waiters -= 1
                else:
                    self._waiters += 1
                    try:
                        self._cond.wait()
                    finally:
                        self._waiters -= 1

    def _release(self) -> None:
        with self._cond:
            self.in_flight = max(0, self.in_flight - 1)
            self._cond.notify_all()

    def set_limit(self, limit: int) -> None:
        """调整上限；调低不抢占在飞命令，调高/释放时唤醒等待者。"""
        with self._cond:
            self.limit = max(0, int(limit or 0))
            if self.limit > 0:
                self.reserve = min(self.reserve, max(0, self.limit - 1))
            else:
                self.reserve = 0
            self._cond.notify_all()

    @contextmanager
    def lease(
        self, *, timeout: float | None = None, reserved: bool = False
    ) -> Iterator[Lease]:
        lease = self.acquire(timeout=timeout, reserved=reserved)
        try:
            yield lease
        finally:
            lease.release()

    def snapshot(self) -> PoolStats:
        with self._cond:
            return PoolStats(
                name=self.name,
                limit=self.limit,
                in_flight=self.in_flight,
                queue_depth=self._waiters,
                total_acquired=self._total_acquired,
                total_timeouts=self._total_timeouts,
                total_wait_ms=self._total_wait_ms,
                max_wait_ms=self._max_wait_ms,
            )


class ConcurrencyGovernor:
    """一次运行共享的资源池集合。"""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        workers = int(
            getattr(settings, "concurrency_workers", 0)
            or getattr(settings, "orchestrator_max_concurrent_workers", 4)
            or 1
        )
        tools = int(
            getattr(settings, "concurrency_tools", 0)
            or getattr(settings, "max_concurrent_tools", 4)
            or 1
        )
        wsl = int(
            getattr(settings, "concurrency_wsl", 0)
            or getattr(settings, "sandbox_max_concurrent_commands", 0)
            or 0
        )
        llm_provider = int(
            getattr(settings, "concurrency_llm_per_provider", 4) or 4
        )
        llm_global = int(
            getattr(settings, "concurrency_llm_global", 6) or 6
        )
        self.worker = AsyncLeasePool("worker", workers)
        self.tool = AsyncLeasePool("tool", tools)
        self.wsl = SyncLeasePool(
            "wsl",
            wsl,
            reserve=int(
                getattr(settings, "concurrency_wsl_reserve", 1) or 0
            ),
        )
        self.llm_global = AsyncLeasePool(
            "llm:global", max(llm_global, llm_provider)
        )
        self._llm_provider_limit = max(1, llm_provider)
        self._llm_pools: dict[str, AsyncLeasePool] = {}

    def llm_pool(self, provider: str) -> AsyncLeasePool:
        pool = self._llm_pools.get(provider)
        if pool is None:
            pool = AsyncLeasePool(
                f"llm:{provider}", self._llm_provider_limit
            )
            self._llm_pools[provider] = pool
        return pool

    def snapshot(self) -> list[dict[str, Any]]:
        pools = [
            self.worker,
            self.tool,
            self.wsl,
            self.llm_global,
            *self._llm_pools.values(),
        ]
        return [pool.snapshot().as_dict() for pool in pools]
