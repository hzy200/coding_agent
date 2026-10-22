"""Bearer Token 认证与匿名边界。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.events import ApprovalRequested, RunFinished, RunStarted
from coding_agent.tenancy import WebTenant
from coding_agent.web import app as web_app
from coding_agent.web.auth import AuthConfig


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "web-data"),
        **overrides,
    )


def test_token_config_from_json_and_file(tmp_path) -> None:
    inline = AuthConfig.from_settings(
        _settings(tmp_path, web_auth_tokens='{"tok-a": "alice"}')
    )
    assert inline.authenticate("tok-a") == "alice"
    assert inline.authenticate("bad") is None

    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"tok-b": "bob"}), encoding="utf-8")
    from_file = AuthConfig.from_settings(
        _settings(tmp_path, web_auth_tokens_file=str(token_file))
    )
    assert from_file.authenticate("tok-b") == "bob"


def test_hashed_token_is_supported(tmp_path) -> None:
    digest = hashlib.sha256(b"secret-token").hexdigest()
    config = AuthConfig.from_settings(
        _settings(tmp_path, web_auth_tokens=json.dumps({f"sha256:{digest}": "alice"}))
    )

    assert config.authenticate("secret-token") == "alice"
    assert config.authenticate("wrong") is None


def test_bad_token_json_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="不是合法 JSON"):
        AuthConfig.from_settings(
            _settings(tmp_path, web_auth_tokens="{not-json")
        )


def _client_with_tenant(monkeypatch, settings: Settings, config: AuthConfig):
    tenant = WebTenant(
        user="alice",
        multi_user=True,
        workspace="/tmp/ws/alice",
        settings=settings,
    )
    monkeypatch.setattr(
        web_app.TenantManager, "for_user", lambda self, _user: tenant
    )
    return TestClient(web_app.create_app(settings, config))


def test_api_requires_a_token(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _client_with_tenant(
        monkeypatch,
        settings,
        AuthConfig(tokens={"tok-a": "alice"}, allow_anonymous_local=False),
    )

    assert client.get("/api/workspace").status_code == 401
    assert (
        client.get(
            "/api/workspace", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )

    ok = client.get(
        "/api/workspace", headers={"Authorization": "Bearer tok-a"}
    )
    assert ok.status_code == 200
    assert ok.json() == {"user": "alice", "workspace": "/tmp/ws/alice"}


def test_healthz_is_public(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _client_with_tenant(
        monkeypatch,
        settings,
        AuthConfig(tokens={"tok-a": "alice"}, allow_anonymous_local=False),
    )

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_loopback_anonymous_is_allowed(tmp_path) -> None:
    config = AuthConfig(tokens={}, allow_anonymous_local=True)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

    assert config.user_from_request(request).name == "local"


def test_remote_anonymous_is_rejected(tmp_path) -> None:
    config = AuthConfig(tokens={}, allow_anonymous_local=True)
    request = SimpleNamespace(
        headers={}, client=SimpleNamespace(host="203.0.113.10")
    )

    with pytest.raises(Exception) as excinfo:
        config.user_from_request(request)

    assert getattr(excinfo.value, "status_code", None) == 401


class _SuspendingRuntime:
    """第一个用户挂起后，另一个用户不能通过同一个 public thread 看到它。"""

    def __init__(self, *_args, **_kwargs) -> None:
        self._stream_active = False

    async def run(self, prompt: str, *, thread_id: str):
        yield RunStarted(thread_id=thread_id)
        yield ApprovalRequested(request_id="c1", tool="shell_exec", command="ls")

    async def resume(self, thread_id: str, decisions):
        yield RunFinished(thread_id=thread_id, answer="done")

    def is_paused(self, _thread_id: str) -> bool:
        return False

    async def aclose(self) -> None:
        return None


def test_threads_are_isolated_between_users(monkeypatch, tmp_path) -> None:
    settings = _settings(
        tmp_path,
        web_auth_tokens=json.dumps({"tok-a": "alice", "tok-b": "bob"}),
    )

    def _tenant_for(self, user):  # noqa: ANN001
        return WebTenant(
            user=user,
            multi_user=True,
            workspace=f"/tmp/ws/{user}",
            settings=settings,
        )

    monkeypatch.setattr(web_app.TenantManager, "for_user", _tenant_for)
    monkeypatch.setattr(web_app, "AgentRuntime", _SuspendingRuntime)
    client = TestClient(
        web_app.create_app(
            settings,
            AuthConfig(
                tokens={"tok-a": "alice", "tok-b": "bob"},
                allow_anonymous_local=False,
            ),
        )
    )
    alice = {"Authorization": "Bearer tok-a"}
    bob = {"Authorization": "Bearer tok-b"}

    response = client.post(
        "/api/run",
        json={"prompt": "hi", "thread_id": "shared"},
        headers=alice,
    )
    assert response.status_code == 200
    assert "approval_requested" in response.text

    assert client.get("/api/status/shared", headers=alice).json()["exists"] is True
    assert client.get("/api/status/shared", headers=bob).json()["exists"] is False
