"""文件写入的内容级保护。"""

from __future__ import annotations

from coding_agent.security.write_policy import protected_write_reason


def test_env_and_keys_are_protected() -> None:
    assert protected_write_reason(".env")
    assert protected_write_reason("config/prod.key")
    assert protected_write_reason("id_rsa")
    assert protected_write_reason(".ssh/config")


def test_env_templates_are_allowed() -> None:
    assert protected_write_reason(".env.example") is None
    assert protected_write_reason(".env.sample") is None
    assert protected_write_reason(".env.template") is None


def test_git_internal_and_agent_state_are_protected() -> None:
    assert protected_write_reason(".git/config")
    assert protected_write_reason(".agent/backups/x")


def test_normal_source_file_is_allowed() -> None:
    assert protected_write_reason("app/main.py") is None


def test_extra_globs_extend_the_defaults() -> None:
    assert protected_write_reason("docs/generated.md") is None
    assert (
        protected_write_reason(
            "docs/generated.md", extra_globs="docs/generated.md"
        )
        is not None
    )
