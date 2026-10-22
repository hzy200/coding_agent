"""审计落盘前的 secret 脱敏。"""

from __future__ import annotations

from coding_agent.audit.logger import sanitize_args, truncate
from coding_agent.security.secrets import redact_secrets, secret_kinds


def test_redacts_common_tokens() -> None:
    text = (
        "sk-abcdefghijklmnopqrstuvwxyz123456 "
        "ghp_abcdefghijklmnopqrstuvwxyz "
        "AKIAABCDEFGHIJKLMNOP"
    )

    redacted = redact_secrets(text)

    assert "sk-" not in redacted
    assert "ghp_" not in redacted
    assert "AKIA" not in redacted
    assert secret_kinds(text) == [
        "aws_access_key",
        "github_token",
        "openai_key",
    ]


def test_redacts_assigned_password_but_keeps_key_name() -> None:
    redacted = redact_secrets('password = "hunter2hunter2"')

    assert "hunter2hunter2" not in redacted
    assert "password" in redacted


def test_redacts_database_url_credentials() -> None:
    text = "DATABASE_URL=postgres://app:hunter2hunter2@db.internal:5432/app"

    redacted = redact_secrets(text)

    assert "hunter2hunter2" not in redacted
    assert "postgres://app:" in redacted
    assert "@db.internal" in redacted


def test_does_not_redact_environment_references() -> None:
    """引用不是密钥：把 settings.api_key / ${VAR} 抹掉会破坏正常代码阅读。"""
    text = (
        "api_key = settings.api_key\n"
        "password = os.environ['PASSWORD']\n"
        "token = ${DB_TOKEN}\n"
    )

    assert redact_secrets(text) == text


def test_redacts_slack_and_google_tokens() -> None:
    text = "xoxb-1234567890-abcdefghij AIza" + "a" * 35

    redacted = redact_secrets(text)

    assert "xoxb-" not in redacted
    assert "AIza" not in redacted


def test_audit_args_are_redacted_and_truncated() -> None:
    args = {
        "content": 'token = "abcdefgh12345678"\n' + "x" * 1_000,
        "nested": {"api_key": "sk-abcdefghijklmnopqrstuvwxyz123456"},
    }

    sanitized = sanitize_args(args, max_chars=100)

    assert "abcdefgh12345678" not in sanitized["content"]
    assert "已截断" in sanitized["content"]
    assert "sk-abcdef" not in sanitized["nested"]["api_key"]


def test_truncate_redacts_before_cutting() -> None:
    text = "prefix " + "sk-" + "a" * 30

    assert "sk-" not in truncate(text, max_chars=10)
