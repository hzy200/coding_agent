"""provider 账单 API 对账。"""

from __future__ import annotations

import json

from coding_agent.config import Settings
from coding_agent.llm import billing


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _settings(billing_spec: dict, *, enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        billing_enabled=enabled,
        llm_providers=json.dumps(
            [
                {
                    "name": "primary",
                    "api_key": "llm-key",
                    "model": "m1",
                    "billing": billing_spec,
                }
            ]
        ),
    )


def test_openai_costs_are_summed(monkeypatch) -> None:
    seen: list[str] = []

    def fake_urlopen(request, timeout=0):
        seen.append(request.full_url)
        assert request.headers["Authorization"] == "Bearer billing-key"
        return _Response(
            {
                "data": [
                    {
                        "results": [
                            {"amount": {"value": "0.12", "currency": "usd"}},
                            {"amount": {"value": 0.03, "currency": "usd"}},
                        ]
                    }
                ]
            }
        )

    monkeypatch.setattr(billing, "urlopen", fake_urlopen)
    settings = _settings(
        {
            "kind": "openai_costs",
            "base_url": "https://billing.invalid/v1",
            "api_key": "billing-key",
        }
    )

    results = billing.fetch_billing(settings, 100, 200)

    assert results[0].provider == "primary"
    assert results[0].cost_usd == 0.15
    assert "start_time=100" in seen[0]
    assert "end_time=200" in seen[0]


def test_generic_json_cost_and_tokens(monkeypatch) -> None:
    def fake_urlopen(request, timeout=0):
        return _Response(
            {
                "data": {
                    "cost": 1.25,
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                }
            }
        )

    monkeypatch.setattr(billing, "urlopen", fake_urlopen)
    settings = _settings(
        {
            "kind": "json",
            "url": "https://billing.invalid/usage",
            "api_key_env": "MISSING_KEY",
            "cost_path": "data.cost",
            "token_path": "data.usage",
        }
    )

    results = billing.fetch_billing(settings, 100, 200)

    assert results[0].cost_usd == 1.25
    assert results[0].input_tokens == 100
    assert results[0].output_tokens == 20


def test_billing_is_disabled_by_default(monkeypatch) -> None:
    def fake_urlopen(request, timeout=0):
        raise AssertionError("不应发起请求")

    monkeypatch.setattr(billing, "urlopen", fake_urlopen)
    settings = _settings(
        {"kind": "json", "url": "https://billing.invalid"}, enabled=False
    )

    assert billing.fetch_billing(settings, 100, 200) == []


def test_single_provider_failure_does_not_raise(monkeypatch) -> None:
    def fake_urlopen(request, timeout=0):
        raise OSError("network down")

    monkeypatch.setattr(billing, "urlopen", fake_urlopen)
    settings = _settings(
        {"kind": "json", "url": "https://billing.invalid/usage"}
    )

    results = billing.fetch_billing(settings, 100, 200)

    assert results[0].error
    assert "network down" in results[0].error
