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

    def _day(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    @property
    def path(self) -> Path:
        """当天首个文件（也是未启用轮转时的唯一文件）。"""
        return self.directory / f"{self._day()}.jsonl"

    def files_today(self) -> list[Path]:
        """当天全部片段，按片号升序（首片在前）。"""
        day = self._day()
        if not self.directory.exists():
            return [self.path]
        pieces: list[tuple[int, Path]] = []
        for candidate in self.directory.glob(f"{day}*.jsonl"):
            match = _PIECE_RE.match(candidate.name)
            if match and match.group(1) == day:
                pieces.append((int(match.group(2) or 0), candidate))
        pieces.sort(key=lambda item: item[0])
        return [p for _, p in pieces] or [self.path]

    def _active_path(self) -> Path:
        """当前应写入的片段；未启用轮转或未写满时就是当天首个文件。"""
        if self.max_bytes <= 0:
            return self.path
        latest = self.files_today()[-1]
        if latest.exists() and latest.stat().st_size >= self.max_bytes:
            match = _PIECE_RE.match(latest.name)
            index = int(match.group(2) or 0) if match else 0
            return self.directory / f"{self._day()}.{index + 1}.jsonl"
        return latest

    def write(self, record: AuditRecord) -> None:
        if not self.enabled:
            return
        target = self._active_path()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            line = record.model_dump_json(exclude_none=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            raise AuditError(f"审计日志写入失败（{target}）：{exc}") from exc


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
