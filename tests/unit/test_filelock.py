"""跨线程/进程文件锁的线程侧语义。"""

from __future__ import annotations

import threading
import time

from coding_agent.filelock import FileLock, NullLock, file_lock, lock_path_for


def test_filelock_serializes_threads(tmp_path) -> None:
    lock = FileLock(tmp_path / "state.lock")
    inside = threading.Event()
    release = threading.Event()
    events: list[str] = []

    def holder() -> None:
        with lock:
            events.append("holder-in")
            inside.set()
            release.wait(2)
            events.append("holder-out")

    def waiter() -> None:
        assert inside.wait(2)
        with lock:
            events.append("waiter-in")

    holder_thread = threading.Thread(target=holder)
    waiter_thread = threading.Thread(target=waiter)
    holder_thread.start()
    waiter_thread.start()
    assert inside.wait(2)

    time.sleep(0.1)
    assert events == ["holder-in"]  # waiter 被锁挡住

    release.set()
    holder_thread.join(2)
    waiter_thread.join(2)

    assert events == ["holder-in", "holder-out", "waiter-in"]


def test_null_lock_and_path_helpers(tmp_path) -> None:
    with NullLock():
        pass
    with file_lock(None):
        pass

    assert lock_path_for(tmp_path / "a.json").name == "a.json.lock"


def test_filelock_reports_whether_os_locking_is_available(tmp_path) -> None:
    lock = FileLock(tmp_path / "state.lock")
    with lock:
        assert lock.degraded is False


def test_filelock_marks_degraded_when_the_lock_file_cannot_be_opened(
    tmp_path,
) -> None:
    # 直接拿目录当锁文件：open 会失败，锁退化为线程互斥并标记 degraded。
    lock = FileLock(tmp_path)
    with lock:
        assert lock.degraded is True
