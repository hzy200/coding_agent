from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox


@pytest.fixture(autouse=True)
def _forbid_real_wsl_unless_marked(request: pytest.FixtureRequest, monkeypatch) -> None:
    """非 WSL 用例不得真的执行沙箱命令。

    Linux CI 上没有 wsl.exe，漏标的用例只会在那边才炸；这个守卫让它在**任何平台**
    当场失败（本机有 WSL 也一样），把问题挡在推送之前。
    确实需要探测宿主 WSL 的用例（如 `doctor`）标 `@pytest.mark.wsl_env` 放行；
    需要真实沙箱的集成用例标 `@pytest.mark.wsl`。
    """
    if request.node.get_closest_marker("wsl") or request.node.get_closest_marker("wsl_env"):
        return

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError(
            "非 WSL 用例触发了一次真实的沙箱执行。请改用假依赖（例如注入空记忆），"
            "或为该用例加 @pytest.mark.wsl（真实沙箱）/ @pytest.mark.wsl_env（探测宿主）。"
        )

    monkeypatch.setattr(WslSandbox, "_exec", _boom)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def sandbox(settings: Settings) -> WslSandbox:
    return WslSandbox(settings)


@pytest.fixture
def require_wsl(sandbox: WslSandbox) -> WslSandbox:
    """需要真实 WSL 的测试用它，环境不具备时自动跳过。"""
    if not WslSandbox.available(sandbox.settings.wsl_distro):
        pytest.skip(f"WSL 发行版 {sandbox.settings.wsl_distro} 不可用")
    return sandbox
