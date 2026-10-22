"""跨进程文件锁（best-effort）。

用于需要「读-改-写」的宿主侧小文件：限流状态、审计追加、steering 队列、
工具执行日志。锁文件与数据文件同目录，退出时自动解锁并关闭。

文件系统不支持锁时退化为 no-op（仍能工作，只是少一层跨进程保护）——
与工作区锁不同：这些是诊断/协调数据，不是安全边界，不应该因为锁不可用
让整个运行失败。
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

# 同进程内对同一路径也必须有互斥：OS 文件的锁在不同平台/句柄上的语义
# 不完全一致，而且两个线程各开一个句柄时会绕过单句柄的锁。
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.RLock:
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path.absolute())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


class FileLock(AbstractContextManager["FileLock"]):
    """跨线程 + 跨进程排他锁；OS 锁不可用时退化为仅线程锁。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: Any = None
        self._thread_lock = _thread_lock_for(self.path)
        self._thread_locked = False
        # OS 锁不可用时置位：调用方（doctor）可据此提示「当前只有线程互斥」。
        self.degraded = False

    def __enter__(self) -> FileLock:
        self._thread_lock.acquire()
        self._thread_locked = True
        try:
            # 调用方通常已经建好目录；避免每条审计记录都重复 mkdir。
            if not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "a+b")
            self._file.seek(0)
            if self._file.read(1) == b"":
                self._file.write(b"0")
                self._file.flush()
            self._file.seek(0)
            try:
                import msvcrt
            except ImportError:
                try:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
                except OSError:
                    self.degraded = True
            else:
                try:
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    self.degraded = True
        except OSError:
            self._file = None
            self.degraded = True
        return self

    def __exit__(self, *exc_info: Any) -> None:
        try:
            if self._file is not None:
                try:
                    self._file.seek(0)
                    try:
                        import msvcrt

                        msvcrt.locking(
                            self._file.fileno(), msvcrt.LK_UNLCK, 1
                        )
                    except ImportError:
                        import fcntl

                        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                finally:
                    try:
                        self._file.close()
                    except OSError:
                        pass
                    self._file = None
        finally:
            if self._thread_locked:
                self._thread_locked = False
                self._thread_lock.release()


class NullLock(AbstractContextManager["NullLock"]):
    """没有可锁路径时的占位实现。"""

    degraded = False

    def __enter__(self) -> NullLock:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


def lock_path_for(path: str | Path) -> Path:
    """数据文件对应的锁文件路径（保持原后缀，避免与数据文件混淆）。"""
    target = Path(path)
    return target.with_suffix(target.suffix + ".lock")


def file_lock(path: str | Path | None) -> FileLock | NullLock:
    return FileLock(path) if path is not None else NullLock()
