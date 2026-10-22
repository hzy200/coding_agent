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
from collections.abc import MutableMapping
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.diffing import count_changes, unified_diff
from coding_agent.sandbox.fs import SandboxFs, SandboxFsError, SandboxFsTooLarge
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.snapshots import MAX_OPERATIONAL_BYTES, SnapshotStore
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.security.read_policy import protected_read_reason
from coding_agent.security.secrets import redact_secrets
from coding_agent.security.write_policy import protected_write_reason
from coding_agent.tools.artifacts import CallArtifact, FileArtifact, pack

READ_TOOL_NAME = "file_read"
WRITE_TOOL_NAME = "file_write"
EDIT_TOOL_NAME = "file_edit"
RESTORE_TOOL_NAME = "file_restore"
MOVE_TOOL_NAME = "file_move"
COPY_TOOL_NAME = "file_copy"
DELETE_TOOL_NAME = "file_delete"
MKDIR_TOOL_NAME = "file_mkdir"
LIST_TOOL_NAME = "file_list"
EDIT_MANY_TOOL_NAME = "file_edit_many"

_STALE_READ_MESSAGE = (
    "文件在最后一次读取之后被其他进程修改过，已拒绝覆盖（避免丢掉别人的改动）。"
    "请先用 file_read 重新读取，再基于最新内容提交修改。"
)


def _changed_since_read(cached: bytes | None, current: str | None) -> bool:
    """模型读到写之间文件是否被外部改过。

    只用**模型自己读过的缓存**做比较；没有读取记录就不猜。
    这不是安全边界，而是防止 `file_write` 静默覆盖编辑器/构建工具的改动。
    """
    if current is None or cached is None:
        return False
    return current.encode("utf-8") != cached

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


class EditSpec(BaseModel):
    path: str = Field(description="文件路径")
    old_string: str = Field(
        min_length=1, description="要替换的原文，必须在当前文件中唯一"
    )
    new_string: str = Field(description="替换后的内容")
    replace_all: bool = Field(
        default=False, description="old_string 出现多次时是否全部替换"
    )


class EditManyInput(BaseModel):
    edits: list[EditSpec] = Field(
        min_length=1,
        max_length=20,
        description="按顺序应用的编辑；同一文件可以有多条",
    )
    reason: str = Field(description="批量修改的意图，一句话说明")


class RestoreInput(BaseModel):
    path: str | None = Field(
        default=None, description="要回滚的文件；省略则回滚最近一次文件改动"
    )
    snapshot_id: str | None = Field(
        default=None, description="指定快照 id；省略则用该文件最近一次留底"
    )
    reason: str = Field(description="回滚的意图，一句话说明")


class MoveInput(BaseModel):
    source: str = Field(description="要移动的文件路径")
    destination: str = Field(description="目标路径（可改名或换目录）")
    reason: str = Field(description="移动的意图，一句话说明")
    overwrite: bool = Field(
        default=False, description="目标已存在时是否覆盖（覆盖前会留底）"
    )


class CopyInput(BaseModel):
    source: str = Field(description="要复制的文件路径")
    destination: str = Field(description="目标路径")
    reason: str = Field(description="复制的意图，一句话说明")
    overwrite: bool = Field(
        default=False, description="目标已存在时是否覆盖（覆盖前会留底）"
    )


class DeleteInput(BaseModel):
    path: str = Field(description="要删除的文件路径（只支持普通文件）")
    reason: str = Field(description="删除的意图，一句话说明")


class MkdirInput(BaseModel):
    path: str = Field(description="要创建的目录路径（会自动创建父目录）")
    reason: str = Field(description="创建目录的意图，一句话说明")


class ListDirInput(BaseModel):
    path: str = Field(default=".", description="目录路径，默认工作区根目录")
    max_depth: int = Field(default=1, ge=1, le=5, description="递归深度")
    limit: int = Field(default=200, ge=1, le=1000, description="最多返回多少项")
    reason: str = Field(description="列目录的意图，一句话说明")


def _relpath(path: str, root: str) -> str:
    """展示用的工作区相对路径。"""
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def build_file_tools(
    settings: Settings,
    sandbox: WslSandbox,
    *,
    allow_write: bool,
    write_scope: list[str] | None = None,
    read_cache: MutableMapping[str, bytes] | None = None,
) -> list[BaseTool]:
    root = resolve_workspace(settings, sandbox)
    fs = SandboxFs(sandbox, root, cache=read_cache)
    snapshots = SnapshotStore(sandbox, fs, root)
    # 并发写 worker 的写时边界。None/空表示不限制（主 agent 的常规文件工具）。
    # 归一化与 orchestrator.graph 的 scope 口径保持一致：POSIX 相对路径、
    # 不带前导斜杠、目录不以 "/" 结尾。
    write_scope_norm = [
        str(item).strip().replace("\\", "/").strip("/")
        for item in (write_scope or [])
        if str(item).strip()
    ]
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

    def _scope_allows_rel(relpath: str) -> bool:
        if not write_scope_norm:
            return True
        rel = relpath.replace("\\", "/").lstrip("/")
        return any(
            rel == item or rel.startswith(item + "/")
            for item in write_scope_norm
        )

    def _scope_allows(target: str) -> bool:
        return _scope_allows_rel(_relpath(target, root))

    def _protected_reason(target: str) -> str | None:
        return protected_write_reason(
            _relpath(target, root), settings.write_deny_globs
        )

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

        protected = protected_read_reason(
            _relpath(target, root), settings.read_deny_globs
        )
        if protected:
            return _reject(path, "read", protected)

        # 词法路径可能是工作区内的符号链接（例如 safe.txt -> .env）。
        # 读保护必须看 realpath，否则链接会绕过保护。
        try:
            info = fs.stat(target)
        except (SandboxFsError, SandboxPathError):
            info = None
        if info is not None and info.real_path:
            real_protected = protected_read_reason(
                _relpath(info.real_path, root), settings.read_deny_globs
            )
            if real_protected:
                return _reject(path, "read", real_protected)

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

        # 内容级兜底：路径规则覆盖不到的配置（例如 application.yml 里的
        # 数据库密码）也要在进入模型上下文前脱敏。file_edit 仍按真实文件
        # 匹配，所以模型只是看不到值，不会把脱敏占位符写回文件。
        numbered = "\n".join(
            f"{start + i + 1:>6}\t{redact_secrets(line)}"
            for i, line in enumerate(window)
        )
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
        # 词法校验零 I/O：越界写在读任何东西之前就拒掉。
        try:
            target = fs.lexical(path)
        except SandboxPathError as exc:
            return _reject(path, "overwrite", f"路径被拒绝：{exc}")

        if not _scope_allows(target):
            return _reject(
                path,
                "overwrite",
                f"路径不在 write_scope 内：{_relpath(target, root)}。"
                f"允许写入：{', '.join(write_scope_norm)}",
            )
        protected = _protected_reason(target)
        if protected:
            return _reject(path, "overwrite", protected)

        # 一次调用拿到 realpath / 类型 / 大小 / 当前内容。新建时 original=None。
        cached = fs.cached_bytes(target)
        try:
            target, info, original = fs.read_for_update(
                target, max_bytes=internal_max_bytes
            )
        except SandboxPathError as exc:
            return _reject(path, "overwrite", f"路径被拒绝：{exc}")
        except SandboxFsTooLarge as exc:
            return _error(
                target,
                "overwrite",
                f"{exc}。无法读取原文件，拒绝覆盖。",
            )
        except SandboxFsError as exc:
            return _error(target, "overwrite", f"无法读取原文件，拒绝覆盖：{exc}")

        if _changed_since_read(cached, original):
            # 让下一次 file_read 真正回到磁盘，而不是继续返回旧缓存。
            fs.invalidate(target)
            return _error(target, "overwrite", _STALE_READ_MESSAGE)

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

        # 留底与目标写入合并成一次沙箱调用（write_many 内部 temp+rename）。
        # 新建也留底：写入 `.absent` 标记，回滚时据此删除文件。
        try:
            snapshot_id = snapshots.write_with_snapshot(
                target,
                content,
                original or "",
                existed=info.exists,
            )
        except (SandboxFsError, SandboxPathError) as exc:
            return _error(target, action, f"写入失败（已中止）：{exc}")
        written = len(content.encode("utf-8"))

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

        if not _scope_allows(target):
            return _reject(
                path,
                "edit",
                f"路径不在 write_scope 内：{_relpath(target, root)}。"
                f"允许写入：{', '.join(write_scope_norm)}",
            )
        protected = _protected_reason(target)
        if protected:
            return _reject(path, "edit", protected)

        cached = fs.cached_bytes(target)
        try:
            target, info, original = fs.read_for_update(
                target, max_bytes=internal_max_bytes
            )
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

        if not info.exists or original is None:
            return _error(
                target,
                "edit",
                f"{_relpath(target, root)} 不存在，无法编辑；请用 file_write 创建。",
            )
        if _changed_since_read(cached, original):
            fs.invalidate(target)
            return _error(target, "edit", _STALE_READ_MESSAGE)

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

        # 留底与目标写入合并成一次沙箱调用。
        try:
            snapshot_id = snapshots.write_with_snapshot(
                target, updated, original, existed=True
            )
        except (SandboxFsError, SandboxPathError) as exc:
            return _error(target, "edit", f"写入失败（已中止）：{exc}")

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

    def _edit_many(edits: list[Any], reason: str) -> str:
        """批量精确替换：先全量校验并算出结果，再用一次 write_many 落盘。"""
        states: dict[str, dict[str, Any]] = {}

        def failure(message: str) -> str:
            return pack(
                message,
                CallArtifact(
                    tool=EDIT_MANY_TOOL_NAME,
                    ok=False,
                    files=[
                        _relpath(item, root) for item in sorted(states)
                    ],
                ),
            )

        for index, raw in enumerate(edits, start=1):
            if hasattr(raw, "model_dump"):
                edit = raw.model_dump()
            else:
                edit = dict(raw or {})
            path = str(edit.get("path", ""))
            old = str(edit.get("old_string", ""))
            new = str(edit.get("new_string", ""))
            replace_all = bool(edit.get("replace_all", False))
            if not old:
                return failure(f"第 {index} 条编辑的 old_string 为空。")

            try:
                target = fs.lexical(path)
            except SandboxPathError as exc:
                return failure(f"第 {index} 条编辑路径被拒绝：{exc}")
            if not _scope_allows(target):
                return failure(
                    f"第 {index} 条编辑路径不在 write_scope 内："
                    f"{_relpath(target, root)}"
                )
            protected = _protected_reason(target)
            if protected:
                return failure(f"第 {index} 条编辑被拒绝：{protected}")

            state = states.get(target)
            if state is None:
                cached = fs.cached_bytes(target)
                try:
                    _, info, original = fs.read_for_update(
                        target, max_bytes=internal_max_bytes
                    )
                except SandboxPathError as exc:
                    return failure(
                        f"第 {index} 条编辑路径被拒绝：{exc}"
                    )
                except SandboxFsError as exc:
                    return failure(f"第 {index} 条编辑读取失败：{exc}")
                if not info.exists or original is None:
                    return failure(
                        f"第 {index} 条编辑的文件不存在："
                        f"{_relpath(target, root)}"
                    )
                if _changed_since_read(cached, original):
                    fs.invalidate(target)
                    return failure(
                        f"第 {index} 条编辑：{_relpath(target, root)} "
                        f"在读取后被其他进程修改。{_STALE_READ_MESSAGE}"
                    )
                state = {
                    "target": target,
                    "original": original,
                    "current": original,
                }
                states[target] = state

            current = state["current"]
            hits = current.count(old)
            if hits == 0:
                return failure(
                    f"第 {index} 条编辑：old_string 在 "
                    f"{_relpath(target, root)} 中未出现，整批未修改。"
                )
            if hits > 1 and not replace_all:
                return failure(
                    f"第 {index} 条编辑：old_string 在 "
                    f"{_relpath(target, root)} 中出现了 {hits} 次，"
                    "请扩展上下文或设置 replace_all=true，整批未修改。"
                )
            if old == new:
                return failure(f"第 {index} 条编辑的 old/new 相同。")
            state["current"] = (
                current.replace(old, new)
                if replace_all
                else current.replace(old, new, 1)
            )

        entries: list[tuple[str, str, str, bool]] = []
        for target, state in states.items():
            updated = state["current"]
            original = state["original"]
            if updated == original:
                continue
            relpath = _relpath(target, root)
            if not _syntax_error(original, relpath):
                problem = _syntax_error(updated, relpath)
                if problem:
                    return failure(
                        f"语法错误，整批未修改：{relpath}：{problem}"
                    )
            entries.append((target, updated, original, True))

        if not entries:
            return failure("所有编辑都没有产生实际改动，未写盘。")

        try:
            snapshot_ids = snapshots.write_many_with_snapshots(entries)
        except (SandboxFsError, SandboxPathError) as exc:
            return failure(f"批量写入失败（已中止）：{exc}")

        blocks: list[str] = []
        artifact_files: list[str] = []
        file_changes: list[dict[str, Any]] = []
        total_added = 0
        total_removed = 0
        for target, updated, original, _existed in entries:
            relpath = _relpath(target, root)
            diff = unified_diff(original, updated, relpath)
            added, removed = count_changes(diff)
            total_added += added
            total_removed += removed
            artifact_files.append(relpath)
            snapshot_id = snapshot_ids.get(target, "")
            file_changes.append(
                {
                    "path": relpath,
                    "action": "edit",
                    "added": added,
                    "removed": removed,
                    "diff": diff,
                    "snapshot_id": snapshot_id,
                }
            )
            blocks.append(f"- {relpath}（备份 {snapshot_id}）\n{diff}")
        artifact = CallArtifact(
            tool=EDIT_MANY_TOOL_NAME,
            ok=True,
            files=artifact_files,
            diff="\n".join(blocks),
            file_changes=file_changes,
        )
        text = (
            f"[文件] {reason}\n批量修改 {len(entries)} 个文件"
            f"（+{total_added} -{total_removed}）\n" + "\n".join(blocks)
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

        if not _scope_allows_rel(entry.path):
            return _reject(
                entry.path,
                "restore",
                f"快照路径不在 write_scope 内：{entry.path}。"
                f"允许写入：{', '.join(write_scope_norm)}",
            )

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

    def _move_or_copy(
        source: str,
        destination: str,
        reason: str,
        *,
        overwrite: bool,
        move: bool,
    ) -> str:
        action = "move" if move else "copy"
        try:
            src = fs.lexical(source)
            dst = fs.lexical(destination)
        except SandboxPathError as exc:
            return _reject(source, action, f"路径被拒绝：{exc}")
        if src == dst:
            return _error(dst, action, "源路径与目标路径相同，未做任何修改。")
        for candidate in (src, dst):
            if not _scope_allows(candidate):
                return _reject(
                    source,
                    action,
                    f"路径不在 write_scope 内：{_relpath(candidate, root)}。"
                    f"允许写入：{', '.join(write_scope_norm)}",
                )
            protected = _protected_reason(candidate)
            if protected:
                return _reject(source, action, protected)

        src_cached = fs.cached_bytes(src)
        try:
            _, src_info, src_text = fs.read_for_update(
                src, max_bytes=internal_max_bytes
            )
        except (SandboxPathError, SandboxFsError) as exc:
            return _error(src, action, f"无法读取源文件：{exc}")
        if not src_info.exists or src_text is None:
            return _error(src, action, f"源文件不存在：{_relpath(src, root)}")
        if _changed_since_read(src_cached, src_text):
            fs.invalidate(src)
            return _error(src, action, _STALE_READ_MESSAGE)

        dst_cached = fs.cached_bytes(dst)
        try:
            _, dst_info, dst_text = fs.read_for_update(
                dst, max_bytes=internal_max_bytes
            )
        except (SandboxPathError, SandboxFsError) as exc:
            return _error(dst, action, f"无法读取目标路径：{exc}")
        if dst_info.exists:
            if not overwrite:
                return _error(
                    dst,
                    action,
                    f"目标已存在：{_relpath(dst, root)}。"
                    "如确需覆盖，请显式设置 overwrite=true（覆盖前会留底）。",
                )
            if _changed_since_read(dst_cached, dst_text):
                fs.invalidate(dst)
                return _error(dst, action, _STALE_READ_MESSAGE)

        source_snapshot = ""
        try:
            if move:
                # 源留底 + 目标留底 + 目标写入合并成一次 write_many。
                source_snapshot, dest_snapshot = snapshots.move_with_snapshot(
                    src,
                    dst,
                    src_text,
                    dst_text or "",
                    dest_existed=dst_info.exists,
                )
            else:
                dest_snapshot = snapshots.write_with_snapshot(
                    dst,
                    src_text,
                    dst_text or "",
                    existed=dst_info.exists,
                )
        except (SandboxFsError, SandboxPathError) as exc:
            return _error(dst, action, f"写入目标失败：{exc}")

        if move:
            try:
                # 源文件已经读过、确认是普通文件，跳过重复 stat。
                fs.remove(src, known_file=True)
            except (SandboxFsError, SandboxPathError) as exc:
                return _error(
                    dst,
                    action,
                    f"目标已写入，但删除源文件失败：{exc}。"
                    f"源文件仍在 {_relpath(src, root)}，请手动确认。",
                )

        diff = unified_diff("", src_text, _relpath(dst, root))
        added, removed = count_changes(diff)
        note = f"（源留底 {source_snapshot}）" if source_snapshot else ""
        artifact = FileArtifact(
            path=_relpath(dst, root),
            action=action,
            ok=True,
            bytes_written=len(src_text.encode("utf-8")),
            added=added,
            removed=removed,
            diff=diff,
            snapshot_id=source_snapshot or dest_snapshot,
        )
        verb = "已移动" if move else "已复制"
        text = (
            f"[文件] {reason}\n{verb} {_relpath(src, root)} → "
            f"{_relpath(dst, root)}（目标留底 {dest_snapshot}）{note}"
        )
        return pack(f"{text}\n{diff}" if diff else text, artifact)

    def _delete(path: str, reason: str) -> str:
        try:
            target = fs.lexical(path)
        except SandboxPathError as exc:
            return _reject(path, "delete", f"路径被拒绝：{exc}")
        if not _scope_allows(target):
            return _reject(
                path,
                "delete",
                f"路径不在 write_scope 内：{_relpath(target, root)}。"
                f"允许写入：{', '.join(write_scope_norm)}",
            )
        protected = _protected_reason(target)
        if protected:
            return _reject(path, "delete", protected)

        cached = fs.cached_bytes(target)
        try:
            _, info, original = fs.read_for_update(
                target, max_bytes=internal_max_bytes
            )
        except (SandboxPathError, SandboxFsError) as exc:
            return _error(target, "delete", f"无法读取文件：{exc}")
        if not info.exists or original is None:
            return _error(target, "delete", f"文件不存在：{_relpath(target, root)}")
        if _changed_since_read(cached, original):
            fs.invalidate(target)
            return _error(target, "delete", _STALE_READ_MESSAGE)

        try:
            snapshot_id = snapshots.save(target, original)
            fs.remove(target, known_file=True)
        except (SandboxFsError, SandboxPathError) as exc:
            return _error(target, "delete", f"删除失败：{exc}")

        removed = len(original.splitlines())
        artifact = FileArtifact(
            path=_relpath(target, root),
            action="delete",
            ok=True,
            removed=removed,
            snapshot_id=snapshot_id,
        )
        text = (
            f"[文件] {reason}\n已删除 {_relpath(target, root)}"
            f"（备份 {snapshot_id}，可用 file_restore 恢复）"
        )
        return pack(text, artifact)

    def _mkdir(path: str, reason: str) -> str:
        try:
            target = fs.lexical(path)
        except SandboxPathError as exc:
            return _reject(path, "mkdir", f"路径被拒绝：{exc}")
        if not _scope_allows(target):
            return _reject(
                path,
                "mkdir",
                f"路径不在 write_scope 内：{_relpath(target, root)}。"
                f"允许写入：{', '.join(write_scope_norm)}",
            )
        protected = _protected_reason(target)
        if protected:
            return _reject(path, "mkdir", protected)
        try:
            fs.mkdir(target)
        except (SandboxPathError, SandboxFsError) as exc:
            return _error(target, "mkdir", f"创建目录失败：{exc}")
        artifact = FileArtifact(
            path=_relpath(target, root), action="mkdir", ok=True
        )
        return pack(
            f"[文件] {reason}\n已创建目录 {_relpath(target, root)}",
            artifact,
        )

    def _list_dir(
        path: str = ".",
        max_depth: int = 1,
        limit: int = 200,
        reason: str = "",
    ) -> str:
        try:
            target = fs.lexical(path)
        except SandboxPathError as exc:
            return _reject(path, "list", f"路径被拒绝：{exc}")
        try:
            entries = fs.list_dir(
                target, max_depth=max_depth, limit=limit
            )
        except (SandboxPathError, SandboxFsError) as exc:
            return _error(target, "list", str(exc))
        kind_label = {"d": "DIR ", "f": "FILE", "l": "LINK"}
        lines = [
            f"{kind_label.get(kind, kind)} {rel}"
            for kind, rel in entries
        ]
        body = "\n".join(lines) if lines else "（空目录）"
        artifact = FileArtifact(
            path=_relpath(target, root),
            action="list",
            ok=True,
            lines_read=len(lines),
        )
        return pack(
            f"[文件] {reason}\n列出 {_relpath(target, root)}"
            f"（{len(lines)} 项）\n{body}",
            artifact,
        )

    tools: list[BaseTool] = [
        StructuredTool.from_function(
            func=_read, name=READ_TOOL_NAME, description=READ_DESCRIPTION, args_schema=ReadInput
        ),
        StructuredTool.from_function(
            func=_list_dir,
            name=LIST_TOOL_NAME,
            description=(
                "列出工作区目录内容（类型 + 相对路径），不递归时可快速了解结构。只读。"
            ),
            args_schema=ListDirInput,
        ),
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
                func=_edit_many,
                name=EDIT_MANY_TOOL_NAME,
                description=(
                    "一次提交多条精确替换（可跨多个文件，最多 20 条）。"
                    "所有编辑先全量校验，任一失败则整批不写盘；每个文件单独留底。"
                ),
                args_schema=EditManyInput,
            ),
            StructuredTool.from_function(
                func=_restore,
                name=RESTORE_TOOL_NAME,
                description=RESTORE_DESCRIPTION,
                args_schema=RestoreInput,
            ),
            StructuredTool.from_function(
                func=lambda source, destination, reason, overwrite=False: _move_or_copy(
                    source,
                    destination,
                    reason,
                    overwrite=overwrite,
                    move=True,
                ),
                name=MOVE_TOOL_NAME,
                description=(
                    "移动/重命名工作区内的文件。源文件会先留底，目标已存在时需 "
                    "overwrite=true。只支持普通文件。"
                ),
                args_schema=MoveInput,
            ),
            StructuredTool.from_function(
                func=lambda source, destination, reason, overwrite=False: _move_or_copy(
                    source,
                    destination,
                    reason,
                    overwrite=overwrite,
                    move=False,
                ),
                name=COPY_TOOL_NAME,
                description=(
                    "复制工作区内的文件。目标已存在时需 overwrite=true，覆盖前会留底。"
                ),
                args_schema=CopyInput,
            ),
            StructuredTool.from_function(
                func=_delete,
                name=DELETE_TOOL_NAME,
                description=(
                    "删除工作区内的普通文件。删除前会留底，可用 file_restore 恢复。"
                    "删除需要用户确认。"
                ),
                args_schema=DeleteInput,
            ),
            StructuredTool.from_function(
                func=_mkdir,
                name=MKDIR_TOOL_NAME,
                description="创建工作区内的目录（自动创建父目录）。",
                args_schema=MkdirInput,
            ),
        ]
    return tools
