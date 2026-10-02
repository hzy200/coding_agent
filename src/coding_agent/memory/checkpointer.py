"""checkpoint 后端的生命周期封装。

- path 为空 → MemorySaver：会话只在进程内有效，重启即丢。
- path 为 `:memory:` → 同样是内存，用于显式声明「不要落盘」。
- 其他路径 → AsyncSqliteSaver 落盘，会话可跨进程恢复（`--thread-id` 复用）。

为什么必须用 **Async**SqliteSaver：同步的 SqliteSaver 虽然方法名齐全，
但 `aget/aput/alist` 全是抛 `NotImplementedError` 的占位。我们的编排是异步的，
用同步版会在第一次 checkpoint 时直接失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

IN_MEMORY = ":memory:"


class CheckpointStore:
    """持有 checkpoint 后端及其连接。

    持久化模式下连接绑定事件循环，必须在同一个循环里 `aopen()` / `aclose()`。
    """

    def __init__(self, path: str | None) -> None:
        self.path = (path or "").strip()
        self._connection: Any = None
        self.saver: Any = None

    @property
    def persistent(self) -> bool:
        return bool(self.path) and self.path != IN_MEMORY

    async def aopen(self) -> Any:
        if self.saver is not None:
            return self.saver

        if not self.persistent:
            self.saver = MemorySaver()
            return self.saver

        # 延迟导入：不落盘的场景不必拉起 aiosqlite
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        target = Path(self.path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(str(target))
        self.saver = AsyncSqliteSaver(self._connection)
        await self.saver.setup()
        return self.saver

    async def aclose(self) -> None:
        # 每次写入都已 commit，所以异常退出也不会丢已落盘的 checkpoint
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        self.saver = None
