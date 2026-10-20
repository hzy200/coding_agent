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

    async def aprune(self, thread_id: str, keep: int) -> int:
        """只保留某个 thread 最近 `keep` 个 checkpoint，返回删除条数。

        多进程/多前端长期使用同一个 thread 时，checkpoint 会无限增长：
        每轮工具调用都会落一个，SQLite 文件和恢复耗时都会持续变大。
        `keep<=0` 表示不限制。

        按 `checkpoint_id` 排序：LangGraph 使用单调递增的 UUIDv6，
        字符串序等价于时间序。checkpoint 被删时对应 writes 一并清理，
        否则会留下永远读不到的孤儿行。
        """
        if not self.persistent or self._connection is None or keep <= 0:
            return 0
        cursor = await self._connection.execute(
            """
            DELETE FROM writes AS w
            WHERE w.thread_id = ? AND NOT EXISTS (
                SELECT 1 FROM checkpoints AS c
                WHERE c.thread_id = w.thread_id
                  AND c.checkpoint_ns = w.checkpoint_ns
                  AND c.checkpoint_id = w.checkpoint_id
                  AND c.checkpoint_id IN (
                      SELECT c2.checkpoint_id FROM checkpoints AS c2
                      WHERE c2.thread_id = w.thread_id
                        AND c2.checkpoint_ns = w.checkpoint_ns
                      ORDER BY c2.checkpoint_id DESC LIMIT ?
                  )
            )
            """,
            (thread_id, keep),
        )
        removed = cursor.rowcount
        await self._connection.execute(
            """
            DELETE FROM checkpoints AS c
            WHERE c.thread_id = ? AND NOT EXISTS (
                SELECT 1 FROM (
                    SELECT c2.checkpoint_id FROM checkpoints AS c2
                    WHERE c2.thread_id = c.thread_id
                      AND c2.checkpoint_ns = c.checkpoint_ns
                    ORDER BY c2.checkpoint_id DESC LIMIT ?
                ) AS kept
                WHERE kept.checkpoint_id = c.checkpoint_id
            )
            """,
            (thread_id, keep),
        )
        await self._connection.commit()
        return max(removed, 0)

    async def aclose(self) -> None:
        # 每次写入都已 commit，所以异常退出也不会丢已落盘的 checkpoint
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        self.saver = None
