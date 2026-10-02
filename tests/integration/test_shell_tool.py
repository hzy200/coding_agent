"""集成测试：shell 工具的完整链路（策略校验 → 路径守卫 → WSL 执行）。

这一层才是真正的安全边界，policy 的单元测试只覆盖了判定逻辑本身。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.sandbox.policy import CommandLevel
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import unpack
from coding_agent.tools.shell import build_shell_tool

pytestmark = pytest.mark.wsl


@pytest.fixture
def read_only_tool(require_wsl: WslSandbox):
    return build_shell_tool(require_wsl.settings, require_wsl, max_level=CommandLevel.READ)


def _invoke(tool, **args) -> tuple[str, dict | None]:
    return unpack(tool.invoke(args))


def _call(tool, **args) -> str:
    return _invoke(tool, **args)[0]


def test_read_command_executes(read_only_tool) -> None:
    out = _call(read_only_tool, command="echo tool-roundtrip", reason="冒烟验证")
    assert "L0 只读" in out
    assert "tool-roundtrip" in out
    assert "exit_code=0" in out


def test_dangerous_command_is_refused_before_execution(read_only_tool) -> None:
    out = _call(read_only_tool, command="rm -rf /", reason="测试拦截")
    assert "命令被安全策略拒绝" in out
    assert "L3 危险" in out
    # 必须真正没被执行
    assert "exit_code" not in out


def test_write_command_refused_in_read_only_mode(read_only_tool) -> None:
    out = _call(read_only_tool, command="mkdir /tmp/should-not-exist", reason="测试拦截")
    assert "命令被安全策略拒绝" in out
    assert "L1 低风险写" in out


def test_write_command_allowed_when_raised(require_wsl: WslSandbox) -> None:
    tool = build_shell_tool(require_wsl.settings, require_wsl, max_level=CommandLevel.LOW_WRITE)
    workspace = resolve_workspace(require_wsl.settings, require_wsl)
    probe = f"probe-{uuid4().hex[:8]}"
    try:
        out = _call(tool, command=f"mkdir -p {probe}", reason="验证 L1 放行", cwd=workspace)
        assert "L1 低风险写" in out
        assert "exit_code=0" in out
    finally:
        # 直连沙箱清理，避免测试把残留目录留在工作区里
        require_wsl.run(f"rmdir {shlex.quote(f'{workspace}/{probe}')}")


def test_cwd_outside_workspace_is_rejected(read_only_tool) -> None:
    out = _call(read_only_tool, command="ls", reason="测试路径守卫", cwd="/etc")
    assert "工作目录非法" in out


def test_failed_command_reports_exit_code(read_only_tool) -> None:
    """失败命令不能抛异常，必须把 exit_code 回灌给模型以便自纠正。"""
    out = _call(read_only_tool, command="ls /no-such-path-agent-probe", reason="验证失败回灌")
    assert "exit_code=2" in out
    assert "No such file" in out


# ---------------- artifact：事件层与审计层依赖它 ----------------

def test_success_artifact(read_only_tool) -> None:
    text, artifact = _invoke(read_only_tool, command="echo hi", reason="验证产物")
    assert artifact is not None
    assert artifact["ok"] is True
    assert artifact["exit_code"] == 0
    assert artifact["rejected"] is False
    assert artifact["level"] == 0
    assert artifact["level_label"] == "L0 只读"
    assert artifact["duration_ms"] >= 0
    # 模型看到的文本里不应出现封装 JSON
    assert '"artifact"' not in text


def test_rejected_artifact_has_no_exit_code(read_only_tool) -> None:
    _, artifact = _invoke(read_only_tool, command="rm -rf /", reason="验证拦截产物")
    assert artifact is not None
    assert artifact["rejected"] is True
    assert artifact["ok"] is False
    assert artifact["exit_code"] is None  # 根本没执行
    assert artifact["level_label"] == "L3 危险"


def test_failure_artifact_carries_exit_code(read_only_tool) -> None:
    _, artifact = _invoke(read_only_tool, command="ls /nope", reason="验证失败产物")
    assert artifact is not None
    assert artifact["ok"] is False
    assert artifact["exit_code"] == 2
