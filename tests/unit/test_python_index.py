"""AST import 图、测试影响分析与 repo map。"""

from __future__ import annotations

import json

from coding_agent.codeintel.python_index import (
    build_index,
    build_repo_map,
    collect_python_sources,
    related_test_files,
)
from coding_agent.sandbox.wsl_exec import ExecResult

SOURCES = {
    "src/app/core.py": "def compute():\n    return 1\n",
    "src/app/api.py": "from app.core import compute\n\ndef handler():\n    return compute()\n",
    "tests/test_api.py": (
        "from app.api import handler\n\ndef test_handler():\n"
        "    assert handler()\n"
    ),
    "tests/test_other.py": "def test_other():\n    assert True\n",
}


def test_reverse_import_closure_finds_transitive_tests() -> None:
    index = build_index(SOURCES)

    assert related_test_files(index, ["src/app/core.py"]) == [
        "tests/test_api.py"
    ]


def test_filename_heuristic_is_used_even_without_imports() -> None:
    sources = {
        "app/foo.py": "def f():\n    return 1\n",
        "tests/test_foo.py": "def test_f():\n    assert True\n",
    }
    index = build_index(sources)

    assert related_test_files(index, ["app/foo.py"]) == ["tests/test_foo.py"]


def test_changed_test_is_included_directly() -> None:
    index = build_index(SOURCES)

    assert related_test_files(index, ["tests/test_other.py"]) == [
        "tests/test_other.py"
    ]


def test_repo_map_contains_top_level_symbols() -> None:
    index = build_index(SOURCES)

    text = build_repo_map(index)

    assert "src/app/core.py: def compute" in text
    assert "tests/test_api.py: def test_handler" in text


class _FakeSandbox:
    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload
        self.commands: list[str] = []

    def run(
        self, command: str, *, cwd: str | None = None, timeout: int | None = None
    ) -> ExecResult:
        self.commands.append(command)
        return ExecResult(
            command=command,
            exit_code=0,
            stdout="AGENT_INDEX_JSON:" + json.dumps(self.payload) + "\n",
            stderr="",
            duration_ms=0,
        )


def test_collect_sources_uses_one_sandbox_call() -> None:
    sandbox = _FakeSandbox({"app/a.py": "x = 1\n"})

    sources = collect_python_sources(sandbox, "/ws")  # type: ignore[arg-type]

    assert sources == {"app/a.py": "x = 1\n"}
    assert len(sandbox.commands) == 1
