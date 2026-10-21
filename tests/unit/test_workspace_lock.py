"""工作区单写者锁：同进程确定互斥，释放后可重新获取。"""

from __future__ import annotations

from coding_agent.workspace_lock import WorkspaceLock


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
