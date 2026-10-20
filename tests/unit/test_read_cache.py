"""run 级读缓存与 shell 变更的一致性。

`SandboxFs` 的缓存只会在文件工具写路径上失效；shell 绕过 SandboxFs，
宿主无法知道它改了哪些文件。因此非只读 shell 命令必须保守地清空缓存。
"""

from __future__ import annotations

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.shell import build_shell_tool

ROOT = "/tmp/agent-read-cache"


class _FakeSandbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self, command: str, *, cwd: str | None = None, timeout: int | None = None
    ) -> ExecResult:
        return ExecResult(
            command=command, exit_code=0, stdout="", stderr="", duration_ms=0
        )


def _settings() -> Settings:
    return Settings(_env_file=None, deepseek_api_key="x", wsl_workspace=ROOT)


def test_read_only_shell_keeps_cache() -> None:
    settings = _settings()
    cache = {f"{ROOT}/a.py": b"old"}
    tool = build_shell_tool(
        settings, _FakeSandbox(settings), read_cache=cache  # type: ignore[arg-type]
    )

    tool.invoke({"command": "ls -la", "reason": "查看目录"})

    assert cache


def test_mutating_shell_clears_cache() -> None:
    settings = _settings()
    cache = {f"{ROOT}/a.py": b"old"}
    tool = build_shell_tool(
        settings, _FakeSandbox(settings), read_cache=cache  # type: ignore[arg-type]
    )

    tool.invoke({"command": "sed -i 's/old/new/' a.py", "reason": "修改文件"})

    assert cache == {}
