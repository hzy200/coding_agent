"""provider 账单 API 对账。

支持两种最小适配器：

- `openai_costs`：OpenAI 兼容的 `/organization/costs`；
- `json`：任意返回 JSON 的账单端点，用点路径取 `cost_path` / `token_path`。

账单密钥与 LLM 密钥分开配置；这里只发 HTTP 请求，不把响应写进模型上下文。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from coding_agent.config import Settings


@dataclass
class BillingTarget:
    provider: str
    kind: str
    url: str
    api_key: str = ""
    params: dict[str, str] = field(default_factory=dict)
    cost_path: str = ""
    token_path: str = ""
    currency: str = "USD"


@dataclass
class BillingResult:
    provider: str
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    currency: str = "USD"
    error: str = ""


def _lookup(payload: Any, path: str) -> Any:
    value = payload
    for part in [item for item in str(path or "").split(".") if item]:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return value


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_provider_specs(settings: Settings) -> list[dict[str, Any]]:
    raw = str(settings.llm_providers or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    return [item for item in items if isinstance(item, dict)]


def billing_targets(settings: Settings) -> list[BillingTarget]:
    targets: list[BillingTarget] = []
    for index, spec in enumerate(_parse_provider_specs(settings)):
        billing = spec.get("billing")
        if not isinstance(billing, dict) or billing.get("enabled", True) is False:
            continue
        kind = str(billing.get("kind") or "json")
        name = str(spec.get("name") or f"provider-{index}")
        api_key = str(billing.get("api_key") or "")
        api_key_env = str(billing.get("api_key_env") or "")
        if not api_key and api_key_env:
            api_key = os.environ.get(api_key_env, "")
        if kind == "openai_costs":
            base_url = str(
                billing.get("base_url") or "https://api.openai.com/v1"
            ).rstrip("/")
            url = f"{base_url}/organization/costs"
            params = {"bucket_width": str(billing.get("bucket_width") or "1d")}
            for key in ("organization", "project"):
                if billing.get(key):
                    params[key] = str(billing[key])
            cost_path = ""
        else:
            url = str(billing.get("url") or "")
            params = {
                str(key): str(value)
                for key, value in (billing.get("params") or {}).items()
            }
            cost_path = str(billing.get("cost_path") or "cost")
        if not url:
            continue
        targets.append(
            BillingTarget(
                provider=name,
                kind=kind,
                url=url,
                api_key=api_key,
                params=params,
                cost_path=cost_path,
                token_path=str(billing.get("token_path") or ""),
                currency=str(billing.get("currency") or "USD"),
            )
        )
    return targets


def _fetch_openai_costs(
    target: BillingTarget, start_ts: float, end_ts: float, timeout: float
) -> BillingResult:
    params = {
        **target.params,
        "start_time": str(int(start_ts)),
        "end_time": str(int(end_ts)),
        "limit": "31",
    }
    payload = _get_json(target, params, timeout)
    total = 0.0
    for bucket in payload.get("data") or []:
        if not isinstance(bucket, dict):
            continue
        for item in bucket.get("results") or []:
            if not isinstance(item, dict):
                continue
            amount = item.get("amount") or {}
            if isinstance(amount, dict):
                total += _as_float(amount.get("value"))
    return BillingResult(
        provider=target.provider,
        cost_usd=total,
        currency=target.currency,
    )


def _fetch_json(
    target: BillingTarget, start_ts: float, end_ts: float, timeout: float
) -> BillingResult:
    params = {
        **target.params,
        "start_time": str(int(start_ts)),
        "end_time": str(int(end_ts)),
    }
    payload = _get_json(target, params, timeout)
    cost = _as_float(_lookup(payload, target.cost_path))
    tokens = _lookup(payload, target.token_path) if target.token_path else None
    input_tokens = 0
    output_tokens = 0
    if isinstance(tokens, dict):
        input_tokens = int(_as_float(tokens.get("input_tokens")))
        output_tokens = int(_as_float(tokens.get("output_tokens")))
    return BillingResult(
        provider=target.provider,
        cost_usd=cost,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        currency=target.currency,
    )


def _get_json(
    target: BillingTarget, params: dict[str, str], timeout: float
) -> dict[str, Any]:
    url = target.url
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    headers = {"Accept": "application/json"}
    if target.api_key:
        headers["Authorization"] = f"Bearer {target.api_key}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - 显式配置的账单端点
        body = response.read()
    payload = json.loads(body.decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def fetch_billing(
    settings: Settings, start_ts: float, end_ts: float
) -> list[BillingResult]:
    """拉取配置中的账单端点；单个 provider 失败不影响其它 provider。"""
    if not settings.billing_enabled:
        return []
    results: list[BillingResult] = []
    timeout = max(1.0, float(settings.billing_timeout_s))
    for target in billing_targets(settings):
        if not target.api_key and target.kind == "openai_costs":
            results.append(
                BillingResult(
                    provider=target.provider,
                    error="缺少账单 API Key",
                )
            )
            continue
        try:
            if target.kind == "openai_costs":
                results.append(
                    _fetch_openai_costs(target, start_ts, end_ts, timeout)
                )
            else:
                results.append(_fetch_json(target, start_ts, end_ts, timeout))
        except Exception as exc:  # noqa: BLE001 - 对账失败不能拖垮 run
            results.append(
                BillingResult(
                    provider=target.provider,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results
