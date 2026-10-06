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

import ast

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.diffing import count_changes, unified_diff
from coding_agent.sandbox.fs import SandboxFs, SandboxFsError, SandboxFsTooLarge
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.snapshots import MAX_OPERATIONAL_BYTES, SnapshotStore
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import FileArtifact, pack

READ_TOOL_NAME = "file_read"
WRITE_TOOL_NAME = "file_write"
EDIT_TOOL_NAME = "file_edit"
RESTORE_TOOL_NAME = "file_restore"

DEFAULT_READ_LINES = 400

# limit 只有下界时，模型传 `limit=100000` 就能把整个文件（上限 2MB）一次性灌进
# 上下文 —— 而上下文裁剪**不碰最近 keep_recent 条**，任何裁剪都拦不住它。
# 这里是一道理智闸门；真正的上下文保证在 `llm/context.py` 的单条消息上限。
MAX_READ_LINES = 2_000

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
注意：回滚一次**新建**（file_write 建出来的文件）会**删掉**那个文件 ——
留底时它并不存在，还原成空文件只是留下一堆删不掉的垃圾。
删除前同样会留底，所以这次删除也可以再回滚。
"""


class ReadInput(BaseModel):
    path: str = Field(description="文件路径，工作区内相对路径或绝对路径")
    reason: str = Field(description="读取这个文件的意图，一句话说明")
    offset: int | None = Field(default=None, description="起始行号（从 1 开始），省略则从头读")
    limit: int | None = Field(
        default=None,
        ge=1,
        le=MAX_READ_LINES,
        description=f"最多读取行数（{DEFAULT_READ_LINES} 以内效果最好，上限 {MAX_READ_LINES}）",
    )


class WriteInput(BaseModel):
    path: str = Field(description="文件路径，工作区内相对路径或绝对路径")
    content: str = Field(description="写入的完整内容")
    reason: str = Field(description="写这个文件的意图，一句话说明")


class EditInput(BaseModel):
    path: str = Field(description="文件路径，工作区内相对路径或绝对路径")
    old_string: str = Field(
        min_length=1,
        description="要被替换掉的原文，**不能为空**，必须与文件内容逐字符一致且唯一",
    )
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
    # 喂进模型上下文的读上限
    max_bytes = settings.max_file_read_bytes
    # 「读出来改完再写回」的上限。这类读取的内容**不进上下文**（模型只看到 diff），
    # 用读限卡它属于口径错配：2MB 以上的文件就彻底改不了，模型只能退回 shell，
    # 而经 shell 的改动不留快照、无法回滚 —— 正好绕开「可回滚」这条底线。
    internal_max_bytes = MAX_OPERATIONAL_BYTES

    def _syntax_error(text: str, relpath: str) -> str:
        """`.py` 的语法预校验；有问题就返回一句可执行的说明，否则空串。

        **为什么在落盘前查**：原先的顺序是「先写下去 → review 事后 `ast.parse` →
        不合格再 repair 重做」，白烧一轮工具预算与一次模型往返。而语法错是这里
        **最便宜就能确定**的一类错误（纯解析、零依赖、零 I/O），没有必要等到事后。

        只查 `.py`：沙箱里只有 `python3`，别的语言查不了 —— 能确定做到的那一点，
        比做不到的承诺有用。

        **判据是「有没有变坏」，不是「是不是好的」**：调用点只在原文件本来就是
        合法的时候才拒。否则模型分步修一个已经坏掉的文件时会被自己的中间状态堵死。
        """
        if not relpath.endswith(".py"):
            return ""
        try:
            ast.parse(text, filename=relpath)
        except SyntaxError as exc:
            where = f"第 {exc.lineno} 行" if exc.lineno else "（无法定位行号）"
            return f"{where}：{exc.msg}"
        return ""

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
        # 词法归一化零 I/O。紧接着的 read_text 会完成 realpath 校验，
        # 再单独 resolve 一次等于为同一次读付两次 wsl.exe 进程启动。
        try:
            target = fs.lexical(path)
        except SandboxPathError as exc:
            return _reject(path, "read", f"路径被拒绝：{exc}")

        try:
            text = fs.read_text(target, max_bytes=max_bytes)
        except SandboxPathError as exc:
            return _reject(path, "read", f"路径被拒绝：{exc}")
        except SandboxFsTooLarge as exc:
            # 只有这一种失败有退路，也只有 file_read 有 offset/limit。
            # 曾经任何错误都拼上这句，文件不存在时也让人去分段读。
            return _error(target, "read", f"{exc}。请用 offset/limit 分段读取。")
        except SandboxFsError as exc:
            return _error(target, "read", str(exc))

        lines = text.splitlines()
        total = len(lines)
        start = max((offset or 1) - 1, 0)
        count = limit if limit is not None else DEFAULT_READ_LINES
        window = lines[start : start + count]

        numbered = "\n".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(window))
        header = f"文件 {_relpath(target, root)}（共 {total} 行"
        if window and (start or len(window) < total):
            header += f"，显示第 {start + 1}-{start + len(window)} 行"
        header += f"）\n{reason}"

        artifact = FileArtifact(
            path=_relpath(target, root),
            action="read",
            ok=True,
            lines_read=len(window),
            lines_total=total,
        )
        if window:
            return pack(f"{header}\n{numbered}", artifact)
        if total == 0:
            return pack(f"{header}\n（文件为空）", artifact)
        # 只有真的没有内容时才能说「空」：offset 越界时说成空文件，
        # 模型会以为该文件可以整份覆盖写入。
        return pack(
            f"{header}\n（从第 {start + 1} 行起没有内容可显示，该文件共 {total} 行，"
            f"未读取到任何内容）",
            artifact,
        )

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
                original = fs.read_text(target, max_bytes=internal_max_bytes)
            except SandboxFsError as exc:
                return _error(target, "overwrite", f"无法读取原文件，拒绝覆盖：{exc}")

        action = "overwrite" if info.exists else "create"
        # 语法预校验：**只在原文件本来就合法时**才拒（原本就坏的说明模型正在
        # 分步修它，中间状态不合法是正常的）。放在留底之前 —— 被拒的写入不该
        # 产生"回滚点"，否则 `.agent/backups/` 会被无谓的失败尝试塞满。
        relpath = _relpath(target, root)
        if not (original is not None and _syntax_error(original, relpath)):
            problem = _syntax_error(content, relpath)
            if problem:
                return _error(
                    target,
                    action,
                    f"语法错误，已拒绝写入（文件未改动）：{problem}。"
                    f"请修正后重新提交完整内容 —— 需要分步改一个已经坏掉的文件时，"
                    f"先把它改回合法状态。",
                )

        snapshot_id: str | None = None
        if original is not None:
            try:
                snapshot_id = snapshots.save(target, original)
            except (SandboxFsError, SandboxPathError) as exc:
                return _error(target, action, f"备份失败，已中止写入：{exc}")
        elif not info.exists:
            # 新建也要留底：否则「回滚最近一次改动」对新建的文件根本不成立，
            # 而新建恰恰是最容易想撤销的一类改动（文件放错位置、内容整个不对）。
            # 留的是「当时不存在」这个事实，回滚时据此删掉它。
            try:
                snapshot_id = snapshots.save(target, "", existed=False)
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
        # 空 old_string 会命中 `str.replace("", x)` 的逐字符插入语义
        # （`"ab".count("") == 3`，能让唯一性检查失效），必须在这里挡下。
        # schema 上的 min_length=1 是第一道，这里是直接调用时的兜底。
        if not old_string:
            return _error(path, "edit", "old_string 不能为空，未做任何修改。")
        try:
            target = fs.lexical(path)
        except SandboxPathError as exc:
            return _reject(path, "edit", f"路径被拒绝：{exc}")

        try:
            original = fs.read_text(target, max_bytes=internal_max_bytes)
        except SandboxPathError as exc:
            return _reject(path, "edit", f"路径被拒绝：{exc}")
        except SandboxFsTooLarge as exc:
            # 这里不能沿用 file_read 那句「请用 offset/limit 分段读取」——
            # file_edit 没有这两个参数，那么说只会把模型推向 shell
            return _error(
                target,
                "edit",
                f"{exc}。file_edit 需要整份读出、精确替换后再写回，改不了这么大的文件。"
                f"可以退回 shell 修改，但经 shell 的改动不留快照、无法回滚。",
            )
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

        # 语法预校验：同样只在原文件**本来就合法**时才拒 ——
        # 模型分步修一个已经坏掉的文件时，中间状态不合法是正常的。
        relpath = _relpath(target, root)
        if not _syntax_error(original, relpath):
            problem = _syntax_error(updated, relpath)
            if problem:
                return _error(
                    target,
                    "edit",
                    f"语法错误，已拒绝修改（文件未改动）：{problem}。"
                    f"一次编辑之后的文件必须是合法 Python —— 需要跨多处改动时，"
                    f"把 old_string 扩展到覆盖整段、一次改完。",
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
                    # 只为拿到与快照条目同一口径的相对路径做比对，
                    # 词法归一化就够 —— resolve 会为它多跑一趟 wsl.exe
                    wanted = _relpath(fs.lexical(path), root).lstrip("/")
                except SandboxPathError as exc:
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
                # 同上：只需要比对的口径，不需要沙箱里那次 realpath 校验
                target = fs.lexical(path)
            except SandboxPathError as exc:
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
