"""多 provider 路由与 fallback。

按配置顺序尝试 provider；某个 provider 被 429 后进入冷却，后续请求跳过它。
`bind_tools` / `with_structured_output` 会分别作用到每个底层模型，保持与
ChatOpenAI 相同的调用面。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from coding_agent.concurrency import ConcurrencyGovernor, ResourceTimeout
from coding_agent.llm.ratelimit import GLOBAL_LIMITER, RateLimitCoordinator
from coding_agent.recovery import TRANSIENT, classify_error


class ProviderUnavailableError(RuntimeError):
    """所有 provider 都不可用或处于限流冷却。"""


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if code is None:
        response = getattr(exc, "response", None)
        code = getattr(response, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _is_rate_limit(exc: BaseException) -> bool:
    if _status_code(exc) == 429:
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        hint in text
        for hint in ("429", "rate limit", "rate_limit", "too many requests")
    )


def _run_id(config: Any) -> str:
    if not isinstance(config, dict):
        return ""
    configurable = config.get("configurable") or {}
    if not isinstance(configurable, dict):
        return ""
    return str(configurable.get("run_id") or configurable.get("thread_id") or "")


def _usage_of(response: Any) -> tuple[int, int, int, int]:
    """从 ChatOpenAI 响应/结构化 raw 中取 token 用量。

    返回 (input, output, cache_hit, cache_miss)。不同 provider 对 prefix cache
    的字段名不同：
    - OpenAI：`prompt_tokens_details.cached_tokens`（langchain 映射到
      `usage_metadata.input_token_details.cache_read`）；
    - DeepSeek：`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。
    没回报 cache 字段时保持 0，不能把全部输入都当成 miss —— 那会让命中率
    在没有缓存能力的 provider 上显示为 0。
    """
    if isinstance(response, dict) and "raw" in response:
        response = response.get("raw")
    usage = getattr(response, "usage_metadata", None)
    hit = miss = 0
    if isinstance(usage, dict):
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        details = usage.get("input_token_details") or {}
        if isinstance(details, dict):
            hit = int(
                details.get("cache_read", details.get("cached_tokens", 0))
                or 0
            )
        if hit and not miss:
            miss = max(0, input_tokens - hit)
        return input_tokens, output_tokens, hit, miss
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict):
        token_usage = metadata.get("token_usage") or {}
        if isinstance(token_usage, dict):
            input_tokens = int(token_usage.get("prompt_tokens", 0) or 0)
            output_tokens = int(token_usage.get("completion_tokens", 0) or 0)
            hit = int(token_usage.get("prompt_cache_hit_tokens", 0) or 0)
            details = token_usage.get("prompt_tokens_details") or {}
            if not hit and isinstance(details, dict):
                hit = int(details.get("cached_tokens", 0) or 0)
            miss = int(token_usage.get("prompt_cache_miss_tokens", 0) or 0)
            if hit and not miss:
                miss = max(0, input_tokens - hit)
            return input_tokens, output_tokens, hit, miss
    return 0, 0, 0, 0


def _retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class ProviderRouter:
    def __init__(
        self,
        providers: list[Any],
        names: list[str],
        keys: list[str],
        *,
        limiter: RateLimitCoordinator | None = None,
        fallback_on_error: bool = True,
        max_wait_s: float = 30.0,
        costs: list[tuple[float, float]] | None = None,
        quotas: list[dict[str, float]] | None = None,
        strategy: str = "cost",
        task_type: str = "",
        required_capabilities: tuple[str, ...] = (),
        capabilities: list[set[str]] | None = None,
        task_types: list[set[str]] | None = None,
        governor: ConcurrencyGovernor | None = None,
        acquire_timeout_s: float = 30.0,
        aimd_min: int = 1,
        aimd_max: int = 8,
    ) -> None:
        if not providers:
            raise ValueError("ProviderRouter 至少需要一个 provider")
        self._providers = providers
        self._names = names
        self._keys = keys
        self._limiter = limiter or GLOBAL_LIMITER
        self._fallback_on_error = fallback_on_error
        self._max_wait_s = max(0.0, max_wait_s)
        self._costs = costs or [(0.0, 0.0) for _ in providers]
        self._quotas = quotas or [{} for _ in providers]
        self._strategy = strategy if strategy in {"cost", "priority"} else "cost"
        self._task_type = task_type
        self._required_capabilities = set(required_capabilities)
        self._capabilities = capabilities or [set() for _ in providers]
        self._task_types = task_types or [set() for _ in providers]
        self._governor = governor
        self._acquire_timeout_s = max(0.0, float(acquire_timeout_s))
        self._aimd_min = max(1, int(aimd_min))
        self._aimd_max = max(self._aimd_min, int(aimd_max))
        self.provider_names = tuple(names)

    def _cost_score(self, index: int) -> float:
        input_cost, output_cost = self._costs[index]
        return float(input_cost) + float(output_cost)

    def _quota_exhausted(self, index: int) -> bool:
        quota = self._quotas[index]
        usage = self._limiter.usage(self._keys[index])
        max_tokens = float(quota.get("max_tokens", 0) or 0)
        if max_tokens > 0:
            used = usage["input_tokens"] + usage["output_tokens"]
            if used >= max_tokens:
                return True
        max_cost = float(quota.get("max_cost", 0) or 0)
        return max_cost > 0 and usage["cost_usd"] >= max_cost

    def _supports(self, index: int) -> bool:
        task_types = self._task_types[index]
        if self._task_type and task_types and self._task_type not in task_types:
            return False
        capabilities = self._capabilities[index]
        if self._required_capabilities and capabilities:
            return self._required_capabilities <= capabilities
        return True

    def _ordered_indices(self) -> list[int]:
        available = [
            index
            for index in range(len(self._providers))
            if self._supports(index)
            and self._limiter.remaining(self._keys[index]) <= 0
            and not self._quota_exhausted(index)
        ]
        if self._strategy == "cost":
            available.sort(key=lambda index: (self._cost_score(index), index))
        return available

    def _record_usage_counts(
        self,
        index: int,
        input_tokens: int,
        output_tokens: int,
        run_id: str = "",
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        if not input_tokens and not output_tokens:
            return
        input_cost, output_cost = self._costs[index]
        cost = (
            input_tokens / 1_000_000 * input_cost
            + output_tokens / 1_000_000 * output_cost
        )
        self._limiter.record_usage(
            self._keys[index],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            run_id=run_id,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
        )

    def _record_usage(
        self, index: int, response: Any, run_id: str = ""
    ) -> None:
        input_tokens, output_tokens, hit, miss = _usage_of(response)
        self._record_usage_counts(
            index, input_tokens, output_tokens, run_id, hit, miss
        )

    def _pool_for(self, index: int):
        if self._governor is None:
            return None
        return self._governor.llm_pool(self._names[index])

    def _clamp_limit(self, value: int) -> int:
        return max(self._aimd_min, min(self._aimd_max, int(value)))

    def _sync_shared_limit(self, index: int):
        """把其他进程写入的自适应并发同步到本地池。"""
        pool = self._pool_for(index)
        if pool is None:
            return None
        shared = self._limiter.concurrency_limit(self._keys[index])
        if shared > 0:
            pool.set_limit(self._clamp_limit(shared))
        return pool

    def _write_shared_limit(self, index: int, value: int) -> int:
        return self._limiter.record_concurrency(
            self._keys[index], self._clamp_limit(value)
        )

    @asynccontextmanager
    async def _call_slot(self, index: int) -> AsyncIterator[None]:
        """为一次 provider 调用占用 global + per-provider 两个槽位。"""
        if self._governor is None:
            yield
            return
        timeout = self._acquire_timeout_s or None
        # 先拿 provider 的专用槽，再拿全局槽：全局槽通常更宽，等待它时
        # 只占住本 provider，不会连带堵住其它 provider。
        pool = self._sync_shared_limit(index)
        assert pool is not None
        async with pool.lease(timeout=timeout):
            async with self._governor.llm_global.lease(timeout=timeout):
                yield

    def _aimd_success(self, index: int) -> None:
        pool = self._sync_shared_limit(index)
        if pool is None:
            return
        shared = self._write_shared_limit(index, pool.limit + 1)
        pool.set_limit(self._clamp_limit(shared))

    def _aimd_failure(self, index: int, exc: BaseException) -> None:
        pool = self._sync_shared_limit(index)
        if pool is None:
            return
        if _is_rate_limit(exc):
            shared = self._write_shared_limit(index, pool.limit // 2)
        elif classify_error(exc) == TRANSIENT:
            shared = self._write_shared_limit(index, pool.limit - 1)
        else:
            return
        pool.set_limit(self._clamp_limit(shared))

    # ---------------- LangChain 兼容面 ----------------

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> ProviderRouter:
        return ProviderRouter(
            [provider.bind_tools(tools, **kwargs) for provider in self._providers],
            self._names,
            self._keys,
            limiter=self._limiter,
            fallback_on_error=self._fallback_on_error,
            max_wait_s=self._max_wait_s,
            costs=self._costs,
            quotas=self._quotas,
            strategy=self._strategy,
            task_type=self._task_type,
            required_capabilities=tuple(self._required_capabilities),
            capabilities=self._capabilities,
            task_types=self._task_types,
            governor=self._governor,
            acquire_timeout_s=self._acquire_timeout_s,
            aimd_min=self._aimd_min,
            aimd_max=self._aimd_max,
        )

    def with_structured_output(self, schema: Any, **kwargs: Any) -> ProviderRouter:
        return ProviderRouter(
            [
                provider.with_structured_output(schema, **kwargs)
                for provider in self._providers
            ],
            self._names,
            self._keys,
            limiter=self._limiter,
            fallback_on_error=self._fallback_on_error,
            max_wait_s=self._max_wait_s,
            costs=self._costs,
            quotas=self._quotas,
            strategy=self._strategy,
            task_type=self._task_type,
            required_capabilities=tuple(self._required_capabilities),
            capabilities=self._capabilities,
            task_types=self._task_types,
            governor=self._governor,
            acquire_timeout_s=self._acquire_timeout_s,
            aimd_min=self._aimd_min,
            aimd_max=self._aimd_max,
        )

    def with_retry(self, **kwargs: Any) -> ProviderRouter:
        providers = [
            provider.with_retry(**kwargs)
            if hasattr(provider, "with_retry")
            else provider
            for provider in self._providers
        ]
        return ProviderRouter(
            providers,
            self._names,
            self._keys,
            limiter=self._limiter,
            fallback_on_error=self._fallback_on_error,
            max_wait_s=self._max_wait_s,
            costs=self._costs,
            quotas=self._quotas,
            strategy=self._strategy,
            task_type=self._task_type,
            required_capabilities=tuple(self._required_capabilities),
            capabilities=self._capabilities,
            task_types=self._task_types,
            governor=self._governor,
            acquire_timeout_s=self._acquire_timeout_s,
            aimd_min=self._aimd_min,
            aimd_max=self._aimd_max,
        )

    @property
    def model_name(self) -> str:
        first = getattr(self._providers[0], "model_name", "")
        return str(first) if first else self._names[0]

    # ---------------- 调用 ----------------

    async def ainvoke(
        self, messages: Any, config: Any = None, **kwargs: Any
    ) -> Any:
        errors: list[str] = []
        attempted = False
        for _attempt in range(2):
            for index in self._ordered_indices():
                provider = self._providers[index]
                name = self._names[index]
                key = self._keys[index]
                attempted = True
                try:
                    async with self._call_slot(index):
                        result = await provider.ainvoke(
                            messages, config, **kwargs
                        )
                    self._record_usage(index, result, _run_id(config))
                    self._limiter.record_success(key)
                    self._aimd_success(index)
                    return result
                except ResourceTimeout as exc:
                    errors.append(f"{name}: {exc}")
                except Exception as exc:  # noqa: BLE001 - 逐个 provider 回退
                    self._aimd_failure(index, exc)
                    errors.append(
                        await self._handle_failure(
                            index, key, exc, streaming_started=False
                        )
                    )
            if attempted:
                break
            # 全都在冷却：等最短的一个，然后重试一次。
            waits = [self._limiter.remaining(key) for key in self._keys]
            waits = [value for value in waits if value > 0]
            if not waits:
                break
            delay = min(min(waits), self._max_wait_s)
            if delay <= 0:
                break
            await asyncio.sleep(delay)
        raise ProviderUnavailableError(
            "所有 provider 均不可用："
            + ("；".join(errors) or "处于限流冷却或配额耗尽")
        )

    def invoke(self, messages: Any, config: Any = None, **kwargs: Any) -> Any:
        errors: list[str] = []
        for index in self._ordered_indices():
            provider = self._providers[index]
            key = self._keys[index]
            try:
                result = provider.invoke(messages, config, **kwargs)
                self._record_usage(index, result)
                self._limiter.record_success(key)
                return result
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    self._sync_handle_failure(index, key, exc)
                )
        raise ProviderUnavailableError(
            "所有 provider 均不可用："
            + ("；".join(errors) or "处于限流冷却或配额耗尽")
        )

    async def astream(self, messages: Any, config: Any = None, **kwargs: Any):
        errors: list[str] = []
        for index in self._ordered_indices():
            provider = self._providers[index]
            name = self._names[index]
            key = self._keys[index]
            emitted = False
            input_tokens = 0
            output_tokens = 0
            cache_hit = 0
            cache_miss = 0
            try:
                async with self._call_slot(index):
                    async for chunk in provider.astream(
                        messages, config, **kwargs
                    ):
                        emitted = True
                        chunk_in, chunk_out, chunk_hit, chunk_miss = _usage_of(
                            chunk
                        )
                        if chunk_in or chunk_out:
                            input_tokens, output_tokens = chunk_in, chunk_out
                            cache_hit, cache_miss = chunk_hit, chunk_miss
                        yield chunk
                self._record_usage_counts(
                    index,
                    input_tokens,
                    output_tokens,
                    _run_id(config),
                    cache_hit,
                    cache_miss,
                )
                self._limiter.record_success(key)
                self._aimd_success(index)
                return
            except ResourceTimeout as exc:
                errors.append(f"{name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                self._aimd_failure(index, exc)
                if emitted:
                    # 已经输出过内容，不能再换 provider 重放。
                    raise
                errors.append(
                    await self._handle_failure(
                        index, key, exc, streaming_started=False
                    )
                )
        raise ProviderUnavailableError(
            "所有 provider 均不可用："
            + ("；".join(errors) or "处于限流冷却或配额耗尽")
        )

    # ---------------- 错误处理 ----------------

    async def _handle_failure(
        self,
        index: int,
        key: str,
        exc: BaseException,
        *,
        streaming_started: bool,
    ) -> str:
        name = self._names[index]
        if _is_rate_limit(exc):
            delay = self._limiter.record_rate_limit(
                key, retry_after=_retry_after(exc)
            )
            return f"{name}: 429（冷却 {delay:.1f}s）"
        self._limiter.record_error(key, str(exc))
        if not self._fallback_on_error:
            raise exc
        return f"{name}: {type(exc).__name__}: {exc}"

    def _sync_handle_failure(
        self, index: int, key: str, exc: BaseException
    ) -> str:
        name = self._names[index]
        if _is_rate_limit(exc):
            delay = self._limiter.record_rate_limit(
                key, retry_after=_retry_after(exc)
            )
            return f"{name}: 429（冷却 {delay:.1f}s）"
        self._limiter.record_error(key, str(exc))
        if not self._fallback_on_error:
            raise exc
        return f"{name}: {type(exc).__name__}: {exc}"
