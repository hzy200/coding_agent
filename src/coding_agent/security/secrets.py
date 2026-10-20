"""常见 secret 的识别与脱敏。

用于**审计落盘前**和错误详情：模型看到的代码/命令仍保持原样（否则会破坏
正常编辑），但日志里不应当出现可用的密钥。这是模式匹配的 best-effort
防线，不是熵检测器。
"""

from __future__ import annotations

import re

_PLACEHOLDER = "[REDACTED:{}]"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----"
            r"[\s\S]*?"
            r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----"
        ),
    ),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    (
        "github_token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    ),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
            r"\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b"
            r"\s*[:=]\s*['\"]?([^\s'\"]{8,})"
        ),
    ),
)


def _replace_assigned(match: re.Match[str]) -> str:
    # 保留字段名，方便知道“这里原本是什么”，只抹掉值。
    return f"{match.group(1)}=[REDACTED:assigned_secret]"


def redact_secrets(text: str) -> str:
    """把文本里可识别的 secret 替换成占位符。"""
    if not text:
        return text
    result = text
    for kind, pattern in _PATTERNS:
        if kind == "assigned_secret":
            result = pattern.sub(_replace_assigned, result)
        else:
            result = pattern.sub(_PLACEHOLDER.format(kind), result)
    return result


def secret_kinds(text: str) -> list[str]:
    """返回文本中命中的 secret 类型（去重、稳定排序）。"""
    if not text:
        return []
    found = {kind for kind, pattern in _PATTERNS if pattern.search(text)}
    return sorted(found)
