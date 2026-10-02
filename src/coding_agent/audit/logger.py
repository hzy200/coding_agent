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

from datetime import UTC, datetime
from pathlib import Path

from coding_agent.audit.models import AuditRecord

MAX_ARG_CHARS = 400
MAX_DETAIL_CHARS = 2_000


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
    """按天分文件的追加式审计日志。"""

    def __init__(self, directory: str | Path, *, enabled: bool = True) -> None:
        self.directory = Path(directory)
        self.enabled = enabled

    @property
    def path(self) -> Path:
        return self.directory / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"

    def write(self, record: AuditRecord) -> None:
        if not self.enabled:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            line = record.model_dump_json(exclude_none=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            raise AuditError(f"审计日志写入失败（{self.path}）：{exc}") from exc


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
