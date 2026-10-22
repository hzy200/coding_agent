"""调度器的可复用策略：优先级、限流退避与 token 预算。

从 `scheduler.py` 抽出来的都是**纯状态/纯计算**，不碰事件、日志或文件。
主循环仍然负责何时派发、何时 drain；这些类只回答「能不能派」「要等多久」
「还剩多少预算」。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

# 角色默认优先级（越大越先启动）。任务可用 `priority` 覆盖。
ROLE_PRIORITY = {"implement": 30, "test": 20, "review": 20, "explore": 10}

# 派发前的 token 预留估计。真实用量在 worker 返回后结算；这里只需要一个
# 保守上界，避免把预算全部押在一个大任务上。
ROLE_TOKEN_RESERVE = {
    "implement": 8_000,
    "review": 3_000,
    "explore": 2_000,
    "test": 500,
}

_RATE_LIMIT_HINTS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota",
    "overloaded",
)


def aged_priority(base: int, waited_s: float) -> int:
    """等待越久优先级越高，避免高优先级任务把低优先级任务永久饿死。

    每等 10s +1，最多 +20；加完仍低于 implement 的基础优先级（30），
    所以 aging 是防饿死，不是颠覆角色优先级。
    """
    if waited_s <= 0:
        return int(base)
    return int(base) + min(int(waited_s // 10), 20)


def task_priority(task: dict[str, Any]) -> int:
    raw = task.get("priority")
    if raw is None:
        return ROLE_PRIORITY.get(str(task.get("role", "")), 10)
    try:
        return max(0, min(int(raw), 100))
    except (TypeError, ValueError):
        return ROLE_PRIORITY.get(str(task.get("role", "")), 10)


def task_token_reserve(task: dict[str, Any]) -> int:
    raw = task.get("max_tokens")
    if raw is not None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return ROLE_TOKEN_RESERVE.get(str(task.get("role", "")), 2_000)


def is_rate_limited(record: dict[str, Any]) -> bool:
    text = (
        f"{record.get('summary', '')} {record.get('error', '')}"
    ).lower()
    return any(hint in text for hint in _RATE_LIMIT_HINTS)


def usage_tokens(record: dict[str, Any]) -> int:
    return int(record.get("input_tokens") or 0) + int(
        record.get("output_tokens") or 0
    )


class RateLimitState:
    """并发上限的自适应升降与退避截止时间。

    - 429：降一个并发槽，按指数退避设置 `not_before`，并告诉调用方 delay
      以便同步进程级冷却；
    - 连续成功：每次恢复一个槽，直到 `max_limit`。
    """

    def __init__(self, *, max_limit: int, backoff_base: float) -> None:
        self.max_limit = max(1, int(max_limit))
        self.current_limit = self.max_limit
        self.backoff_base = max(float(backoff_base), 0.01)
        self.streak = 0
        self.not_before = 0.0

    def on_result(
        self, record: dict[str, Any], *, now: float | None = None
    ) -> float | None:
        """记录一次 worker 结果；被限流时返回需要冷却的秒数。"""
        now = time.monotonic() if now is None else now
        if is_rate_limited(record):
            self.streak += 1
            self.current_limit = max(1, self.current_limit - 1)
            delay = min(
                self.backoff_base * (2 ** (self.streak - 1)), 30.0
            )
            self.not_before = now + delay
            return delay
        self.streak = 0
        if (
            record.get("status") in {"success", "partial"}
            and self.current_limit < self.max_limit
        ):
            self.current_limit += 1
        return None

    def note_global_wait(self, seconds: float, *, now: float | None = None) -> None:
        """进程级冷却也会阻塞派发；取两者的较晚值。"""
        if seconds <= 0:
            return
        now = time.monotonic() if now is None else now
        self.not_before = max(self.not_before, now + seconds)

    def can_launch(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now >= self.not_before

    def wait_remaining(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self.not_before - now)

    def snapshot(self) -> dict[str, float]:
        """可写入 checkpoint 的状态。

        `not_before` 内部用 monotonic；跨 scheduler 节点调用（甚至跨进程
        resume）必须换成墙钟，否则新进程里的 monotonic 基准没有意义。
        """
        remaining = max(0.0, self.not_before - time.monotonic())
        return {
            "current_limit": float(self.current_limit),
            "streak": float(self.streak),
            "not_before_wall": time.time() + remaining,
        }

    def restore(self, data: Mapping[str, Any] | None) -> None:
        if not isinstance(data, Mapping):
            return
        try:
            limit = int(data.get("current_limit", self.max_limit))
        except (TypeError, ValueError):
            limit = self.max_limit
        self.current_limit = max(1, min(self.max_limit, limit))
        try:
            self.streak = max(0, int(data.get("streak", 0)))
        except (TypeError, ValueError):
            self.streak = 0
        try:
            wall = float(data.get("not_before_wall", 0.0))
        except (TypeError, ValueError):
            wall = 0.0
        self.not_before = time.monotonic() + max(0.0, wall - time.time())


class TokenBudget:
    """任务级 token 预算：派发前预留、完成后按实际用量结算。

    `limit <= 0` 表示不限制；此时 `can_reserve` 恒为真，`spent` 只做统计。
    """

    def __init__(
        self, limit: int, results: Iterable[dict[str, Any]] = ()
    ) -> None:
        self.limit = max(0, int(limit or 0))
        self.spent = sum(usage_tokens(result) for result in results)
        self.reserved = 0
        self._reserved_by_task: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def can_reserve(self, amount: int) -> bool:
        if not self.enabled:
            return True
        return self.spent + self.reserved + max(0, int(amount)) <= self.limit

    def reserve(self, task_id: str, amount: int) -> bool:
        amount = max(0, int(amount))
        if not self.can_reserve(amount):
            return False
        self.reserved += amount
        self._reserved_by_task[task_id] = amount
        return True

    def release(self, task_id: str) -> int:
        amount = self._reserved_by_task.pop(task_id, 0)
        self.reserved = max(0, self.reserved - amount)
        return amount

    def settle(self, task_id: str, record: dict[str, Any]) -> None:
        """worker 返回：释放预留并累计真实用量。"""
        self.release(task_id)
        self.spent += usage_tokens(record)
