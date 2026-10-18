"""implement 补丁的宿主侧合并。

并发只发生在 worktree；主工作区的 `git apply` 必须串行。这里把
「check → 解析路径 → 留快照 → apply」收成一个可复用函数，旧 dispatch
和事件驱动 scheduler 共用同一套判定，避免两条路径的合并语义分叉。
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from coding_agent.orchestrator.nodes import (
    _normalize_patch_path,
    _patch_paths,
    _snapshot_paths,
)
from coding_agent.sandbox.wsl_exec import WslSandbox


async def merge_implement_patch(
    sandbox: WslSandbox,
    root: str,
    record: dict[str, Any],
) -> tuple[bool, str, list[str]]:
    """把一条 implement WorkerResult 的补丁合并进主工作区。

    返回 `(merged, error, snapshot_ids)`。任何一步失败都不应用补丁。
    """
    patch_path = str(record.get("patch_path") or "")
    if not patch_path:
        return False, "补丁路径为空", []
    if record.get("status") in {"failed", "cancelled"}:
        return False, "worker 未成功，补丁不合并", []

    check = await asyncio.to_thread(
        sandbox.run,
        f"git apply --check {shlex.quote(patch_path)}",
        cwd=root,
    )
    if not check.ok:
        return False, check.render(400), []

    patch_paths, path_error = await asyncio.to_thread(
        _patch_paths, sandbox, patch_path, root
    )
    if path_error:
        return False, path_error, []

    snapshot_paths = sorted(
        {
            normalized
            for normalized in (
                _normalize_patch_path(str(item))
                for item in [
                    *(record.get("files") or []),
                    *patch_paths,
                ]
            )
            if normalized
        }
    )
    snapshot_ids, snapshot_error = await asyncio.to_thread(
        _snapshot_paths, sandbox, root, snapshot_paths
    )
    if snapshot_error:
        return False, snapshot_error, snapshot_ids

    applied = await asyncio.to_thread(
        sandbox.run,
        f"git apply {shlex.quote(patch_path)}",
        cwd=root,
    )
    if applied.ok:
        return True, "", snapshot_ids
    return False, applied.render(400), snapshot_ids
