from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox


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
