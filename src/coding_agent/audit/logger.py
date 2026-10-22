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

import hashlib
import json
import re
import secrets as _secrets
from datetime import UTC, datetime
from pathlib import Path

from coding_agent.audit.models import AuditRecord
from coding_agent.filelock import FileLock
from coding_agent.security.secrets import redact_secrets

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


def _sanitize_value(value, max_chars: int):
    if isinstance(value, str):
        return truncate(value, max_chars)
    if isinstance(value, dict):
        return {
            str(key): _sanitize_value(item, max_chars)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, max_chars) for item in value]
    return value


def sanitize_args(args: dict, max_chars: int = MAX_ARG_CHARS) -> dict:
    """先脱敏 secret，再截断过长参数（尤其是 file_write 的 content）。"""
    return {
        str(key): _sanitize_value(value, max_chars)
        for key, value in args.items()
    }


def truncate(text: str, max_chars: int = MAX_DETAIL_CHARS) -> str:
    text = redact_secrets(text)
    return (
        text
        if len(text) <= max_chars
        else f"{text[:max_chars]}…<已截断，原长 {len(text)}>"
    )


def _canonical_record(record: AuditRecord) -> str:
    payload = record.model_dump(
        exclude={"prev_hash", "hash"}, exclude_none=True
    )
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def record_hash(record: AuditRecord, prev_hash: str) -> str:
    material = f"{prev_hash}\n{_canonical_record(record)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _read_last_hash(path: Path) -> str:
    """读取一个片段最后一条有效记录的 hash；空/不存在返回空串。"""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if size <= 0:
                return ""
            handle.seek(max(0, size - 65_536))
            data = handle.read()
    except OSError:
        return ""
    for raw in reversed(data.splitlines()):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            return str(payload.get("hash", "") or "")
    return ""


def _previous_piece(path: Path) -> Path | None:
    match = _PIECE_RE.match(path.name)
    if match is None:
        return None
    index = int(match.group(2) or 0)
    if index <= 0:
        return None
    if index == 1:
        return path.with_name(f"{match.group(1)}.jsonl")
    return path.with_name(f"{match.group(1)}.{index - 1}.jsonl")


def _chain_head(path: Path) -> str:
    """当前片段若为空，链头来自上一片；不扫整个目录。"""
    head = _read_last_hash(path)
    if head:
        return head
    previous = _previous_piece(path)
    while previous is not None:
        head = _read_last_hash(previous)
        if head:
            return head
        previous = _previous_piece(previous)
    return ""


def verify_audit_files(paths: list[Path]) -> tuple[bool, str]:
    """验证审计哈希链。

    返回 `(ok, message)`。旧日志没有 hash 字段：在遇到第一条 hashed 记录
    之前按 legacy 跳过；一旦进入链，后续任何缺 hash/断链/内容改动都失败。
    """
    expected_prev = ""
    started = False
    checked = 0
    for path in paths:
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError as exc:
            return False, f"无法读取 {path}：{exc}"
        with handle:
            for lineno, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = AuditRecord.model_validate_json(text)
                except ValueError:
                    return False, f"{path.name}:{lineno} 无法解析"
                if not record.hash:
                    if started:
                        return (
                            False,
                            f"{path.name}:{lineno} 链中断：缺少 hash",
                        )
                    continue
                if record.prev_hash != expected_prev:
                    return (
                        False,
                        f"{path.name}:{lineno} 链中断：prev_hash 不匹配",
                    )
                computed = record_hash(record, expected_prev)
                if not _secrets.compare_digest(record.hash, computed):
                    return (
                        False,
                        f"{path.name}:{lineno} 内容哈希不匹配（可能被篡改）",
                    )
                expected_prev = record.hash
                started = True
                checked += 1
    if not started:
        return True, "仅包含 legacy 记录，没有可验证的哈希链"
    return True, f"哈希链完整（{checked} 条带 hash 的记录）"


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
        day = self._day()
        try:
            if not self._dir_ready:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._dir_ready = True
            # 多个进程可能写同一个审计目录；按天串行化「选片 + 追加」，
            # 避免轮转判断与追加交错导致记录落进旧片或重复换片。
            with FileLock(self.directory / f"{day}.lock"):
                target = self._active_path()
                # 写盘前统一脱敏，再计算哈希；两者必须基于同一份内容。
                record.detail = redact_secrets(record.detail)
                if record.steps:
                    record.steps = [
                        redact_secrets(step) for step in record.steps
                    ]
                if record.task:
                    record.task = redact_secrets(record.task)
                prev_hash = _chain_head(target)
                record.prev_hash = prev_hash
                record.hash = record_hash(record, prev_hash)
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
