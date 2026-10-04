"""R2 守卫自测：非 WSL 用例不得真的执行沙箱命令。

`tests/conftest.py` 的 autouse fixture 会把 `WslSandbox._exec` 换成抛错，
这样"本不该碰 WSL 的用例碰了 WSL"会在任何平台当场暴露（而不是只在 Linux CI 上）。
"""

from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox


def test_non_wsl_test_cannot_execute_real_sandbox_command() -> None:
    sandbox = WslSandbox(Settings(_env_file=None, wsl_workspace="/ws"))
    with pytest.raises(AssertionError, match="非 WSL 用例"):
        sandbox.run("true")
