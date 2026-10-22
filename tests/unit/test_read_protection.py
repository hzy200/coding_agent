"""秘密文件读保护：文件工具与检索工具都不能把内容带进上下文。"""

from __future__ import annotations

from coding_agent.config import Settings
from coding_agent.sandbox.fs import FileStat, SandboxFs
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.artifacts import unpack
from coding_agent.tools.files import build_file_tools
from coding_agent.tools.search import SEARCH_CODE, build_search_tools
from coding_agent.tools.shell import build_shell_tool


class _NoExecSandbox:
    def run(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("受保护路径不应触发任何沙箱命令")


def test_file_read_rejects_secret_files_before_io() -> None:
    settings = Settings(_env_file=None, wsl_workspace="/ws")
    tools = build_file_tools(
        settings, _NoExecSandbox(), allow_write=False  # type: ignore[arg-type]
    )
    read = next(tool for tool in tools if tool.name == "file_read")

    _text, artifact = unpack(
        read.invoke({"path": ".env", "reason": "看看配置"})
    )

    assert artifact is not None
    assert artifact["rejected"] is True
    assert artifact["ok"] is False


def test_file_read_rejects_a_symlink_to_a_secret(monkeypatch) -> None:
    """词法路径是 safe.txt，但 realpath 指向 .env：必须拒绝。"""
    settings = Settings(_env_file=None, wsl_workspace="/ws")
    monkeypatch.setattr(
        SandboxFs,
        "stat",
        lambda self, path: FileStat(
            exists=True, is_file=True, size=10, real_path="/ws/.env"
        ),
    )
    monkeypatch.setattr(
        SandboxFs,
        "read_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("不应读取受保护的真实目标")
        ),
    )
    tools = build_file_tools(
        settings, _NoExecSandbox(), allow_write=False  # type: ignore[arg-type]
    )
    read = next(tool for tool in tools if tool.name == "file_read")

    _text, artifact = unpack(
        read.invoke({"path": "safe.txt", "reason": "看链接"})
    )

    assert artifact is not None
    assert artifact["rejected"] is True


class _SearchSandbox:
    def run(self, command, *, cwd=None, timeout=None, capture_limit=None):  # noqa: ANN001
        if command.startswith("command -v rg"):
            return ExecResult(
                command=command, exit_code=1, stdout="", stderr="", duration_ms=1
            )
        return ExecResult(
            command=command,
            exit_code=0,
            stdout=".env:1:SECRET\na.py:1:ok\n",
            stderr="",
            duration_ms=1,
        )


def test_search_filters_secret_paths_from_output() -> None:
    settings = Settings(_env_file=None, wsl_workspace="/ws")
    tools = build_search_tools(
        settings, _SearchSandbox()  # type: ignore[arg-type]
    )
    search = next(tool for tool in tools if tool.name == SEARCH_CODE)

    text, _artifact = unpack(
        search.invoke({"pattern": "SECRET", "reason": "搜索"})
    )

    assert "a.py:1:ok" in text
    assert ".env:1:SECRET" not in text


def test_file_read_redacts_content_level_secrets(monkeypatch) -> None:
    """路径规则覆盖不到的配置（如 database.yml）也要在回灌前脱敏。"""
    settings = Settings(_env_file=None, wsl_workspace="/ws")
    monkeypatch.setattr(
        SandboxFs,
        "stat",
        lambda self, path: FileStat(
            exists=True,
            is_file=True,
            size=80,
            real_path="/ws/config/database.yml",
        ),
    )
    monkeypatch.setattr(
        SandboxFs,
        "read_text",
        lambda *args, **kwargs: (
            'production:\n  password: "hunter2hunter2"\n'
        ),
    )
    tools = build_file_tools(
        settings, _NoExecSandbox(), allow_write=False  # type: ignore[arg-type]
    )
    read = next(tool for tool in tools if tool.name == "file_read")

    text, artifact = unpack(
        read.invoke({"path": "config/database.yml", "reason": "看配置"})
    )

    assert artifact is not None and artifact["ok"] is True
    assert "hunter2hunter2" not in text
    assert "[REDACTED:assigned_secret]" in text


class _SecretOutputSandbox:
    def run(self, command, *, cwd=None, timeout=None, capture_limit=None):  # noqa: ANN001
        return ExecResult(
            command=command,
            exit_code=0,
            stdout="DATABASE_URL=postgres://app:hunter2hunter2@db/app\n",
            stderr="",
            duration_ms=1,
        )


def test_shell_output_is_redacted_before_the_model_sees_it() -> None:
    settings = Settings(_env_file=None, wsl_workspace="/ws")
    tool = build_shell_tool(
        settings, _SecretOutputSandbox()  # type: ignore[arg-type]
    )

    text, _artifact = unpack(
        tool.invoke({"command": "cat deploy.log", "reason": "看日志"})
    )

    assert "hunter2hunter2" not in text
    assert "[REDACTED:db_url]" in text


class _SecretSearchSandbox:
    def run(self, command, *, cwd=None, timeout=None, capture_limit=None):  # noqa: ANN001
        if command.startswith("command -v rg"):
            return ExecResult(
                command=command, exit_code=1, stdout="", stderr="", duration_ms=1
            )
        return ExecResult(
            command=command,
            exit_code=0,
            stdout='config/app.yml:3:password = "hunter2hunter2"\n',
            stderr="",
            duration_ms=1,
        )


def test_search_output_is_redacted_before_the_model_sees_it() -> None:
    settings = Settings(_env_file=None, wsl_workspace="/ws")
    tools = build_search_tools(
        settings, _SecretSearchSandbox()  # type: ignore[arg-type]
    )
    search = next(tool for tool in tools if tool.name == SEARCH_CODE)

    text, _artifact = unpack(
        search.invoke({"pattern": "password", "reason": "搜索配置"})
    )

    assert "hunter2hunter2" not in text
    assert "[REDACTED:assigned_secret]" in text
