"""集成测试：shell 工具的完整链路（路径守卫 → WSL 执行 → 产物回报）。

注意：shell 工具**不做**安全分级判定 —— 那是 approval_gate + tools 节点的事
（见 tests/unit/test_nodes.py）。这里只验证执行与回报。
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from coding_agent.sandbox.policy import CommandLevel
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import ShellArtifact, unpack
from coding_agent.tools.shell import build_shell_tool

pytestmark = pytest.mark.wsl


@pytest.fixture
def tool(require_wsl: WslSandbox, workspace: str, settings):
    scoped = settings.model_copy(update={"wsl_workspace": workspace})
    return build_shell_tool(scoped, require_wsl)


@pytest.fixture
def workspace(require_wsl: WslSandbox) -> str:
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-sh-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {root}")
    yield root
    require_wsl.run(f"rm -rf {root}")


def _invoke(tool, **args) -> tuple[str, ShellArtifact]:
    text, artifact = unpack(tool.invoke(args))
    assert artifact is not None
    return text, ShellArtifact.model_validate(artifact)


def _call(tool, **args) -> str:
    return _invoke(tool, **args)[0]


def test_read_command_executes(tool) -> None:
    out = _call(tool, command="echo tool-roundtrip", reason="冒烟验证")
    assert "L0 只读" in out
    assert "tool-roundtrip" in out
    assert "exit_code=0" in out


def test_success_artifact(tool) -> None:
    text, artifact = _invoke(tool, command="echo hi", reason="验证产物")
    assert artifact.ok is True
    assert artifact.exit_code == 0
    assert artifact.rejected is False
    assert artifact.level == int(CommandLevel.READ)
    assert artifact.level_label == "L0 只读"
    assert artifact.decision == "auto"
    assert artifact.duration_ms >= 0
    # 模型看到的文本里不应出现封装 JSON
    assert '"artifact"' not in text


def test_failed_command_reports_exit_code(tool) -> None:
    """失败命令不能抛异常，必须把 exit_code 回灌给模型以便自纠正。"""
    text, artifact = _invoke(tool, command="ls /no-such-path-agent-probe", reason="验证失败回灌")
    assert artifact.ok is False
    assert artifact.exit_code == 2
    assert "No such file" in text


def test_stderr_is_reported(tool) -> None:
    text, artifact = _invoke(tool, command="echo boom >&2; exit 1", reason="验证 stderr")
    assert artifact.exit_code == 1
    assert "boom" in text


def test_command_level_is_reported_not_enforced(tool) -> None:
    """等级只用于回报与审计：工具自己不放行也不拦截。"""
    _, artifact = _invoke(tool, command="pip install requests", reason="验证等级回报")
    assert artifact.level == int(CommandLevel.MUTATE)
    assert artifact.level_label == "L2 变更性"


def test_cwd_inside_workspace_is_honoured(tool, workspace) -> None:
    text, artifact = _invoke(tool, command="pwd", reason="验证 cwd", cwd=workspace)
    assert artifact.ok
    assert workspace in text


def test_cwd_outside_workspace_is_rejected(tool) -> None:
    """路径守卫仍留在工具里 —— 它与审批无关，是执行侧的必要约束。"""
    text, artifact = _invoke(tool, command="ls", reason="测试路径守卫", cwd="/etc")
    assert artifact.rejected is True
    assert "工作目录非法" in text
    assert artifact.exit_code is None


def test_awkward_quoting_survives(tool) -> None:
    """命令经 stdin 传递，含引号/重定向的复杂命令不应被 Windows 层破坏。"""
    text, _ = _invoke(
        tool, command="""printf '%s\\n' "a 'b' c" | tr -d "'" """, reason="验证引号"
    )
    assert "a b c" in text


def test_timeout_is_reported(require_wsl, workspace, settings) -> None:
    # 超时上限来自 sandbox 的配置，所以要另建一个短超时的实例
    fast = settings.model_copy(update={"wsl_workspace": workspace, "shell_timeout": 2})
    fast_sandbox = WslSandbox(fast)
    require_wsl.run("true", timeout=60)  # 预热，避免冷启动耗时被算进超时

    started = time.perf_counter()
    text, artifact = _invoke(
        build_shell_tool(fast, fast_sandbox), command="sleep 30", reason="验证超时"
    )
    elapsed = time.perf_counter() - started

    assert artifact.timed_out is True
    assert artifact.exit_code == 124  # 沙箱内 timeout 的约定退出码
    assert "timed_out=true" in text
    # 关键：由沙箱内的 timeout 生效，而不是等外层 subprocess 兜底
    # （外层是 shell_timeout + 10s 的宽限，会明显更久）
    assert elapsed < 8, f"超时未被沙箱内 timeout 及时终止，耗时 {elapsed:.1f}s"
