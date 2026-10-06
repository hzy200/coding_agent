"""审计日志：按天一个 JSONL 文件，追加写。

设计取舍
--------
- **失败就报错，不静默丢弃。** 这个项目的卖点之一就是可审计，
  一条悄悄丢掉的记录比一次失败的运行更糟。写失败会抛 `AuditError`，
  由 runtime 转成 `RunFailed` 事件。
- **参数先脱敏再落库。** `file_write` 的 content 可能是整个文件，
  原样写进日志会让日志膨胀且泄露内容，因此统一截断。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from coding_agent.audit.models import AuditRecord

MAX_ARG_CHARS = 400
MAX_DETAIL_CHARS = 2_000

# 当天审计文件的命名：`<date>.jsonl`（首片）与 `<date>.<n>.jsonl`（轮转片）
_PIECE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.(\d+))?\.jsonl$")


def audit_files_by_day(directory: str | Path) -> dict[str, list[Path]]:
    """审计目录下的所有片段，**按天分组**，组内按片号升序（首片在前）。

    要分组而不是平铺，是因为「回溯最近 N 天」和「取最近 N 个文件」在启用轮转
    （`audit_max_mb > 0`）之后不是一回事：一天会产出 `<date>.1.jsonl`、
    `<date>.2.jsonl`…后者会被轮转片吃掉配额，更早的会话就静默消失了。
    """
    root = Path(directory)
    if not root.exists():
        return {}
    grouped: dict[str, list[tuple[int, Path]]] = {}
    for candidate in root.glob("*.jsonl"):
        match = _PIECE_RE.match(candidate.name)
        if not match:
            continue
        grouped.setdefault(match.group(1), []).append((int(match.group(2) or 0), candidate))
    return {
        day: [path for _, path in sorted(pieces, key=lambda item: item[0])]
        for day, pieces in grouped.items()
    }


def _next_piece(day: str, path: Path) -> Path:
    """`path` 之后的下一片：`<day>.jsonl` → `<day>.1.jsonl` → `<day>.2.jsonl`。"""
    match = _PIECE_RE.match(path.name)
    index = int(match.group(2) or 0) if match else 0
    return path.with_name(f"{day}.{index + 1}.jsonl")


class AuditError(RuntimeError):
    """审计写入失败。"""


def sanitize_args(args: dict, max_chars: int = MAX_ARG_CHARS) -> dict:
    """截断过长的参数值（尤其是 file_write 的 content）。"""
    sanitized: dict = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > max_chars:
            sanitized[key] = f"{value[:max_chars]}…<已截断，原长 {len(value)}>"
        else:
            sanitized[key] = value
    return sanitized


def truncate(text: str, max_chars: int = MAX_DETAIL_CHARS) -> str:
    return text if len(text) <= max_chars else f"{text[:max_chars]}…<已截断，原长 {len(text)}>"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuditLogger:
    """按天分文件的追加式审计日志。

    `max_bytes > 0` 时按大小轮转：当天写满一片就开下一片（`<date>.1.jsonl`、
    `<date>.2.jsonl`…），避免单文件无上限增长。读取用 `files_today()` 把当天的
    所有片段拼起来。
    """

    def __init__(
        self, directory: str | Path, *, enabled: bool = True, max_bytes: int = 0
    ) -> None:
        self.directory = Path(directory)
        self.enabled = enabled
        self.max_bytes = max_bytes
        # 「当天该写哪一片」的缓存。只在跨天或需要换片时才重新扫目录 ——
        # 一个任务上百条记录，每条都 glob 一次纯属浪费（见 C5）。
        self._active: tuple[str, Path] | None = None
        # 目录只需建一次；运行中途被删掉的话下一次 open 会失败并照常报错
        self._dir_ready = False

    def _day(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    @property
    def path(self) -> Path:
        """当天首个文件（也是未启用轮转时的唯一文件）。"""
        return self.directory / f"{self._day()}.jsonl"

    def files_today(self) -> list[Path]:
        """当天全部片段，按片号升序（首片在前）。

        这是**给外部读**用的（TUI 的 `/audit` 要列出当天的每一片），所以不缓存。
        写入侧走 `_active_path()`，那里才做缓存。
        """
        return audit_files_by_day(self.directory).get(self._day()) or [self.path]

    def _active_path(self) -> Path:
        """当前应写入的片段；未启用轮转或未写满时就是当天首个文件。

        缓存的是"最近写过的那一片"，判满只做一次 `stat()`（比扫目录便宜得多），
        跨天或写满时才推进片号。外部另起了更高的片号时也由 stat 循环兜住。
        """
        day = self._day()
        if self.max_bytes <= 0:
            return self.directory / f"{day}.jsonl"

        known_day, target = self._active or ("", None)
        if target is None or known_day != day:
            target = self.files_today()[-1]
        while target.exists() and target.stat().st_size >= self.max_bytes:
            target = _next_piece(day, target)
        self._active = (day, target)
        return target

    def write(self, record: AuditRecord) -> None:
        if not self.enabled:
            return
        target: Path | None = None
        try:
            if not self._dir_ready:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._dir_ready = True
            target = self._active_path()
            line = record.model_dump_json(exclude_none=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            raise AuditError(f"审计日志写入失败（{target or self.path}）：{exc}") from exc


def read_records(
    path: str | Path,
    *,
    thread_id: str | None = None,
    limit: int = 50,
) -> list[AuditRecord]:
    """读取审计记录。

    损坏的行会被跳过而不是让整个查询失败 —— 日志是用来事后排查的，
    不能因为一行坏数据就读不出来。
    """
    file_path = Path(path)
    if not file_path.exists():
        return []

    records: list[AuditRecord] = []
    with file_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = AuditRecord.model_validate_json(line)
            except ValueError:
                continue
            if thread_id and record.thread_id != thread_id:
                continue
            records.append(record)

    return records[-limit:] if limit > 0 else records


def read_records_many(
    paths: list[Path], *, thread_id: str | None = None, limit: int = 50
) -> list[AuditRecord]:
    """按顺序读多个审计文件（如当天的轮转片段），合并后再取最近 limit 条。

    片段是同一天的、按片号升序传入，所以拼接即时间序。
    """
    records: list[AuditRecord] = []
    for path in paths:
        records.extend(read_records(path, thread_id=thread_id, limit=0))
    return records[-limit:] if limit > 0 else records
