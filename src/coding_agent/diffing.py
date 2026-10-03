"""unified diff 的生成与统计。

文件工具、快照回滚、前端展示都用同一套语义，避免三处各写一份 `difflib` 调用
却在边界（结尾换行、空文件、统计口径）上各自飘。
"""

from __future__ import annotations

import difflib


def unified_diff(old: str, new: str, display_path: str, context: int = 3) -> str:
    """生成 unified diff；内容相同返回空串。"""
    if old == new:
        return ""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{display_path}",
            tofile=f"b/{display_path}",
            n=context,
        )
    )


def count_changes(diff: str) -> tuple[int, int]:
    """统计新增/删除行数。跳过 `+++` / `---` 文件头，否则每条 diff 都虚增两行。"""
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed
