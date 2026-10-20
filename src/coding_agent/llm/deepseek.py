"""DeepSeek V4 客户端。

DeepSeek 提供 OpenAI 兼容协议，因此直接复用 langchain-openai 的 ChatOpenAI，
只替换 base_url 与模型名。
"""

from __future__ import annotations

import json
import os
from typing import Any

from langchain_openai import ChatOpenAI

from coding_agent.config import Settings
from coding_agent.llm.ratelimit import GLOBAL_LIMITER
from coding_agent.llm.router import ProviderRouter


class MissingApiKeyError(RuntimeError):
    """未配置任何 provider 的 API Key。"""


class ProviderConfigError(RuntimeError):
    """AGENT_LLM_PROVIDERS 配置无法解析。"""


def with_json_output(llm: Any, schema: type, **kwargs: Any) -> Any:
    """结构化输出，走 JSON 模式而不是默认的 json_schema。

    **不要改回默认方式。** `with_structured_output(schema)` 走 OpenAI 的
    `json_schema` response_format，而 DeepSeek 端点对这个直接返回 400
    （`This response_format type is unavailable now`，v4-flash / chat / reasoner
    都一样）。而 planner 把异常静默吞掉、降级成「整条请求当作一步」——
    于是一个**完全失效的规划，看起来和正常工作一模一样**，直到有人去翻审计
    才发现每次的计划都是请求原文。

    `method="json_mode"` 走老式的 `json_object`，DeepSeek 支持。代价是 schema
    不随请求下发，**必须写进提示词**（见 prompts.py 里各处的「输出格式」一节）。
    `method="function_calling"` 也不行：v4-flash 的 thinking 模式不支持强制
    `tool_choice`。

    额外的 `kwargs` 原样透传（例如子智能体报告需要 `include_raw=True` 才能拿到
    这次结构化调用的 token 用量）。

    真实 provider 上的可用性由 `tests/integration/test_llm_structured_output.py`
    守着（标 `llm`，需要 API Key）。
    """
    return llm.with_structured_output(schema, method="json_mode", **kwargs)


def _provider_specs(settings: Settings) -> list[dict[str, Any]]:
    raw = str(settings.llm_providers or "").strip()
    if not raw:
        if not settings.deepseek_api_key:
            raise MissingApiKeyError(
                "未配置 DEEPSEEK_API_KEY 或 AGENT_LLM_PROVIDERS。"
                "请复制 .env.example 为 .env 并填入至少一个 provider。"
            )
        return [
            {
                "name": "deepseek",
                "base_url": settings.deepseek_base_url,
                "model": settings.deepseek_model,
                "api_key": settings.deepseek_api_key,
                "priority": 0,
                "input_cost_per_1m": 0.0,
                "output_cost_per_1m": 0.0,
                "max_tokens": 0,
                "max_cost": 0.0,
                "capabilities": [],
                "task_types": [],
            }
        ]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderConfigError(
            f"AGENT_LLM_PROVIDERS 不是合法 JSON：{exc}"
        ) from exc
    items = parsed if isinstance(parsed, list) else [parsed]
    specs: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        name = str(item.get("name") or f"provider-{index}")
        api_key = str(item.get("api_key") or "")
        api_key_env = str(item.get("api_key_env") or "")
        if not api_key and api_key_env:
            api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise MissingApiKeyError(
                f"provider {name} 没有 api_key/api_key_env"
            )
        specs.append(
            {
                "name": name,
                "base_url": str(
                    item.get("base_url") or settings.deepseek_base_url
                ),
                "model": str(item.get("model") or settings.deepseek_model),
                "api_key": api_key,
                "priority": int(item.get("priority", index) or 0),
                "input_cost_per_1m": float(
                    item.get("input_cost_per_1m", 0.0) or 0.0
                ),
                "output_cost_per_1m": float(
                    item.get("output_cost_per_1m", 0.0) or 0.0
                ),
                "max_tokens": float(item.get("max_tokens", 0) or 0),
                "max_cost": float(item.get("max_cost", 0) or 0),
                "capabilities": [
                    str(value)
                    for value in (item.get("capabilities") or [])
                ],
                "task_types": [
                    str(value)
                    for value in (item.get("task_types") or [])
                ],
            }
        )
    if not specs:
        raise MissingApiKeyError("AGENT_LLM_PROVIDERS 中没有启用的 provider。")
    return sorted(specs, key=lambda spec: spec["priority"])


def build_llm(
    settings: Settings,
    *,
    streaming: bool = True,
    model_override: str = "",
    task_type: str = "agent",
    required_capabilities: tuple[str, ...] = (),
) -> Any:
    GLOBAL_LIMITER.configure(settings.resolved_rate_limit_state_path)
    specs = _provider_specs(settings)
    required = set(required_capabilities)
    specs = [
        spec
        for spec in specs
        if (
            not task_type
            or not spec.get("task_types")
            or task_type in spec["task_types"]
        )
        and (
            not required
            or not spec.get("capabilities")
            or required <= set(spec["capabilities"])
        )
    ]
    if not specs:
        raise ProviderConfigError(
            f"没有满足 task_type={task_type!r} / capabilities={sorted(required)} "
            "的 provider。"
        )
    models: list[ChatOpenAI] = []
    names: list[str] = []
    keys: list[str] = []
    costs: list[tuple[float, float]] = []
    quotas: list[dict[str, float]] = []
    capabilities: list[set[str]] = []
    task_types: list[set[str]] = []
    needs_router = False
    for spec in specs:
        model = model_override or spec["model"]
        models.append(
            ChatOpenAI(
                model=model,
                api_key=spec["api_key"],
                base_url=spec["base_url"],
                temperature=settings.llm_temperature,
                timeout=settings.llm_timeout,
                max_retries=2,
                streaming=streaming,
                # 流式响应也带回 usage，运行结束才能在审计里记 token 用量
                stream_usage=True,
            )
        )
        names.append(str(spec["name"]))
        keys.append(f"{spec['name']}:{spec['base_url']}:{model}")
        costs.append(
            (
                float(spec.get("input_cost_per_1m", 0.0) or 0.0),
                float(spec.get("output_cost_per_1m", 0.0) or 0.0),
            )
        )
        quotas.append(
            {
                "max_tokens": float(spec.get("max_tokens", 0) or 0),
                "max_cost": float(spec.get("max_cost", 0) or 0),
            }
        )
        capabilities.append(set(spec.get("capabilities") or []))
        task_types.append(set(spec.get("task_types") or []))
        if any(costs[-1]) or any(quotas[-1].values()):
            needs_router = True
    if len(models) == 1 and not needs_router:
        return models[0]
    return ProviderRouter(
        models,
        names,
        keys,
        fallback_on_error=settings.llm_fallback_on_error,
        max_wait_s=settings.llm_provider_max_wait_s,
        costs=costs,
        quotas=quotas,
        strategy=settings.llm_routing_strategy,
        task_type=task_type,
        required_capabilities=required_capabilities,
        capabilities=capabilities,
        task_types=task_types,
    )
