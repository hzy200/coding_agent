"""超时后的清理必须有兜底（不依赖 WSL）。

外层 `communicate(timeout=…)` 到点会 `kill()`，但 kill 只保证 wsl.exe 收到信号：
若它卡在不可中断状态（网络挂载、驱动），紧随其后的**无参** `communicate()`
会永久挂住 —— 一次已经判定超时的命令，反而在清理阶段把整轮运行钉死。
这里用的是假 Popen，所以不需要真的 WSL。
"""

from __future__ import annotations

import io
import subprocess
from typing import Any

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.limits import TIMEOUT_EXIT_CODE
from coding_agent.sandbox.wsl_exec import WslSandbox

# conftest 会把 WslSandbox._exec 换成「一碰就炸」的桩（防非 WSL 用例真跑沙箱）。
# 这里要测的正是 _exec 里的清理逻辑，所以在用例里把真身换回来 ——
# 真正会被启动的 wsl.exe 由下面的假 Popen 顶替，没有任何真实执行。
_REAL_EXEC = WslSandbox._exec


@pytest.fixture
def real_exec(monkeypatch):
    monkeypatch.setattr(WslSandbox, "_exec", _REAL_EXEC)


class _FakeProc:
    """模拟 Popen；`wait` 第一次抛超时（模拟外层到点），之后按 `hang` 决定行为。"""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
        timeout_first: bool = True,
    ) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.hang = hang
        self.timeout_first = timeout_first
        self.killed = False
        self.waits: list[Any] = []
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.hang or (self.timeout_first and not self.killed):
            raise subprocess.TimeoutExpired("wsl.exe", timeout or 1)
        self.returncode = -9 if self.killed else 0
        return self.returncode


def _sandbox(
    monkeypatch,
    proc: _FakeProc,
    *,
    max_capture_bytes: int = 1_000_000,
) -> WslSandbox:
    sandbox = WslSandbox(
        Settings(_env_file=None, max_capture_bytes=max_capture_bytes)
    )
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: proc  # noqa: ARG005
    )
    return sandbox


def test_cleanup_after_timeout_has_a_timeout(monkeypatch, real_exec) -> None:
    """清理那一次 wait 也必须带 timeout，不能裸调用。"""
    proc = _FakeProc()
    sandbox = _sandbox(monkeypatch, proc)

    result = sandbox.run("sleep 100", timeout=1)

    assert proc.killed is True
    assert result.timed_out is True
    assert result.exit_code == TIMEOUT_EXIT_CODE
    # 第一次是正式等待（wall + 余量），第二次是清理，两次都得有上限
    assert proc.waits[0] is not None and proc.waits[0] > 0
    assert proc.waits[1] is not None and proc.waits[1] > 0


def test_cleanup_gives_up_instead_of_hanging_forever(monkeypatch, real_exec) -> None:
    """连清理都超时时放弃回收，照样按超时返回 —— 不能让清理拖垮整轮运行。"""
    proc = _FakeProc(hang=True)
    sandbox = _sandbox(monkeypatch, proc)

    result = sandbox.run("sleep 100", timeout=1)

    assert proc.killed is True
    assert result.timed_out is True
    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert result.stdout == ""  # 拿不到输出也不能是 None


def test_normal_run_is_not_marked_as_timed_out(monkeypatch, real_exec) -> None:
    proc = _FakeProc(stdout=b"done", timeout_first=False)
    sandbox = _sandbox(monkeypatch, proc)

    result = sandbox.run("echo done", timeout=5)

    assert result.timed_out is False
    assert result.stdout == "done"
    assert proc.killed is False


def test_capture_is_bounded_before_it_reaches_the_host(monkeypatch, real_exec) -> None:
    proc = _FakeProc(stdout=b"x" * 10_000, timeout_first=False)
    sandbox = _sandbox(monkeypatch, proc, max_capture_bytes=100)

    result = sandbox.run("cat big.log", timeout=5, capture_limit=100)

    assert "截断" in result.stdout
    assert len(result.stdout) < 300


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
