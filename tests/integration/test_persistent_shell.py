"""常驻 WSL bash：复用进程、保留 exit code/stderr/超时语义。"""

from __future__ import annotations

import pytest

from coding_agent.sandbox.wsl_exec import WslSandbox


@pytest.mark.wsl
def test_persistent_shell_reuses_one_process(require_wsl) -> None:  # noqa: ANN001
    settings = require_wsl.settings.model_copy(
        update={"persistent_shell": True, "persistent_shell_pool": 2}
    )
    sandbox = WslSandbox(settings)
    try:
        first = sandbox.run("printf hello")
        second = sandbox.run("printf bye")

        assert first.stdout == "hello"
        assert second.stdout == "bye"
        assert sandbox.command_count == 1
    finally:
        sandbox.close()


@pytest.mark.wsl
def test_persistent_shell_keeps_exit_code_and_stderr(require_wsl) -> None:  # noqa: ANN001
    settings = require_wsl.settings.model_copy(
        update={"persistent_shell": True, "persistent_shell_pool": 1}
    )
    sandbox = WslSandbox(settings)
    try:
        result = sandbox.run("printf err >&2; exit 3")

        assert result.exit_code == 3
        assert "err" in result.stderr
    finally:
        sandbox.close()


@pytest.mark.wsl
def test_persistent_shell_timeout_does_not_poison_the_next_command(  # noqa: ANN001
    require_wsl,
) -> None:
    settings = require_wsl.settings.model_copy(
        update={"persistent_shell": True, "persistent_shell_pool": 1}
    )
    sandbox = WslSandbox(settings)
    try:
        result = sandbox.run("sleep 10", timeout=1)
        assert result.timed_out

        after = sandbox.run("printf ok")
        assert after.stdout == "ok"
    finally:
        sandbox.close()
