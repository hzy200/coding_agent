"""多 provider 路由、fallback 与全局限流协调。"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_openai import ChatOpenAI

from coding_agent.config import Settings
from coding_agent.llm.deepseek import (
    MissingApiKeyError,
    ProviderConfigError,
    build_llm,
)
from coding_agent.llm.ratelimit import RateLimitCoordinator
from coding_agent.llm.router import ProviderRouter, ProviderUnavailableError


class _RateLimit(Exception):
    status_code = 429


class _Response:
    def __init__(self, content: str, usage: tuple[int, int]) -> None:
        self.content = content
        self.usage_metadata = {
            "input_tokens": usage[0],
            "output_tokens": usage[1],
        }


class _Provider:
    def __init__(
        self,
        name: str,
        *,
        error: Exception | None = None,
        text: str = "ok",
        usage: tuple[int, int] | None = None,
    ) -> None:
        self.name = name
        self.error = error
        self.text = text
        self.usage = usage
        self.tools: list[str] = []
        self.model_name = f"{name}-model"

    async def ainvoke(self, messages, config=None, **kwargs):
        if self.error is not None:
            raise self.error
        return _Response(self.text, self.usage) if self.usage else self.text

    async def astream(self, messages, config=None, **kwargs):
        if self.error is not None:
            raise self.error
        yield self.text

    def bind_tools(self, tools, **kwargs):
        self.tools = [str(tool) for tool in tools]
        return self

    def with_structured_output(self, schema, **kwargs):
        return self

    def with_retry(self, **kwargs):
        return self


def _router(*providers: _Provider) -> tuple[ProviderRouter, RateLimitCoordinator]:
    limiter = RateLimitCoordinator(base_cooldown=0.01, max_cooldown=0.05)
    router = ProviderRouter(
        list(providers),
        [provider.name for provider in providers],
        [f"key-{provider.name}" for provider in providers],
        limiter=limiter,
        max_wait_s=0.1,
    )
    return router, limiter


def test_rate_limited_provider_falls_back_to_the_next() -> None:
    router, limiter = _router(
        _Provider("primary", error=_RateLimit("429")),
        _Provider("backup", text="from backup"),
    )

    result = asyncio.run(router.ainvoke([]))

    assert result == "from backup"
    assert limiter.remaining("key-primary") > 0
    assert limiter.remaining("key-backup") == 0


def test_generic_error_falls_back_when_enabled() -> None:
    router, _ = _router(
        _Provider("primary", error=RuntimeError("boom")),
        _Provider("backup", text="ok"),
    )

    assert asyncio.run(router.ainvoke([])) == "ok"


def test_fallback_can_be_disabled() -> None:
    limiter = RateLimitCoordinator()
    router = ProviderRouter(
        [
            _Provider("primary", error=RuntimeError("boom")),
            _Provider("backup", text="ok"),
        ],
        ["primary", "backup"],
        ["k1", "k2"],
        limiter=limiter,
        fallback_on_error=False,
    )

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(router.ainvoke([]))


def test_all_blocked_waits_for_the_shortest_cooldown() -> None:
    router, limiter = _router(
        _Provider("primary", text="recovered"),
        _Provider("backup", text="backup"),
    )
    limiter.record_rate_limit("key-primary", retry_after=0.01)
    limiter.record_rate_limit("key-backup", retry_after=0.01)

    assert asyncio.run(router.ainvoke([])) == "recovered"


def test_stream_falls_back_before_first_chunk() -> None:
    router, _ = _router(
        _Provider("primary", error=_RateLimit("429")),
        _Provider("backup", text="streamed"),
    )

    async def collect() -> list[str]:
        return [chunk async for chunk in router.astream([])]

    assert asyncio.run(collect()) == ["streamed"]


def test_cost_strategy_prefers_the_cheaper_provider() -> None:
    limiter = RateLimitCoordinator()
    router = ProviderRouter(
        [_Provider("expensive", text="expensive"), _Provider("cheap", text="cheap")],
        ["expensive", "cheap"],
        ["k-expensive", "k-cheap"],
        limiter=limiter,
        costs=[(100.0, 100.0), (1.0, 1.0)],
        strategy="cost",
    )

    assert asyncio.run(router.ainvoke([])) == "cheap"


def test_priority_strategy_keeps_config_order() -> None:
    limiter = RateLimitCoordinator()
    router = ProviderRouter(
        [_Provider("first", text="first"), _Provider("cheap", text="cheap")],
        ["first", "cheap"],
        ["k-first", "k-cheap"],
        limiter=limiter,
        costs=[(100.0, 100.0), (1.0, 1.0)],
        strategy="priority",
    )

    assert asyncio.run(router.ainvoke([])) == "first"


def test_provider_quota_is_enforced_after_usage() -> None:
    limiter = RateLimitCoordinator()
    router = ProviderRouter(
        [
            _Provider("limited", text="limited", usage=(80, 20)),
            _Provider("backup", text="backup", usage=(10, 0)),
        ],
        ["limited", "backup"],
        ["k-limited", "k-backup"],
        limiter=limiter,
        quotas=[{"max_tokens": 100}, {}],
        strategy="priority",
    )

    first = asyncio.run(router.ainvoke([]))
    second = asyncio.run(router.ainvoke([]))

    assert first.content == "limited"
    assert second.content == "backup"
    usage = limiter.usage("k-limited")
    assert usage["input_tokens"] + usage["output_tokens"] == 100


def test_task_type_filters_candidates() -> None:
    limiter = RateLimitCoordinator()
    router = ProviderRouter(
        [_Provider("worker", text="worker"), _Provider("agent", text="agent")],
        ["worker", "agent"],
        ["k-worker", "k-agent"],
        limiter=limiter,
        task_type="worker",
        task_types=[{"worker"}, {"agent"}],
        strategy="priority",
    )

    assert asyncio.run(router.ainvoke([])) == "worker"


def test_required_capabilities_filter_candidates() -> None:
    limiter = RateLimitCoordinator()
    router = ProviderRouter(
        [
            _Provider("text-only", text="text"),
            _Provider("tools", text="tools"),
        ],
        ["text-only", "tools"],
        ["k-text", "k-tools"],
        limiter=limiter,
        required_capabilities=("tools",),
        capabilities=[{"chat"}, {"chat", "tools"}],
        strategy="priority",
    )

    assert asyncio.run(router.ainvoke([])) == "tools"


def test_bind_tools_is_applied_to_every_provider() -> None:
    primary = _Provider("primary")
    backup = _Provider("backup")
    router, _ = _router(primary, backup)

    router.bind_tools(["file_read", "search_code"])

    assert primary.tools == ["file_read", "search_code"]
    assert backup.tools == ["file_read", "search_code"]


def test_unavailable_error_is_explicit() -> None:
    router, limiter = _router(_Provider("primary", text="ok"))
    limiter.record_rate_limit("key-primary", retry_after=60)
    router._max_wait_s = 0  # type: ignore[attr-defined]

    with pytest.raises(ProviderUnavailableError, match="provider"):
        asyncio.run(router.ainvoke([]))


# ---------------- build_llm ----------------

def test_single_provider_keeps_chatopenai() -> None:
    settings = Settings(_env_file=None, deepseek_api_key="sk-test")
    assert isinstance(build_llm(settings, streaming=False), ChatOpenAI)


def test_multiple_providers_build_a_router() -> None:
    settings = Settings(
        _env_file=None,
        llm_providers=json.dumps(
            [
                {
                    "name": "primary",
                    "base_url": "https://primary.invalid",
                    "api_key": "k1",
                    "model": "m1",
                    "input_cost_per_1m": 2,
                    "output_cost_per_1m": 3,
                    "max_tokens": 10_000,
                },
                {
                    "name": "backup",
                    "base_url": "https://backup.invalid",
                    "api_key": "k2",
                    "model": "m2",
                    "priority": 1,
                    "input_cost_per_1m": 1,
                    "output_cost_per_1m": 1,
                },
            ]
        ),
    )
    llm = build_llm(settings, streaming=False)
    assert isinstance(llm, ProviderRouter)
    assert llm.provider_names == ("primary", "backup")
    assert llm._costs == [(2.0, 3.0), (1.0, 1.0)]  # type: ignore[attr-defined]
    assert llm._quotas[0]["max_tokens"] == 10_000  # type: ignore[attr-defined]


def test_build_filters_by_task_type_and_capability() -> None:
    settings = Settings(
        _env_file=None,
        llm_providers=json.dumps(
            [
                {
                    "name": "agent-only",
                    "api_key": "k1",
                    "model": "agent-model",
                    "task_types": ["agent"],
                    "capabilities": ["tools"],
                },
                {
                    "name": "worker-only",
                    "api_key": "k2",
                    "model": "worker-model",
                    "task_types": ["worker"],
                    "capabilities": ["tools"],
                },
            ]
        ),
    )

    worker = build_llm(settings, streaming=False, task_type="worker")
    assert worker.model_name == "worker-model"

    with pytest.raises(ProviderConfigError):
        build_llm(settings, streaming=False, task_type="missing")


def test_invalid_provider_json_is_reported() -> None:
    settings = Settings(_env_file=None, llm_providers="{not json")
    with pytest.raises(ProviderConfigError):
        build_llm(settings)


def test_missing_keys_are_reported() -> None:
    settings = Settings(_env_file=None, deepseek_api_key="")
    with pytest.raises(MissingApiKeyError):
        build_llm(settings)


def test_usage_of_extracts_prefix_cache_tokens() -> None:
    from coding_agent.llm.router import _usage_of

    class _Cached:
        usage_metadata = {
            "input_tokens": 100,
            "output_tokens": 10,
            "input_token_details": {"cache_read": 80},
        }

    assert _usage_of(_Cached()) == (100, 10, 80, 20)


def test_usage_of_keeps_zero_when_provider_has_no_cache() -> None:
    from coding_agent.llm.router import _usage_of

    class _Plain:
        usage_metadata = {"input_tokens": 100, "output_tokens": 10}

    assert _usage_of(_Plain()) == (100, 10, 0, 0)


def test_rate_limiter_accumulates_cache_tokens() -> None:
    limiter = RateLimitCoordinator()
    limiter.record_usage(
        "k",
        input_tokens=100,
        output_tokens=10,
        run_id="r",
        cache_hit_tokens=80,
        cache_miss_tokens=20,
    )
    usage = limiter.run_usage("r")
    assert usage["cache_hit_tokens"] == 80
    assert usage["cache_miss_tokens"] == 20
    assert usage["providers"]["k"]["cache_hit_tokens"] == 80
