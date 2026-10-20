"""常驻 shell 的 sentinel 分帧解析。"""

from __future__ import annotations

from coding_agent.sandbox import wsl_exec
from coding_agent.sandbox.wsl_exec import (
    ExecResult,
    _parse_session_output,
    _ShellSessionPool,
)


def test_parses_stdout_stderr_and_exit_code() -> None:
    token = "abc123"
    raw = (
        b"shell noise\n"
        b"\n__AGENT_OUT_abc123__\nhello\n"
        b"\n__AGENT_ERR_abc123__\noops\n"
        b"\n__AGENT_END_abc123__:3\n"
    )

    assert _parse_session_output(raw, token) == ("hello\n", "oops\n", 3)


def test_missing_end_marker_returns_none() -> None:
    token = "abc123"
    raw = b"\n__AGENT_OUT_abc123__\nhello\n"

    assert _parse_session_output(raw, token) is None


class _FakeSession:
    created = 0

    def __init__(self, argv: list[str]) -> None:
        type(self).created += 1
        self.argv = argv
        self._alive = True
        self.closed = False

    def alive(self) -> bool:
        return self._alive

    def run(self, script: str, *, wall: int, command: str) -> ExecResult:
        return ExecResult(
            command=command,
            exit_code=0,
            stdout=command,
            stderr="",
            duration_ms=1,
        )

    def close(self) -> None:
        self.closed = True
        self._alive = False


def test_pool_reuses_an_idle_session(monkeypatch) -> None:
    _FakeSession.created = 0
    monkeypatch.setattr(wsl_exec, "_ShellSession", _FakeSession)
    pool = _ShellSessionPool(["bash"], 2)

    first = pool.run("script", wall=1, command="a")
    second = pool.run("script", wall=1, command="b")

    assert first.stdout == "a"
    assert second.stdout == "b"
    assert _FakeSession.created == 1
    pool.close()


def test_pool_discards_a_dead_session(monkeypatch) -> None:
    _FakeSession.created = 0

    class _DeadSession(_FakeSession):
        def alive(self) -> bool:
            return False

    monkeypatch.setattr(wsl_exec, "_ShellSession", _DeadSession)
    pool = _ShellSessionPool(["bash"], 2)

    pool.run("script", wall=1, command="a")
    pool.run("script", wall=1, command="b")

    assert _DeadSession.created == 2
