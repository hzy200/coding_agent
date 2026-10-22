"""安全 profile 的强制覆盖与读保护。"""

from __future__ import annotations

from coding_agent.config import Settings, apply_security_profile
from coding_agent.memory.longterm import resolve_memory_root
from coding_agent.sandbox.policy import CommandLevel, classify
from coding_agent.security.read_policy import (
    is_protected_read_path,
    protected_read_reason,
)


class _HomeSandbox:
    def home(self) -> str:
        return "/home/tester"


def test_untrusted_profile_forces_hard_controls() -> None:
    raw = Settings(
        _env_file=None,
        security_profile="untrusted",
        shell_sandbox="off",
        shell_network=True,
        approval_mode="approve",
        trust_repo_instructions=True,
        trust_repo_memory=True,
        allow_rules="shell_exec:*",
    )

    settings = apply_security_profile(raw)

    assert settings.shell_sandbox == "bwrap"
    assert settings.shell_network is False
    assert settings.approval_mode == "ask"
    assert settings.trust_repo_instructions is False
    assert settings.trust_repo_memory is False
    assert settings.allow_rules == ""
    assert settings.audit_enabled is True


def test_production_profile_is_read_only_and_offline() -> None:
    raw = Settings(
        _env_file=None,
        security_profile="production",
        shell_sandbox="off",
        shell_network=True,
        approval_mode="approve",
        trust_repo_instructions=True,
        trust_repo_memory=True,
        allow_rules="shell_exec:*",
    )

    settings = apply_security_profile(raw)

    assert settings.shell_sandbox == "bwrap"
    assert settings.shell_network is False
    assert settings.approval_mode == "deny"
    assert settings.allow_rules == ""
    assert settings.trust_repo_instructions is False
    assert settings.trust_repo_memory is False


def test_production_profile_forces_runtime_read_only(tmp_path) -> None:
    from coding_agent.audit import AuditLogger
    from coding_agent.runtime import AgentRuntime

    settings = Settings(
        _env_file=None,
        security_profile="production",
        audit_dir=str(tmp_path),
        wsl_workspace="/ws",
    )
    runtime = AgentRuntime(
        settings,
        workspace="/ws",
        allow_write=True,
        approval_mode="approve",
        audit=AuditLogger(tmp_path),
    )

    assert runtime.allow_write is False
    assert runtime.policy.approval_mode == "deny"
    assert runtime.policy.allow_write is False


def test_local_profile_is_unchanged() -> None:
    raw = Settings(
        _env_file=None,
        security_profile="local",
        shell_sandbox="off",
        approval_mode="approve",
        trust_repo_instructions=True,
    )

    assert apply_security_profile(raw) is raw


def test_protected_read_paths_and_templates() -> None:
    assert protected_read_reason(".env")
    assert protected_read_reason("config/.env")
    assert protected_read_reason(".ssh/id_rsa")
    assert protected_read_reason("certs/server.pem")
    assert protected_read_reason(".env.example") is None
    assert protected_read_reason("src/app.py") is None
    assert is_protected_read_path("id_ed25519")
    # 新增的常见凭据文件
    assert is_protected_read_path(".npmrc")
    assert is_protected_read_path(".netrc")
    assert is_protected_read_path(".aws/credentials")
    assert is_protected_read_path("config/.aws/credentials")
    assert is_protected_read_path(".kube/config")
    assert is_protected_read_path("deploy/credentials.json")
    assert is_protected_read_path("infra/prod.tfstate")
    assert is_protected_read_path("keys/service-account.json")


def test_shell_reads_of_secret_files_are_escalated() -> None:
    assert classify("cat .env").level >= CommandLevel.MUTATE
    assert classify("head -n 5 config/.env.local").level >= CommandLevel.MUTATE
    assert classify("cat .env.example").level == CommandLevel.READ
    # shell 展开前的 glob 形状
    assert classify("cat .env*").level >= CommandLevel.MUTATE
    assert classify("cat *.key").level >= CommandLevel.MUTATE
    # 用配置追加的 glob 也要在 shell 路径生效
    assert (
        classify("cat config/prod.toml").level == CommandLevel.READ
    )
    assert (
        classify(
            "cat config/prod.toml", read_deny_globs="config/prod.toml"
        ).level
        >= CommandLevel.MUTATE
    )


def test_environment_dump_and_secret_searches_require_confirmation() -> None:
    assert classify("printenv").level is CommandLevel.DANGER
    assert classify("env").level is CommandLevel.DANGER
    assert classify("grep -rn password src/").level is CommandLevel.MUTATE
    assert classify("rg -n api_key .").level is CommandLevel.MUTATE
    # 不含秘密词的普通检索仍然是 L0
    assert classify("grep -rn todo src/").level is CommandLevel.READ


def test_memory_root_moves_outside_untrusted_workspace() -> None:
    trusted = Settings(_env_file=None, trust_repo_memory=True)
    untrusted = Settings(_env_file=None, trust_repo_memory=False, memory_root="")
    explicit = Settings(
        _env_file=None,
        trust_repo_memory=False,
        memory_root="/var/lib/memory",
    )

    assert resolve_memory_root(trusted, _HomeSandbox(), "/repo") == "/repo"
    assert (
        resolve_memory_root(untrusted, _HomeSandbox(), "/repo")
        == "/home/tester/.agent-memory"
    )
    assert resolve_memory_root(explicit, _HomeSandbox(), "/repo") == "/var/lib/memory"
