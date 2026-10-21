"""包管理器可用性探测：pip 缺失时回退 python3 -m pip。"""

from __future__ import annotations

from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.deps import resolve_manager


class _FakeSandbox:
    def __init__(self, available: set[str]) -> None:
        self.available = available

    def run(self, command: str, **_kwargs) -> ExecResult:
        ok = any(marker in command for marker in self.available)
        return ExecResult(
            command=command,
            exit_code=0 if ok else 1,
            stdout="",
            stderr="",
            duration_ms=1,
        )


def test_resolve_manager_prefers_pip_when_available() -> None:
    sandbox = _FakeSandbox({"command -v pip"})
    assert resolve_manager(sandbox, ["pip", "list"]) == ["pip", "list"]


def test_resolve_manager_falls_back_to_python_module_pip() -> None:
    sandbox = _FakeSandbox({"python3 -m pip --version"})
    assert resolve_manager(sandbox, ["pip", "install"]) == [
        "python3",
        "-m",
        "pip",
        "install",
    ]


def test_resolve_manager_returns_none_when_nothing_is_available() -> None:
    sandbox = _FakeSandbox(set())
    assert resolve_manager(sandbox, ["pip", "list"]) is None
    assert resolve_manager(sandbox, ["uv", "add"]) is None


def test_resolve_manager_checks_other_managers() -> None:
    sandbox = _FakeSandbox({"command -v uv"})
    assert resolve_manager(sandbox, ["uv", "add"]) == ["uv", "add"]
