"""文件工具：模型修改代码的唯一通道。

设计要点（对应「Shell 与文件工具分离的双工具架构」）：

- **不走 shell**：内容经 base64 在沙箱内落盘，不经过命令行解释，不受引号/换行影响。
- **精确替换**：`file_edit` 要求 `old_string` 在文件中唯一，出现 0 次或多于 1 次都拒绝执行，
  并把原因回灌给模型让它补充上下文 —— 这是「修改精确」的实现方式。
- **写前备份**：覆盖或编辑前把原文件存进 `.agent/backups/<snapshot_id>/`，
  回滚时据此还原（见 `sandbox/snapshots.py`）。
- **路径双重校验**：词法 + realpath（见 sandbox/fs.py），堵住符号链接逃逸。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.diffing import count_changes, unified_diff
from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.snapshots import SnapshotStore
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import FileArtifact, pack

READ_TOOL_NAME = "file_read"
WRITE_TOOL_NAME = "file_write"
EDIT_TOOL_NAME = "file_edit"
RESTORE_TOOL_NAME = "file_restore"

DEFAULT_READ_LINES = 400

READ_DESCRIPTION = """\
读取工作区内某个文本文件的内容，返回带行号的文本。

何时使用：需要查看代码、配置、日志的具体内容时。
注意：只读，不会修改文件。大文件请用 offset/limit 分段读取。
"""

WRITE_DESCRIPTION = """\
把完整内容写入工作区内的文件（存在则覆盖，不存在则创建）。

何时使用：新建文件，或需要整体重写一个小文件。
何时不用：只想改几行时请用 file_edit —— 覆盖式写入容易丢掉别处的改动。
"""

EDIT_DESCRIPTION = """\
在工作区内的文件里做一次精确的字符串替换。

要求 old_string 在文件中**唯一出现**：
- 出现 0 次 → 拒绝，请先读取文件确认实际内容。
- 出现多次 → 拒绝，请把 old_string 扩展到包含上下文的唯一片段（或显式用 replace_all）。
这条约束是为了保证修改精确、可预测。

何时使用：改动已有文件的局部内容。
"""

RESTORE_DESCRIPTION = """\
把一个文件回滚到之前某次修改前的状态。

何时使用：你刚做的修改是错的、或用户要求撤销时。
不传参数则回滚最近一次文件改动；也可以给 path 回滚该文件最近一次修改，
或用 snapshot_id 指定具体快照。

回滚前会先给当前内容留底，所以这次回滚本身也可以再回滚。
"""


class ReadInput(BaseModel):
    path: str = Field(description="文件路径，工作区内相对路径或绝对路径")
    reason: str = Field(description="读取这个文件的意图，一句话说明")
    offset: int | None = Field(default=None, description="起始行号（从 1 开始），省略则从头读")
    limit: int | None = Field(default=None, description=f"最多读取行数，默认 {DEFAULT_READ_LINES}")


class WriteInput(BaseModel):
    path: str = Field(description="文件路径，工作区内相对路径或绝对路径")
    content: str = Field(description="写入的完整内容")
    reason: str = Field(description="写这个文件的意图，一句话说明")


class EditInput(BaseModel):
    path: str = Field(description="文件路径，工作区内相对路径或绝对路径")
    old_string: str = Field(description="要被替换掉的原文，必须与文件内容逐字符一致且唯一")
    new_string: str = Field(description="替换成的新内容；传空字符串表示删除这段")
    reason: str = Field(description="这次修改的意图，一句话说明")
    replace_all: bool = Field(default=False, description="old_string 出现多次时是否全部替换")


class RestoreInput(BaseModel):
    path: str | None = Field(
        default=None, description="要回滚的文件；省略则回滚最近一次文件改动"
    )
    snapshot_id: str | None = Field(
        default=None, description="指定快照 id；省略则用该文件最近一次留底"
    )
    reason: str = Field(description="回滚的意图，一句话说明")


def _relpath(path: str, root: str) -> str:
    """展示用的工作区相对路径。"""
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def build_file_tools(
    settings: Settings,
    sandbox: WslSandbox,
    *,
    allow_write: bool,
) -> list[BaseTool]:
    root = resolve_workspace(settings, sandbox)
    fs = SandboxFs(sandbox, root)
    snapshots = SnapshotStore(sandbox, fs, root)
    max_bytes = settings.max_file_read_bytes

    def _reject(path: str, action: str, message: str) -> str:
        artifact = FileArtifact(path=path, action=action, ok=False, rejected=True)
        return pack(message, artifact)

    def _error(path: str, action: str, message: str) -> str:
        artifact = FileArtifact(path=path, action=action, ok=False)
        return pack(message, artifact)

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def _read(path: str, reason: str, offset: int | None = None, limit: int | None = None) -> str:
        try:
            target = fs.resolve(path)
        except SandboxPathError as exc:
            return _reject(path, "read", f"路径被拒绝：{exc}")
        except SandboxFsError as exc:
            return _error(path, "read", f"路径解析失败：{exc}")

        try:
            text = fs.read_text(target, max_bytes=max_bytes)
        except SandboxFsError as exc:
            return _error(target, "read", str(exc))

        lines = text.splitlines()
        total = len(lines)
        start = max((offset or 1) - 1, 0)
        count = limit if limit is not None else DEFAULT_READ_LINES
        window = lines[start : start + count]

        numbered = "\n".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(window))
        header = f"文件 {_relpath(target, root)}（共 {total} 行"
        if start or len(window) < total:
            header += f"，显示第 {start + 1}-{start + len(window)} 行"
        header += f"）\n{reason}"

        artifact = FileArtifact(
            path=_relpath(target, root),
            action="read",
            ok=True,
            lines_read=len(window),
            lines_total=total,
        )
        return pack(f"{header}\n{numbered}" if window else f"{header}\n（文件为空）", artifact)

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    def _write(path: str, content: str, reason: str) -> str:
        # probe 一次拿到路径与状态：拆成 resolve + stat 就是两次 wsl.exe 启动
        try:
            target, info = fs.probe(path)
        except SandboxPathError as exc:
            return _reject(path, "overwrite", f"路径被拒绝：{exc}")
        except SandboxFsError as exc:
            return _error(path, "overwrite", f"无法读取文件状态：{exc}")

        original: str | None = None
        if info.exists:
            if not info.is_file:
                return _error(target, "overwrite", f"不是普通文件，拒绝覆盖：{target}")
            try:
                original = fs.read_text(target, max_bytes=max_bytes)
            except SandboxFsError as exc:
                return _error(target, "overwrite", f"无法读取原文件，拒绝覆盖：{exc}")

        action = "overwrite" if info.exists else "create"
        snapshot_id: str | None = None
        if original is not None:
            try:
                snapshot_id = snapshots.save(target, original)
            except (SandboxFsError, SandboxPathError) as exc:
                return _error(target, action, f"备份失败，已中止写入：{exc}")

        try:
            written = fs.write_text(target, content)
        except (SandboxFsError, SandboxPathError) as exc:
            return _error(target, action, f"写入失败：{exc}")

        diff = unified_diff(original or "", content, _relpath(target, root))
        added, removed = count_changes(diff)
        verb = "已覆盖" if action == "overwrite" else "已创建"
        note = f"（备份 {snapshot_id}）" if snapshot_id else ""
        artifact = FileArtifact(
            path=_relpath(target, root),
            action=action,
            ok=True,
            bytes_written=written,
            added=added,
            removed=removed,
            diff=diff,
            snapshot_id=snapshot_id,
        )
        text = f"[文件] {reason}\n{verb} {_relpath(target, root)}，{written} 字节{note}"
        return pack(f"{text}\n{diff}" if diff else text, artifact)

    # ------------------------------------------------------------------
    # edit
    # ------------------------------------------------------------------

    def _edit(
        path: str,
        old_string: str,
        new_string: str,
        reason: str,
        replace_all: bool = False,
    ) -> str:
        try:
            target = fs.resolve(path)
        except (SandboxPathError, SandboxFsError) as exc:
            return _reject(path, "edit", f"路径被拒绝：{exc}")

        try:
            original = fs.read_text(target, max_bytes=max_bytes)
        except SandboxFsError as exc:
            return _error(target, "edit", str(exc))

        hits = original.count(old_string)
        if hits == 0:
            return _error(
                target,
                "edit",
                f"old_string 在 {_relpath(target, root)} 中未出现，未做任何修改。"
                f"请先用 {READ_TOOL_NAME} 读取文件，确认要替换的原文逐字符一致。",
            )
        if hits > 1 and not replace_all:
            shown = _relpath(target, root)
            return _error(
                target,
                "edit",
                f"old_string 在 {shown} 中出现了 {hits} 次，无法确定改哪一处，未做任何修改。"
                f"请把 old_string 扩展到包含上下文的唯一片段，或显式设置 replace_all=true。",
            )
        if old_string == new_string:
            return _error(target, "edit", "old_string 与 new_string 相同，无需修改。")

        updated = original.replace(old_string, new_string) if replace_all else original.replace(
            old_string, new_string, 1
        )

        try:
            snapshot_id = snapshots.save(target, original)
        except (SandboxFsError, SandboxPathError) as exc:
            return _error(target, "edit", f"备份失败，已中止修改：{exc}")

        try:
            fs.write_text(target, updated)
        except (SandboxFsError, SandboxPathError) as exc:
            return _error(target, "edit", f"写入失败：{exc}")

        diff = unified_diff(original, updated, _relpath(target, root))
        added, removed = count_changes(diff)
        scope = f"{hits} 处" if replace_all else "1 处"
        artifact = FileArtifact(
            path=_relpath(target, root),
            action="edit",
            ok=True,
            bytes_written=len(updated.encode("utf-8")),
            added=added,
            removed=removed,
            diff=diff,
            snapshot_id=snapshot_id,
        )
        text = (
            f"[文件] {reason}\n已修改 {_relpath(target, root)}（{scope}，"
            f"+{added} -{removed}，备份 {snapshot_id}）\n{diff}"
        )
        return pack(text, artifact)

    # ------------------------------------------------------------------
    # restore
    # ------------------------------------------------------------------

    def _restore(
        reason: str, path: str | None = None, snapshot_id: str | None = None
    ) -> str:
        entry = None
        if snapshot_id:
            wanted = None
            if path:
                try:
                    wanted = _relpath(fs.resolve(path), root).lstrip("/")
                except (SandboxPathError, SandboxFsError) as exc:
                    return _reject(path, "restore", f"路径被拒绝：{exc}")
            entry = snapshots.find(snapshot_id, wanted)
            if entry is None:
                return _error(
                    path or snapshot_id,
                    "restore",
                    f"找不到快照 {snapshot_id}"
                    + (f" 中与 {wanted} 匹配的文件。" if wanted else "。"),
                )
        elif path:
            try:
                target = fs.resolve(path)
            except (SandboxPathError, SandboxFsError) as exc:
                return _reject(path, "restore", f"路径被拒绝：{exc}")
            entry = snapshots.latest_for(target)
            if entry is None:
                return _error(
                    target, "restore", f"{_relpath(target, root)} 没有任何留底，无法回滚。"
                )
        else:
            entries = snapshots.list(limit=1)
            if not entries:
                return _error("", "restore", "工作区里还没有任何快照，无法回滚。")
            entry = entries[0]

        result = snapshots.restore(entry)
        if not result.ok:
            return _error(result.path, "restore", result.message)

        artifact = FileArtifact(
            path=result.path,
            action="restore" if result.diff else "restore-noop",
            ok=True,
            added=result.added,
            removed=result.removed,
            diff=result.diff,
            snapshot_id=result.snapshot_id,
        )
        note = (
            f"（回滚前的状态已留底为 {result.undo_snapshot_id}）"
            if result.undo_snapshot_id
            else ""
        )
        text = f"[文件] {reason}\n{result.message}{note}"
        return pack(f"{text}\n{result.diff}" if result.diff else text, artifact)

    tools: list[BaseTool] = [
        StructuredTool.from_function(
            func=_read, name=READ_TOOL_NAME, description=READ_DESCRIPTION, args_schema=ReadInput
        )
    ]
    if allow_write:
        tools += [
            StructuredTool.from_function(
                func=_write,
                name=WRITE_TOOL_NAME,
                description=WRITE_DESCRIPTION,
                args_schema=WriteInput,
            ),
            StructuredTool.from_function(
                func=_edit,
                name=EDIT_TOOL_NAME,
                description=EDIT_DESCRIPTION,
                args_schema=EditInput,
            ),
            StructuredTool.from_function(
                func=_restore,
                name=RESTORE_TOOL_NAME,
                description=RESTORE_DESCRIPTION,
                args_schema=RestoreInput,
            ),
        ]
    return tools
