"""文件快照：改写前的留底，回滚的依据。

布局（工作区内）：

    <工作区>/.agent/backups/<snapshot_id>/<工作区相对路径>

`snapshot_id` 形如 `20261003T024018123456-a1b2c3`（时间戳精确到微秒），
前缀让目录名天然按时间排序，因此**不需要额外的索引文件** ——
列目录就能按新旧找到某个文件的历史版本。

恢复语义是「先留底再覆盖」：回滚前把当前内容也存一份，
所以回滚本身也是可回滚的，不会把用户当下的改动直接抹掉。
"""

from __future__ import annotations

import itertools
import posixpath
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from coding_agent.diffing import count_changes, unified_diff
from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.wsl_exec import WslSandbox

AGENT_STATE_DIRNAME = ".agent"
BACKUP_DIRNAME = f"{AGENT_STATE_DIRNAME}/backups"

ACTION_RESTORE = "restore"

# 同一时钟刻度内生成多个 id 时，用它保证严格递增（进程内有效）
_SNAPSHOT_SEQ = itertools.count()


def _new_snapshot_id() -> str:
    """按时间排序的快照 id：`YYYYmmddTHHMMSSffffff<seq3>-<uuid6>`。

    必须**严格可排序**：`SnapshotStore.list` 靠 id 的字典序判断新旧。只用秒级
    时间戳时，同一秒内的多次留底会退化成按随机 uuid 排序，`latest_for` /
    "最老一次" 会选错版本。因此时间戳精确到微秒，并再追加一个进程内递增序号
    —— 光靠时间戳不够：系统时钟粒度可能让同一刻度内生成多个相同时间戳。
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    seq = next(_SNAPSHOT_SEQ) % 1000
    return f"{stamp}{seq:03d}-{uuid4().hex[:6]}"


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    snapshot_id: str
    path: str  # 工作区相对路径


@dataclass(slots=True)
class RestoreResult:
    ok: bool
    path: str = ""
    snapshot_id: str = ""
    # 回滚前为当前内容留下的新快照，可用于「撤销这次撤销」
    undo_snapshot_id: str | None = None
    added: int = 0
    removed: int = 0
    diff: str = ""
    message: str = ""


def _relpath(path: str, root: str) -> str:
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


class SnapshotStore:
    """工作区内的文件快照。"""

    def __init__(self, sandbox: WslSandbox, fs: SandboxFs, root: str) -> None:
        self._sandbox = sandbox
        self._fs = fs
        self.root = root

    @property
    def backup_root(self) -> str:
        return posixpath.join(self.root, BACKUP_DIRNAME)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save(self, path: str, original: str) -> str:
        """把 path 当前的内容留底，返回 snapshot_id。"""
        snapshot_id = _new_snapshot_id()
        self._write(snapshot_id, _relpath(path, self.root), original)
        return snapshot_id

    def _write(self, snapshot_id: str, relpath: str, content: str) -> None:
        target = posixpath.join(self.backup_root, snapshot_id, relpath.lstrip("/"))
        self._fs.write_text(target, content)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list(self, *, limit: int = 0) -> list[SnapshotEntry]:
        """按时间倒序列出快照（每个快照里的每个文件算一条）。"""
        script = (
            f"if [ -d {shlex.quote(self.backup_root)} ]; then "
            f"find {shlex.quote(self.backup_root)} -type f -printf '%P\\n'; fi"
        )
        result = self._sandbox.run(script)
        if not result.ok:
            return []

        entries: list[SnapshotEntry] = []
        for line in result.stdout.splitlines():
            name = line.strip()
            snapshot_id, sep, relpath = name.partition("/")
            if sep and snapshot_id and relpath:
                entries.append(SnapshotEntry(snapshot_id=snapshot_id, path=relpath))

        entries.sort(key=lambda e: e.snapshot_id, reverse=True)
        return entries[:limit] if limit > 0 else entries

    def latest_for(self, path: str) -> SnapshotEntry | None:
        """某个文件最近一次的留底。"""
        try:
            relpath = _relpath(self._fs.resolve(path), self.root).lstrip("/")
        except (SandboxPathError, SandboxFsError):
            relpath = path.lstrip("/")

        for entry in self.list():
            if entry.path == relpath:
                return entry
        return None

    def find(self, snapshot_id: str, path: str | None = None) -> SnapshotEntry | None:
        for entry in self.list():
            if entry.snapshot_id != snapshot_id:
                continue
            if path is None or entry.path == path.lstrip("/"):
                return entry
        return None

    def read(self, entry: SnapshotEntry) -> str:
        target = posixpath.join(self.backup_root, entry.snapshot_id, entry.path)
        return self._fs.read_text(target, max_bytes=10_000_000)

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    def restore(self, entry: SnapshotEntry) -> RestoreResult:
        """把文件还原到该快照的内容；还原前先把当前内容留底。"""
        target = posixpath.join(self.root, entry.path)
        try:
            original = self.read(entry)
        except (SandboxFsError, SandboxPathError) as exc:
            return RestoreResult(ok=False, path=entry.path, message=f"快照内容读取失败：{exc}")

        current: str | None = None
        try:
            info = self._fs.stat(target)
            if info.exists and info.is_file:
                current = self._fs.read_text(target, max_bytes=10_000_000)
        except (SandboxFsError, SandboxPathError):
            current = None

        if current == original:
            return RestoreResult(
                ok=True,
                path=entry.path,
                snapshot_id=entry.snapshot_id,
                message="当前内容与该快照一致，无需回滚。",
            )

        # 先给「当前状态」留底，让回滚本身也可以被回滚
        undo_snapshot_id: str | None = None
        if current is not None:
            try:
                undo_snapshot_id = self.save(target, current)
            except (SandboxFsError, SandboxPathError) as exc:
                return RestoreResult(
                    ok=False,
                    path=entry.path,
                    message=f"回滚前的留底失败，已中止：{exc}",
                )

        try:
            self._fs.write_text(target, original)
        except (SandboxFsError, SandboxPathError) as exc:
            return RestoreResult(ok=False, path=entry.path, message=f"回滚写入失败：{exc}")

        diff = unified_diff(current or "", original, entry.path)
        added, removed = count_changes(diff)
        return RestoreResult(
            ok=True,
            path=entry.path,
            snapshot_id=entry.snapshot_id,
            undo_snapshot_id=undo_snapshot_id,
            added=added,
            removed=removed,
            diff=diff,
            message=f"已回滚 {entry.path} 到快照 {entry.snapshot_id}",
        )
