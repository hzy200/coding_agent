"""租户目录与 thread 命名空间隔离。"""

from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tenancy import TenantError, TenantManager


class _FakeSandbox:
    def __init__(self, home: str = "/home/tester") -> None:
        self._home = home
        self.commands: list[str] = []

    def home(self) -> str:
        return self._home

    def run(self, command, *, cwd=None, timeout=None, capture_limit=None):  # noqa: ANN001
        self.commands.append(command)
        return ExecResult(
            command=command, exit_code=0, stdout="", stderr="", duration_ms=1
        )


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        web_auth_tokens='{"tok": "alice"}',
        web_workspaces_root="/srv/workspaces",
        web_memory_root="/srv/memory",
        web_data_root=str(tmp_path / "host-data"),
        wsl_workspace="/srv/workspaces/alice",
    )


def test_per_user_paths_are_isolated(tmp_path) -> None:
    manager = TenantManager(_settings(tmp_path), sandbox=_FakeSandbox())

    tenant = manager.for_user("alice")

    assert tenant.multi_user is True
    assert tenant.workspace == "/srv/workspaces/alice"
    assert tenant.settings.wsl_workspace == "/srv/workspaces/alice"
    assert tenant.settings.audit_dir == str(
        tmp_path / "host-data" / "alice" / "audit"
    )
    assert tenant.settings.checkpoint_path == str(
        tmp_path / "host-data" / "alice" / "checkpoints.sqlite"
    )
    assert tenant.settings.memory_root == "/srv/memory/alice"
    assert tenant.thread_id("t1") == "alice:t1"
    assert tenant.public_thread("alice:t1") == "t1"


def test_invalid_user_is_rejected(tmp_path) -> None:
    manager = TenantManager(_settings(tmp_path), sandbox=_FakeSandbox())

    for name in ("../escape", "a/b", "", "x" * 65):
        with pytest.raises(TenantError):
            manager.for_user(name)


def test_local_mode_keeps_existing_paths(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "host-data"),
    )
    manager = TenantManager(settings, sandbox=_FakeSandbox())

    tenant = manager.for_user("local")

    assert tenant.multi_user is False
    assert tenant.settings is settings
    assert tenant.thread_id("t1") == "t1"
