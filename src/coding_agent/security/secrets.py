"""常见 secret 的识别与脱敏。

用于**审计落盘前**、错误详情，以及 shell/search/file_read 的结果回灌前：
日志与模型上下文都不应出现可用的密钥。这是模式匹配的 best-effort 防线，
不是熵检测器；源文件编辑仍以真实内容匹配，脱敏只影响模型看到的那一份。
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
        # 只认“看起来像字面量”的值：带引号的任意 8+ 字符，或不含
        # `.` `[` `(` `$` 的 12+ 字符。这样不会把
        # `api_key = settings.api_key` / `os.environ[...]` / `${VAR}`
        # 这类**引用**误判成密钥。
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b"
            r"\s*[:=]\s*"
            r"(?:['\"]([^'\"]{8,})['\"]|([A-Za-z0-9+/=_\-]{12,}))"
        ),
    ),
    (
        "db_url",
        re.compile(
            r"(?i)\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)"
            r"://[^\s:/@]+:)([^\s@/]{3,})(@)"
        ),
    ),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)


def _replace_assigned(match: re.Match[str]) -> str:
    # 保留字段名，方便知道“这里原本是什么”，只抹掉值。
    return f"{match.group(1)}=[REDACTED:assigned_secret]"


def _replace_db_url(match: re.Match[str]) -> str:
    return f"{match.group(1)}[REDACTED:db_url]{match.group(3)}"


def redact_secrets(text: str) -> str:
    """把文本里可识别的 secret 替换成占位符。"""
    if not text:
        return text
    result = text
    for kind, pattern in _PATTERNS:
        if kind == "assigned_secret":
            result = pattern.sub(_replace_assigned, result)
        elif kind == "db_url":
            result = pattern.sub(_replace_db_url, result)
        else:
            result = pattern.sub(_PLACEHOLDER.format(kind), result)
    return result


def secret_kinds(text: str) -> list[str]:
    """返回文本中命中的 secret 类型（去重、稳定排序）。"""
    if not text:
        return []
    found = {kind for kind, pattern in _PATTERNS if pattern.search(text)}
    return sorted(found)
