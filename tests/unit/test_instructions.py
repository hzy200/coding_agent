"""分层项目指令：发现顺序、合并与容量上限。"""

from __future__ import annotations

from coding_agent import instructions
from coding_agent.sandbox.wsl_exec import ExecResult


class _Sandbox:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.commands: list[str] = []

    def run(self, command: str, *, cwd=None, timeout=None) -> ExecResult:  # noqa: ANN001
        self.commands.append(command)
        return ExecResult(
            command=command,
            exit_code=0,
            stdout=self.stdout,
            stderr="",
            duration_ms=0,
        )


class _Fs:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def read_text(self, path: str, *, max_bytes: int) -> str:
        from coding_agent.sandbox.fs import SandboxFsError

        if path not in self.files:
            raise SandboxFsError("not found")
        return self.files[path]


def test_discover_orders_shallow_before_deep() -> None:
    sandbox = _Sandbox(
        "./src/deep/AGENTS.md\n./CLAUDE.md\n./src/AGENTS.md\n./README.md\n"
    )
    found = instructions.discover_instruction_paths(sandbox, "/work")
    assert found == ["CLAUDE.md", "src/AGENTS.md", "src/deep/AGENTS.md"]


def test_discover_filters_unrelated_names() -> None:
    sandbox = _Sandbox("./README.md\n./docs/guide.md\n")
    assert instructions.discover_instruction_paths(sandbox, "/work") == []


def test_load_merges_files_with_headers(monkeypatch) -> None:
    sandbox = _Sandbox("./CLAUDE.md\n./src/AGENTS.md\n")
    fs = _Fs(
        {
            "CLAUDE.md": "根目录规则",
            "src/AGENTS.md": "子目录规则",
        }
    )
    monkeypatch.setattr(
        instructions, "SandboxFs", lambda _sandbox, _root: fs
    )
    text = instructions.load_project_instructions(sandbox, "/work")
    assert "### CLAUDE.md" in text
    assert "### src/AGENTS.md" in text
    # 更具体的子目录指令排在后面
    assert text.index("根目录规则") < text.index("子目录规则")


def test_no_instruction_files_returns_empty(monkeypatch) -> None:
    sandbox = _Sandbox("")
    monkeypatch.setattr(
        instructions, "SandboxFs", lambda _sandbox, _root: _Fs({})
    )
    assert instructions.load_project_instructions(sandbox, "/work") == ""
