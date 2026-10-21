"""implement 补丁的待合并批次。

并发 worker 各自产出 patch，但主工作区的 `git apply` 必须串行且尽量
按批原子合并。`MergeQueue` 只负责「攒批 / 判断能否 flush / 丢弃」，
真正的校验、留底与应用仍在 `merge.py`。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from coding_agent.orchestrator.merge import merge_implement_patches_atomic

Finalize = Callable[[dict[str, Any]], None]


class MergeQueue:
    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []

    @property
    def pending(self) -> list[dict[str, Any]]:
        return list(self._pending)

    def __bool__(self) -> bool:
        return bool(self._pending)

    def add(self, record: dict[str, Any]) -> None:
        """加入待合并批次；真正的合并等到没有 implement 在飞时。"""
        self._pending.append(record)

    def discard(self, reason: str, finalize: Finalize) -> None:
        """取消/失败路径：未合并的补丁一律丢弃。"""
        for record in self._pending:
            record["status"] = "cancelled"
            record["merged"] = False
            record["merge_error"] = reason
            finalize(record)
        self._pending.clear()

    def has_active_implement(
        self,
        *,
        in_flight: set[str],
        status: dict[str, str],
        by_id: dict[str, dict[str, Any]],
    ) -> bool:
        """是否还有 implement **正在运行**。

        只认 `in_flight`：已完成待合并、以及因并发/合并背压还没启动的
        implement 都不算。否则合并队列永远等不到 flush，背压场景下这批
        补丁还会在收尾时被整体丢弃。
        """
        return any(
            task.get("role") == "implement"
            for task_id, task in by_id.items()
            if task_id in in_flight
        )

    async def flush(
        self,
        *,
        sandbox: Any,
        root: str,
        allow_overlap: bool,
        finalize: Finalize,
    ) -> bool:
        """原子合并当前批次；返回是否真的有补丁落进主工作区。"""
        if not self._pending:
            return False
        batch = list(self._pending)
        self._pending.clear()
        if sandbox is None or not root:
            for record in batch:
                finalize(record)
            return False

        outcomes = await merge_implement_patches_atomic(
            sandbox,
            root,
            batch,
            allow_overlap=allow_overlap,
        )
        merged_any = False
        for record in batch:
            task_id = str(record.get("task_id", ""))
            merged, merge_error, snapshot_ids = outcomes.get(
                task_id, (False, "未合并", [])
            )
            record["merged"] = merged
            record["merge_error"] = merge_error
            record["snapshot_ids"] = snapshot_ids
            if merged:
                merged_any = True
            elif record.get("status") in {"success", "partial"}:
                record["status"] = "failed"
            finalize(record)
        return merged_any
