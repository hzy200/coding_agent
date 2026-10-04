"""集成测试：bubblewrap 隔离的真实行为。沙箱内没有 bwrap 时跳过。"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.sandbox.wsl_exec import WslSandbox

pytestmark = pytest.mark.wsl


@pytest.fixture
def sandbox(require_wsl: WslSandbox, settings) -> WslSandbox:  # noqa: ANN001
    scoped = settings.model_copy(update={"shell_sandbox": "bwrap"})
    sb = WslSandbox(scoped)
    if not sb.bwrap_available():
        pytest.skip("沙箱内 bubblewrap 不可用")
    return sb


@pytest.fixture
def workspace(require_wsl: WslSandbox) -> str:
    root = f"/tmp/bwrap-test-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(root)}")
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


def test_workspace_is_writable(sandbox: WslSandbox, workspace: str) -> None:
    result = sandbox.run("echo hi > f.txt && cat f.txt", cwd=workspace)
    assert result.ok, result.render(500)
    assert "hi" in result.stdout


def test_real_home_is_hidden(
    sandbox: WslSandbox, require_wsl: WslSandbox, workspace: str
) -> None:
    """隔离视图里 /home 是空 tmpfs，家目录下的文件读不到。"""
    secret = f"{require_wsl.home()}/.bwrap-secret-{uuid4().hex[:8]}"
    require_wsl.run(f"printf topsecret > {shlex.quote(secret)}")
    try:
        result = sandbox.run(f"cat {shlex.quote(secret)} 2>&1 || echo MISSING", cwd=workspace)
        assert "topsecret" not in result.stdout
    finally:
        require_wsl.run(f"rm -f {shlex.quote(secret)}")


def test_windows_drives_are_hidden(sandbox: WslSandbox, workspace: str) -> None:
    result = sandbox.run("test -e /mnt && echo YES || echo NO", cwd=workspace)
    assert result.stdout.strip() == "NO"


def test_system_reads_still_work(sandbox: WslSandbox, workspace: str) -> None:
    """隔离不能把正常能力也砍掉：只读系统目录仍然可读。"""
    result = sandbox.run("cat /etc/hostname >/dev/null && echo OK", cwd=workspace)
    assert result.ok and "OK" in result.stdout
