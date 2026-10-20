"""改动文件 → 相关测试文件的影响分析。"""

from __future__ import annotations

from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.testrun import related_test_paths

ROOT = "/ws"


class _FakeSandbox:
    def __init__(self, files: list[str]) -> None:
        self.files = files

    def run(
        self, command: str, *, cwd: str | None = None, timeout: int | None = None
    ) -> ExecResult:
        return ExecResult(
            command=command,
            exit_code=0,
            stdout="\n".join(self.files),
            stderr="",
            duration_ms=0,
        )


def test_maps_source_files_to_matching_tests() -> None:
    sandbox = _FakeSandbox(
        [
            "tests/test_calc.py",
            "tests/test_other.py",
            "pkg/foo_test.py",
        ]
    )

    assert related_test_paths(
        sandbox, ROOT, ["src/calc.py", "pkg/foo.py"]  # type: ignore[arg-type]
    ) == ["pkg/foo_test.py", "tests/test_calc.py"]


def test_changed_test_file_is_included_directly() -> None:
    sandbox = _FakeSandbox(["tests/test_calc.py"])

    assert related_test_paths(
        sandbox, ROOT, ["tests/test_calc.py"]  # type: ignore[arg-type]
    ) == ["tests/test_calc.py"]


def test_no_match_returns_empty_for_full_verification_fallback() -> None:
    sandbox = _FakeSandbox(["tests/test_calc.py"])

    assert related_test_paths(
        sandbox, ROOT, ["src/unknown.py"]  # type: ignore[arg-type]
    ) == []
