"""工作区单写者锁。

同一个工作区同时跑两个 runtime 会带来两类问题：

- 两个进程一起启动 `wsl.exe`，把并发上限乘以进程数；
- 两边同时改文件/合并补丁，快照与 review 水位线互相污染。

锁文件放在宿主的审计目录（不是被锁的工作区），键是工作区路径的哈希。
同进程内用注册表保证确定性的互斥；跨进程再叠加 OS 文件锁。文件系统不支持
锁时退化为仅进程内互斥（保证单进程正确性，不牺牲可用性）。
"""

from __future__ import annotations

import errno
import hashlib
import os
import threading
from pathlib import Path

_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()

# 锁被占用时各平台实际返回的 errno：
# - Windows（msvcrt.locking）：EACCES
# - POSIX（fcntl.flock）：EAGAIN / EWOULDBLOCK / EACCES
# 必须与「文件系统不支持锁」区分开，否则第二个进程会把占用当成降级信号。
_LOCK_BUSY_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.EPERM,
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        errno.EDEADLK,
    }
)
_LOCK_UNSUPPORTED_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        errno.ENOSYS,
        errno.ENOLCK,
    )
    if value is not None
)


class WorkspaceLock:
    def __init__(self, directory: str | Path, workspace: str) -> None:
        digest = hashlib.sha1(workspace.encode("utf-8")).hexdigest()[:12]
        self.path = Path(directory) / f"workspace-{digest}.lock"
        self._key = str(self.path.resolve())
        self._handle: object | None = None
        self._os_locked = False

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        with _ACTIVE_LOCK:
            if self._key in _ACTIVE:
                return False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.path.open("a+b")
                # Windows 的 msvcrt.locking 需要至少 1 个字节可锁。
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"1")
                    handle.flush()
                handle.seek(0)
            except OSError as exc:
                # 目录不可写等 I/O 问题不是“锁被占用”；交给调用方决定是否
                # 降级（runtime 会继续运行，让真正的审计错误暴露出来）。
                raise OSError(f"无法创建/打开工作区锁：{self.path}") from exc

            os_locked = self._lock_os(handle)
            if os_locked is False:
                # 锁已被另一个进程持有：这是明确的「忙」，不能当成降级继续跑。
                try:
                    handle.close()
                except OSError:
                    pass
                return False
            _ACTIVE.add(self._key)
            self._handle = handle
            self._os_locked = os_locked is True
            return True

    def _lock_os(self, handle: object) -> bool | None:
        """尝试加 OS 锁。

        返回 `True` 已获得、`False` 被其他进程占用、`None` 不支持文件锁
        （此时只能退化为进程内互斥）。把「忙」和「不支持」混为一谈会让
        跨进程互斥在真正需要时恰好失效。
        """
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                return True
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            return True
        except ImportError:
            return None
        except OSError as exc:
            if exc.errno in _LOCK_BUSY_ERRNOS:
                return False
            if exc.errno in _LOCK_UNSUPPORTED_ERRNOS:
                return None
            # 未知的 OS 错误不冒充「被占用」，也不阻断启动：保持原来的可用性优先。
            return None

    def release(self) -> None:
        with _ACTIVE_LOCK:
            handle = self._handle
            if handle is None:
                return
            if self._os_locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)  # type: ignore[attr-defined]
                        msvcrt.locking(  # type: ignore[attr-defined]
                            handle.fileno(), msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                        )
                    else:
                        import fcntl

                        fcntl.flock(  # type: ignore[attr-defined]
                            handle.fileno(), fcntl.LOCK_UN  # type: ignore[attr-defined]
                        )
                except OSError:
                    pass
            try:
                handle.close()  # type: ignore[attr-defined]
            except OSError:
                pass
            _ACTIVE.discard(self._key)
            self._handle = None
            self._os_locked = False
