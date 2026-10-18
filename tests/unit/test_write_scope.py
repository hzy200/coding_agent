"""写 worker 的 write_scope 必须在 I/O 之前生效。"""

from __future__ import annotations

from typing import Any

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.fs import FileStat
from coding_agent.sandbox.pathguard import ensure_inside
from coding_agent.tools import files as files_module


class _FakeFs:
    instances: list[_FakeFs] = []

    def __init__(self, sandbox: Any, root: str, *, cache: Any = None) -> None:
        self.root = root
        self.writes: dict[str, str] = {}
        type(self).instances.append(self)

    def lexical(self, path: str) -> str:
        return ensure_inside(path, self.root, cwd=self.root)

    def probe(self, path: str) -> tuple[str, FileStat]:
        target = self.lexical(path)
        return target, FileStat(
            exists=False, is_file=False, size=0, real_path=target
        )

    def read_text(self, path: str, *, max_bytes: int) -> str:
        return ""

    def write_text(self, path: str, content: str) -> int:
        self.writes[path] = content
        return len(content.encode("utf-8"))


@pytest.fixture
def file_tools(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(files_module, "SandboxFs", _FakeFs)
    settings = Settings(_env_file=None, wsl_workspace="/ws")
    tools = files_module.build_file_tools(
        settings,
        object(),  # type: ignore[arg-type]
        allow_write=True,
        write_scope=["app/allowed"],
    )
    return {tool.name: tool for tool in tools}, settings


def test_write_outside_scope_is_rejected_before_io(file_tools) -> None:
    tools, _settings = file_tools
    output = tools["file_write"].invoke(
        {
            "path": "app/other.py",
            "content": "x = 1\n",
            "reason": "越界测试",
        }
    )
    assert "write_scope" in output
    assert "app/other.py" in output


def test_edit_outside_scope_is_rejected_before_read(file_tools) -> None:
    tools, _settings = file_tools
    output = tools["file_edit"].invoke(
        {
            "path": "app/other.py",
            "old_string": "a",
            "new_string": "b",
            "reason": "越界测试",
        }
    )
    assert "write_scope" in output


def test_write_inside_scope_reaches_the_filesystem(file_tools) -> None:
    tools, _settings = file_tools
    fs = _FakeFs.instances[-1]
    output = tools["file_write"].invoke(
        {
            "path": "app/allowed/new.py",
            "content": "x = 1\n",
            "reason": "合法写入",
        }
    )
    assert "write_scope" not in output
    assert "已创建" in output
    assert "/ws/app/allowed/new.py" in fs.writes
