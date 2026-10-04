"""bwrap 隔离包装：argv 构造与 fail-closed（不依赖 WSL）。"""

from __future__ import annotations

import shlex

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox, WslUnavailableError, build_bwrap_script

# ---------------- argv 构造 ----------------

def test_script_wraps_command_with_bwrap() -> None:
    script = build_bwrap_script("ls -la", workdir="/ws", workspace="/ws")
    parts = shlex.split(script)
    assert parts[0] == "bwrap"
    # 命令作为一个完整参数传给 `bash -lc`，不被拆散
    assert parts[-3:] == ["bash", "-lc", "ls -la"]


def test_script_hides_home_and_keeps_system_readonly() -> None:
    script = build_bwrap_script("true", workdir="/ws", workspace="/ws")
    assert "--ro-bind /etc /etc" in script
    assert "--tmpfs /home" in script
    assert "--tmpfs /root" in script
    assert "--unshare-user" in script
    assert "--unshare-pid" in script


def test_workspace_is_bound_readwrite_and_deduplicated() -> None:
    script = build_bwrap_script("true", workdir="/ws", workspace="/ws")
    assert script.count("--bind /ws /ws") == 1
    assert "--chdir /ws" in script


def test_binding_extra_workspace_when_cwd_is_a_subdir() -> None:
    script = build_bwrap_script("true", workdir="/ws/sub", workspace="/ws")
    assert "--bind /ws /ws" in script
    assert "--bind /ws/sub /ws/sub" in script
    assert "--chdir /ws/sub" in script


def test_command_with_special_chars_survives_quoting() -> None:
    command = "echo 'a b' && grep -n \"x y\" f.txt"
    parts = shlex.split(build_bwrap_script(command, workdir="/ws"))
    assert parts[-1] == command  # 原样作为一个参数


# ---------------- fail closed ----------------

def test_bwrap_requested_but_unavailable_fails_closed() -> None:
    sandbox = WslSandbox(
        Settings(_env_file=None, shell_sandbox="bwrap", wsl_workspace="/ws")
    )
    sandbox._bwrap_ok = False  # 模拟沙箱内不可用（不真的探测 WSL）
    with pytest.raises(WslUnavailableError, match="bubblewrap"):
        sandbox.run("ls")


def test_bwrap_requires_a_workspace_path() -> None:
    sandbox = WslSandbox(
        Settings(_env_file=None, shell_sandbox="bwrap", wsl_workspace="")
    )
    sandbox._bwrap_ok = True
    with pytest.raises(WslUnavailableError, match="工作区"):
        sandbox.run("ls")


def test_default_mode_does_not_wrap() -> None:
    assert Settings(_env_file=None).shell_sandbox == "off"
