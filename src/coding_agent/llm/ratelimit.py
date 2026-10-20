"""跨进程限流协调。

provider router 在收到 429 时把对应 provider 置入冷却；scheduler 在 worker
报告限流时把“全局”置入短暂冷却。状态可以落到宿主侧 JSON 文件，多个
`agent` 进程共享同一份冷却，避免 A 进程刚被 429 打回、B 进程立刻又去撞。

文件访问使用平台文件锁 + 原子替换；读写失败时退化为进程内状态。
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_COOLDOWN_S = 2.0
MAX_COOLDOWN_S = 60.0


class _FileLock(AbstractContextManager["_FileLock"]):
    """跨平台排他锁；不可用时退化为 no-op（仍能工作，只是少一层并发保护）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any = None

    def __enter__(self) -> _FileLock:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "a+b")
            self._file.seek(0)
            if self._file.read(1) == b"":
                self._file.write(b"0")
                self._file.flush()
            self._file.seek(0)
            try:
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_LOCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
        except OSError:
            self._file = None
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            try:
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        finally:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


@dataclass
class ProviderState:
    blocked_until: float = 0.0
    consecutive: int = 0
    last_error: str = ""
    successes: int = 0
    updated_at: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    # provider 的 prefix cache：命中/未命中 token（provider 不回报时为 0）
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0


@dataclass
class RateLimitCoordinator:
    base_cooldown: float = DEFAULT_COOLDOWN_S
    max_cooldown: float = MAX_COOLDOWN_S
    path: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _providers: dict[str, ProviderState] = field(default_factory=dict)
    _global_until: float = 0.0
    _runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    _cache_mtime: float = -1.0
    _cache_size: int = -1

    # ---------------- 配置 / 共享状态 ----------------

    def configure(self, path: str | Path | None) -> None:
        with self._lock:
            new_path = Path(path) if path not in (None, "", ":memory:") else None
            if new_path == self.path:
                return
            self.path = new_path
            self._cache_mtime = -1.0
            self._cache_size = -1
            self._sync_from_shared_locked()

    @property
    def lock_path(self) -> Path | None:
        return self.path.with_suffix(self.path.suffix + ".lock") if self.path else None

    def _read_shared_locked(self) -> dict[str, Any] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        if stat.st_mtime == self._cache_mtime and stat.st_size == self._cache_size:
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        self._cache_mtime = stat.st_mtime
        self._cache_size = stat.st_size
        return payload if isinstance(payload, dict) else None

    def _sync_from_shared_locked(self) -> None:
        if self.path is not None:
            try:
                self.path.stat()
            except OSError:
                # 共享文件被删除（例如另一个进程 reset）：同步清空本地状态。
                self._providers.clear()
                self._global_until = 0.0
                self._cache_mtime = -1.0
                self._cache_size = -1
                return
        payload = self._read_shared_locked()
        if not payload:
            return
        try:
            self._global_until = max(
                self._global_until, float(payload.get("global_until", 0.0))
            )
        except (TypeError, ValueError):
            pass
        providers = payload.get("providers")
        if not isinstance(providers, dict):
            return
        for key, raw in providers.items():
            if not isinstance(raw, dict):
                continue
            try:
                blocked_until = float(raw.get("blocked_until", 0.0))
                updated_at = float(raw.get("updated_at", 0.0))
            except (TypeError, ValueError):
                blocked_until, updated_at = 0.0, 0.0
            state = self._providers.setdefault(str(key), ProviderState())
            if updated_at > state.updated_at:
                # 共享文件里是更新的状态：整体替换，success 才能清掉本地冷却。
                state.blocked_until = blocked_until
                state.consecutive = int(raw.get("consecutive", 0) or 0)
                state.last_error = str(raw.get("last_error", ""))[:200]
                state.successes = int(raw.get("successes", 0) or 0)
                state.input_tokens = int(raw.get("input_tokens", 0) or 0)
                state.output_tokens = int(raw.get("output_tokens", 0) or 0)
                state.cost_usd = float(raw.get("cost_usd", 0.0) or 0.0)
                state.cache_hit_tokens = int(
                    raw.get("cache_hit_tokens", 0) or 0
                )
                state.cache_miss_tokens = int(
                    raw.get("cache_miss_tokens", 0) or 0
                )
                state.updated_at = updated_at
            else:
                if blocked_until >= state.blocked_until:
                    state.blocked_until = blocked_until
                    state.last_error = str(raw.get("last_error", ""))[:200]
                state.consecutive = max(
                    state.consecutive, int(raw.get("consecutive", 0) or 0)
                )
                state.successes = max(
                    state.successes, int(raw.get("successes", 0) or 0)
                )
                state.input_tokens = max(
                    state.input_tokens, int(raw.get("input_tokens", 0) or 0)
                )
                state.output_tokens = max(
                    state.output_tokens, int(raw.get("output_tokens", 0) or 0)
                )
                state.cost_usd = max(
                    state.cost_usd, float(raw.get("cost_usd", 0.0) or 0.0)
                )
                state.cache_hit_tokens = max(
                    state.cache_hit_tokens,
                    int(raw.get("cache_hit_tokens", 0) or 0),
                )
                state.cache_miss_tokens = max(
                    state.cache_miss_tokens,
                    int(raw.get("cache_miss_tokens", 0) or 0),
                )

        runs = payload.get("runs")
        if isinstance(runs, dict):
            for run_id, raw_run in runs.items():
                if not isinstance(raw_run, dict):
                    continue
                current = self._runs.setdefault(
                    str(run_id),
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                        "providers": {},
                    },
                )
                for field_name in (
                    "input_tokens",
                    "output_tokens",
                    "cache_hit_tokens",
                    "cache_miss_tokens",
                ):
                    current[field_name] = max(
                        int(current.get(field_name, 0) or 0),
                        int(raw_run.get(field_name, 0) or 0),
                    )
                current["cost_usd"] = max(
                    float(current.get("cost_usd", 0.0) or 0.0),
                    float(raw_run.get("cost_usd", 0.0) or 0.0),
                )
                raw_started = float(raw_run.get("started_at", 0.0) or 0.0)
                current_started = float(current.get("started_at", 0.0) or 0.0)
                if raw_started and (
                    not current_started or raw_started < current_started
                ):
                    current["started_at"] = raw_started
                raw_providers = raw_run.get("providers")
                if not isinstance(raw_providers, dict):
                    continue
                providers = current.setdefault("providers", {})
                for provider_key, raw_usage in raw_providers.items():
                    if not isinstance(raw_usage, dict):
                        continue
                    provider_usage = providers.setdefault(
                        str(provider_key),
                        {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                    )
                    for field_name in (
                        "input_tokens",
                        "output_tokens",
                        "cache_hit_tokens",
                        "cache_miss_tokens",
                    ):
                        provider_usage[field_name] = max(
                            int(provider_usage.get(field_name, 0) or 0),
                            int(raw_usage.get(field_name, 0) or 0),
                        )
                    provider_usage["cost_usd"] = max(
                        float(provider_usage.get("cost_usd", 0.0) or 0.0),
                        float(raw_usage.get("cost_usd", 0.0) or 0.0),
                    )

    def _write_shared_locked(self, *, acquire_lock: bool = True) -> None:
        if self.path is None:
            return
        payload = {
            "global_until": self._global_until,
            "runs": self._runs,
            "providers": {
                key: {
                    "blocked_until": state.blocked_until,
                    "consecutive": state.consecutive,
                    "last_error": state.last_error,
                    "successes": state.successes,
                    "updated_at": state.updated_at,
                    "input_tokens": state.input_tokens,
                    "output_tokens": state.output_tokens,
                    "cost_usd": state.cost_usd,
                    "cache_hit_tokens": state.cache_hit_tokens,
                    "cache_miss_tokens": state.cache_miss_tokens,
                }
                for key, state in self._providers.items()
            },
        }
        lock_path = self.lock_path
        context = (
            _FileLock(lock_path)
            if acquire_lock and lock_path is not None
            else _NoLock()
        )
        with context:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(self.path.suffix + ".tmp")
                temp.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                os.replace(temp, self.path)
                stat = self.path.stat()
                self._cache_mtime = stat.st_mtime
                self._cache_size = stat.st_size
            except OSError:
                pass

    # ---------------- 查询 ----------------

    def remaining(self, key: str) -> float:
        now = time.time()
        with self._lock:
            self._sync_from_shared_locked()
            state = self._providers.get(key)
            return max(0.0, (state.blocked_until if state else 0.0) - now)

    def global_remaining(self) -> float:
        now = time.time()
        with self._lock:
            self._sync_from_shared_locked()
            return max(0.0, self._global_until - now)

    # ---------------- 变更 ----------------

    def usage(self, key: str) -> dict[str, float]:
        with self._lock:
            self._sync_from_shared_locked()
            state = self._providers.get(key)
            if state is None:
                return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            return {
                "input_tokens": state.input_tokens,
                "output_tokens": state.output_tokens,
                "cost_usd": state.cost_usd,
                "cache_hit_tokens": state.cache_hit_tokens,
                "cache_miss_tokens": state.cache_miss_tokens,
            }

    def start_run(self, run_id: str) -> None:
        with self._lock:
            self._sync_from_shared_locked()
            run = self._runs.setdefault(
                run_id,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 0,
                    "providers": {},
                },
            )
            run.setdefault("started_at", time.time())
            self._write_shared_locked()

    def run_usage(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            self._sync_from_shared_locked()
            raw = self._runs.get(run_id) or {}
            return {
                "input_tokens": int(raw.get("input_tokens", 0) or 0),
                "output_tokens": int(raw.get("output_tokens", 0) or 0),
                "cost_usd": float(raw.get("cost_usd", 0.0) or 0.0),
                "cache_hit_tokens": int(
                    raw.get("cache_hit_tokens", 0) or 0
                ),
                "cache_miss_tokens": int(
                    raw.get("cache_miss_tokens", 0) or 0
                ),
                "started_at": float(raw.get("started_at", 0.0) or 0.0),
                "providers": {
                    str(key): dict(value)
                    for key, value in (raw.get("providers") or {}).items()
                    if isinstance(value, dict)
                },
            }

    def record_usage(
        self,
        key: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        run_id: str = "",
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        """原子累加 provider / run 用量；多进程下不会丢增量。"""
        with self._lock:
            lock_path = self.lock_path
            context = (
                _FileLock(lock_path)
                if lock_path is not None
                else _NoLock()
            )
            with context:
                if self.path is not None:
                    # 强制重读，拿到锁之前其他进程可能已经累加过。
                    self._cache_mtime = -1.0
                    self._cache_size = -1
                self._sync_from_shared_locked()
                state = self._providers.setdefault(key, ProviderState())
                add_in = max(0, int(input_tokens))
                add_out = max(0, int(output_tokens))
                add_cost = max(0.0, float(cost_usd))
                add_hit = max(0, int(cache_hit_tokens))
                add_miss = max(0, int(cache_miss_tokens))
                state.input_tokens += add_in
                state.output_tokens += add_out
                state.cost_usd += add_cost
                state.cache_hit_tokens += add_hit
                state.cache_miss_tokens += add_miss
                state.updated_at = max(time.time(), state.updated_at + 1e-6)
                if run_id:
                    run = self._runs.setdefault(
                        run_id,
                        {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cost_usd": 0.0,
                            "cache_hit_tokens": 0,
                            "cache_miss_tokens": 0,
                            "providers": {},
                        },
                    )
                    run.setdefault("started_at", time.time())
                    run["input_tokens"] = int(run.get("input_tokens", 0)) + add_in
                    run["output_tokens"] = (
                        int(run.get("output_tokens", 0)) + add_out
                    )
                    run["cost_usd"] = float(run.get("cost_usd", 0.0)) + add_cost
                    run["cache_hit_tokens"] = (
                        int(run.get("cache_hit_tokens", 0)) + add_hit
                    )
                    run["cache_miss_tokens"] = (
                        int(run.get("cache_miss_tokens", 0)) + add_miss
                    )
                    provider_usage = run.setdefault("providers", {}).setdefault(
                        key,
                        {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cost_usd": 0.0,
                            "cache_hit_tokens": 0,
                            "cache_miss_tokens": 0,
                        },
                    )
                    provider_usage["input_tokens"] = (
                        int(provider_usage.get("input_tokens", 0)) + add_in
                    )
                    provider_usage["output_tokens"] = (
                        int(provider_usage.get("output_tokens", 0)) + add_out
                    )
                    provider_usage["cost_usd"] = (
                        float(provider_usage.get("cost_usd", 0.0)) + add_cost
                    )
                    provider_usage["cache_hit_tokens"] = (
                        int(provider_usage.get("cache_hit_tokens", 0)) + add_hit
                    )
                    provider_usage["cache_miss_tokens"] = (
                        int(provider_usage.get("cache_miss_tokens", 0)) + add_miss
                    )
                self._write_shared_locked(acquire_lock=False)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._sync_from_shared_locked()
            state = self._providers.setdefault(key, ProviderState())
            state.blocked_until = 0.0
            state.consecutive = 0
            state.last_error = ""
            state.successes += 1
            state.updated_at = max(time.time(), state.updated_at + 1e-6)
            self._write_shared_locked()

    def record_rate_limit(
        self, key: str, *, retry_after: float | None = None
    ) -> float:
        with self._lock:
            self._sync_from_shared_locked()
            state = self._providers.setdefault(key, ProviderState())
            state.consecutive += 1
            delay = retry_after
            if delay is None:
                delay = self.base_cooldown * (2 ** (state.consecutive - 1))
            delay = max(0.01, min(float(delay), self.max_cooldown))
            state.blocked_until = time.time() + delay
            state.last_error = "rate limited"
            state.updated_at = max(time.time(), state.updated_at + 1e-6)
            self._write_shared_locked()
            return delay

    def record_error(self, key: str, message: str = "") -> None:
        with self._lock:
            self._sync_from_shared_locked()
            state = self._providers.setdefault(key, ProviderState())
            state.last_error = message[:200]
            state.updated_at = max(time.time(), state.updated_at + 1e-6)
            self._write_shared_locked()

    def block_all(self, seconds: float) -> None:
        delay = max(0.01, min(float(seconds), self.max_cooldown))
        with self._lock:
            self._sync_from_shared_locked()
            self._global_until = max(
                self._global_until, time.time() + delay
            )
            self._write_shared_locked()

    def reset(self) -> None:
        with self._lock:
            self._providers.clear()
            self._runs.clear()
            self._global_until = 0.0
            self._cache_mtime = -1.0
            self._cache_size = -1
            if self.path is not None:
                try:
                    self.path.unlink()
                except OSError:
                    pass


class _NoLock(AbstractContextManager["_NoLock"]):
    def __enter__(self) -> _NoLock:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


GLOBAL_LIMITER = RateLimitCoordinator()
