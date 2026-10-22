"""工作区单写者锁：同进程与跨进程都必须互斥。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from coding_agent.workspace_lock import WorkspaceLock

SRC = Path(__file__).resolve().parents[2] / "src"


def test_same_workspace_is_exclusive(tmp_path) -> None:
    first = WorkspaceLock(tmp_path, "/ws/a")
    second = WorkspaceLock(tmp_path, "/ws/a")

    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()

    assert second.acquire() is True
    second.release()


def test_different_workspaces_do_not_conflict(tmp_path) -> None:
    first = WorkspaceLock(tmp_path, "/ws/a")
    second = WorkspaceLock(tmp_path, "/ws/b")

    assert first.acquire() is True
    assert second.acquire() is True
    first.release()
    second.release()


def test_release_is_idempotent(tmp_path) -> None:
    lock = WorkspaceLock(tmp_path, "/ws/a")
    assert lock.acquire() is True
    lock.release()
    lock.release()
    assert lock.acquire() is True
    lock.release()


def test_lock_is_exclusive_across_processes(tmp_path) -> None:
    """OS 锁被真正占用时必须返回 False，而不是当成「文件系统不支持」降级。"""
    workspace = "/ws/cross-process"
    code = textwrap.dedent(
        f"""\
        import sys, time
        sys.path.insert(0, {str(SRC)!r})
        from coding_agent.workspace_lock import WorkspaceLock

        lock = WorkspaceLock({str(tmp_path)!r}, {workspace!r})
        print("ACQUIRED" if lock.acquire() else "BUSY", flush=True)
        time.sleep(2.0)
        lock.release()
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        line = child.stdout.readline().strip() if child.stdout else ""
        if line != "ACQUIRED":
            child.wait(timeout=10)
            stderr = child.stderr.read() if child.stderr else ""
            raise AssertionError(f"子进程未能持有锁：{line!r} {stderr}")

        parent = WorkspaceLock(tmp_path, workspace)
        assert parent.acquire() is False
    finally:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)

    # 子进程退出后锁应自动释放。
    reopened = WorkspaceLock(tmp_path, workspace)
    assert reopened.acquire() is True
    reopened.release()
