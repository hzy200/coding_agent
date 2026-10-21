"""瞬时错误分类与节点级重试。

为什么要分类
------------
「网络抖了一下」和「测试失败了」在原实现里最终都走 `RunFailed`：
前者应该退避重试，后者应该进入 repair，两者混在一起只能二选一。
这里把异常压成四类：

- `TRANSIENT`：超时、连接重置、5xx、429 —— 值得退避重试；
- `ENVIRONMENT`：WSL 不可用、EBUSY/ENOSPC 等环境抖动 —— 可以短暂重试，
  但耗尽后要如实报成「环境异常」而不是「代码错误」；
- `SEMANTIC`：上下文超长、测试/语法失败 —— 重试同一请求没有意义；
- `FATAL`：配置、认证、参数错误 —— 直接失败，不要浪费预算。

重试策略刻意保守：指数退避 + 抖动、总等待有上限、只对前两类生效。
`CancelledError` 与 `KeyboardInterrupt` 一律原样抛出。
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

TRANSIENT = "transient"
ENVIRONMENT = "environment"
SEMANTIC = "semantic"
FATAL = "fatal"

RETRYABLE = frozenset({TRANSIENT, ENVIRONMENT})

T = TypeVar("T")

# 网络层常见的异常类名（openai/httpx 的类名在不同版本间略有差异，按名字匹配
# 比硬导入更稳；缺失的依赖不该让整个模块 import 失败）。
_TRANSIENT_NAMES = (
    "timeout",
    "connection",
    "ratelimit",
    "rate_limit",
    "internalserver",
    "serviceunavailable",
    "overloaded",
    "remoteprotocol",
    "providerunavailable",
)

_TRANSIENT_HINTS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "temporarily unavailable",
    "too many requests",
    "rate limit",
    "rate_limit",
    "overloaded",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "429",
    "502",
    "503",
    "504",
)

_SEMANTIC_HINTS = (
    "context length",
    "maximum context",
    "too many tokens",
    "context_length_exceeded",
)

# 这些 OSError 属于环境瞬时问题；配置/路径类错误不重试。
_ENVIRONMENT_ERRNOS = frozenset(
    {
        errno.EAGAIN,
        errno.EBUSY,
        errno.ETXTBSY,
        errno.EINTR,
        errno.ENOMEM,
        errno.ENOSPC,
    }
)


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        raw = getattr(exc, attr, None)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    response = getattr(exc, "response", None)
    raw = getattr(response, "status_code", None)
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return None


def classify_error(exc: BaseException) -> str:
    """把异常压成四类；未知异常按 FATAL 处理（不盲目重试）。"""
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        return FATAL

    status = _status_code(exc)
    if status == 429 or (status is not None and 500 <= status < 600):
        return TRANSIENT

    name = type(exc).__name__.lower()
    text = str(exc).lower()

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TRANSIENT

    if "wslunavailable" in name:
        return ENVIRONMENT

    if isinstance(exc, OSError):
        if isinstance(
            exc, (FileNotFoundError, PermissionError, IsADirectoryError)
        ):
            return FATAL
        if exc.errno in _ENVIRONMENT_ERRNOS:
            return ENVIRONMENT
        return TRANSIENT

    if any(_hint in text for _hint in _SEMANTIC_HINTS):
        return SEMANTIC

    if any(token in name for token in _TRANSIENT_NAMES):
        return TRANSIENT
    if any(hint in text for hint in _TRANSIENT_HINTS):
        return TRANSIENT

    return FATAL


def is_retryable(exc: BaseException) -> bool:
    return classify_error(exc) in RETRYABLE


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    max_total_wait: float = 60.0,
    on_retry: Callable[[dict[str, Any]], Any] | None = None,
    label: str = "",
) -> T:
    """执行 `operation`；只对瞬时/环境错误做有界退避重试。

    `attempts` 是总尝试次数（1 = 不重试）。`on_retry` 可以同步或异步，
    收到一条结构化记录（node/label、attempt、delay、reason、kind）。
    """
    total_attempts = max(1, int(attempts))
    base = max(0.0, float(base_delay))
    cap = max(base, float(max_delay))
    total_wait_budget = max(0.0, float(max_total_wait))
    waited = 0.0

    for attempt in range(1, total_attempts + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - 分类后再决定是否重试
            kind = classify_error(exc)
            if kind not in RETRYABLE or attempt >= total_attempts:
                raise

            delay = min(base * (2 ** (attempt - 1)), cap)
            if delay > 0:
                # 抖动避免多个 worker 同时醒来再次撞上限流。
                delay += random.uniform(0.0, delay * 0.25)
            if waited + delay > total_wait_budget:
                raise

            if on_retry is not None:
                record = {
                    "label": label,
                    "attempt": attempt,
                    "attempts": total_attempts,
                    "delay_s": round(delay, 3),
                    "kind": kind,
                    "reason": f"{type(exc).__name__}: {exc}"[:300],
                }
                maybe = on_retry(record)
                if inspect.isawaitable(maybe):
                    await maybe
            if delay > 0:
                await asyncio.sleep(delay)
            waited += delay

    # 循环必然以 return 或 raise 结束；这里只满足类型检查。
    raise RuntimeError("retry_async 未执行任何尝试")
