"""集成测试：真实调用 WSL2 沙箱。没有可用发行版时自动跳过。"""

from __future__ import annotations

import pytest

from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace

pytestmark = pytest.mark.wsl


def test_home_is_absolute(require_wsl: WslSandbox) -> None:
    assert require_wsl.home().startswith("/")


def test_resolve_workspace_prefers_explicit_config(require_wsl: WslSandbox) -> None:
    settings = require_wsl.settings
    assert resolve_workspace(settings, require_wsl) == f"{require_wsl.home()}/agent-ws"

    configured = settings.model_copy(update={"wsl_workspace": "/srv/custom"})
    assert resolve_workspace(configured, require_wsl) == "/srv/custom"


def test_echo_roundtrip(require_wsl: WslSandbox) -> None:
    result = require_wsl.run("echo hello-sandbox")
    assert result.ok
    assert "hello-sandbox" in result.stdout


def test_exit_code_propagates(require_wsl: WslSandbox) -> None:
    result = require_wsl.run("exit 3")
    assert not result.ok
    assert result.exit_code == 3


def test_stderr_is_captured(require_wsl: WslSandbox) -> None:
    result = require_wsl.run("echo boom >&2; exit 1")
    assert result.exit_code == 1
    assert "boom" in result.stderr


def test_cwd_is_applied(require_wsl: WslSandbox) -> None:
    result = require_wsl.run("pwd", cwd="/tmp")
    assert result.ok
    assert result.stdout.strip() == "/tmp"


def test_timeout_kills_command(require_wsl: WslSandbox) -> None:
    # 先预热，避免发行版冷启动耗时被算进超时
    require_wsl.run("true", timeout=60)
    result = require_wsl.run("sleep 30", timeout=2)
    assert result.timed_out
    assert not result.ok


def test_awkward_quoting_survives(require_wsl: WslSandbox) -> None:
    """命令经 stdin 传递，含引号/重定向的复杂命令不应被 Windows 层破坏。"""
    result = require_wsl.run("""printf '%s\\n' "a 'b' c" | tr -d "'" """)
    assert result.ok
    assert "a b c" in result.stdout
