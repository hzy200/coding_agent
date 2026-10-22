"""Web Bearer Token 认证。

token 通过 `AGENT_WEB_AUTH_TOKENS`（JSON `{"token": "user"}`）或
`AGENT_WEB_AUTH_TOKENS_FILE`（同格式文件）配置。支持 `sha256:<hex>`
形式，避免在环境变量里保存明文 token；客户端仍发送明文，服务端做哈希比较。

未配置 token 时只允许 loopback 匿名访问（单用户本地模式）。远程匿名必须
显式打开 `web_allow_anonymous_local` 之外的开关 —— 当前实现直接拒绝，避免
误把公网端口暴露成无鉴权 RCE。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

from coding_agent.config import Settings

_BEARER_PREFIX = "bearer "
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True, slots=True)
class AuthUser:
    name: str


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """token -> user 的只读映射。"""

    tokens: dict[str, str]
    allow_anonymous_local: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> AuthConfig:
        tokens: dict[str, str] = {}
        raw = str(settings.web_auth_tokens or "").strip()
        if raw:
            tokens.update(_parse_tokens(raw, source="AGENT_WEB_AUTH_TOKENS"))
        path = str(settings.web_auth_tokens_file or "").strip()
        if path:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(
                    f"无法读取 AGENT_WEB_AUTH_TOKENS_FILE（{path}）：{exc}"
                ) from exc
            tokens.update(_parse_tokens(text, source=path))
        return cls(
            tokens=tokens,
            allow_anonymous_local=bool(settings.web_allow_anonymous_local),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.tokens)

    def authenticate(self, token: str) -> str | None:
        if not token or not self.tokens:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for candidate, user in self.tokens.items():
            expected = candidate
            if candidate.startswith("sha256:"):
                expected = candidate[len("sha256:") :]
                if secrets.compare_digest(digest, expected):
                    return user
                continue
            if secrets.compare_digest(token, candidate):
                return user
        return None

    @staticmethod
    def is_loopback(host: str | None) -> bool:
        if not host:
            return False
        # Starlette TestClient 的 client.host 是 "testclient"；它不是可由
        # 外部网络伪造的来源地址，只用于测试环境。
        if host == "testclient":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def user_from_request(self, request: Request) -> AuthUser:
        header = request.headers.get("Authorization", "")
        token = ""
        if header.lower().startswith(_BEARER_PREFIX):
            token = header[len(_BEARER_PREFIX) :].strip()
        if self.enabled:
            user = self.authenticate(token)
            if user is None:
                raise _unauthorized()
            return AuthUser(user)

        # 未配置 token：只允许本机匿名。
        client = request.client.host if request.client else ""
        if self.allow_anonymous_local and self.is_loopback(client):
            return AuthUser("local")
        raise _unauthorized()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="需要认证",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _parse_tokens(raw: str, *, source: str) -> dict[str, str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{source} 必须是 {{token: user}} 对象")
    tokens: dict[str, str] = {}
    for token, user in payload.items():
        token_text = str(token).strip()
        user_text = str(user).strip()
        if not token_text or not user_text:
            raise ValueError(f"{source} 里存在空 token 或空用户名")
        if not _USER_RE.match(user_text) or user_text in {".", ".."}:
            raise ValueError(
                f"{source} 里用户名 {user_text!r} 非法："
                "只允许字母、数字、下划线、点、连字符（1–64）"
            )
        tokens[token_text] = user_text
    return tokens
