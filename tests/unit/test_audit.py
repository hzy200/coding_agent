from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from coding_agent.audit import (
    AuditError,
    AuditLogger,
    AuditRecord,
    read_records,
    read_records_many,
)
from coding_agent.audit.logger import now_iso, sanitize_args, truncate


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _record(**kwargs) -> AuditRecord:
    base = {"ts": now_iso(), "kind": "tool_call", "thread_id": "t1"}
    return AuditRecord(**{**base, **kwargs})


# ---------------- 写入与读取 ----------------

def test_write_then_read(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    logger.write(_record(tool="shell_exec", decision="auto", ok=True))
    logger.write(_record(tool="file_edit", decision="auto", ok=True))

    records = read_records(logger.path)
    assert [r.tool for r in records] == ["shell_exec", "file_edit"]


def test_file_is_append_only_jsonl(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    logger.write(_record(tool="a"))
    logger.write(_record(tool="b"))

    lines = logger.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["tool"] == "a"


def test_creates_directory_on_demand(tmp_path) -> None:
    logger = AuditLogger(tmp_path / "nested" / "deeper")
    logger.write(_record())
    assert logger.path.exists()


def test_disabled_logger_writes_nothing(tmp_path) -> None:
    logger = AuditLogger(tmp_path, enabled=False)
    logger.write(_record())
    assert not logger.path.exists()


def test_write_failure_raises(tmp_path) -> None:
    """审计有缺口必须显式失败，不能静默丢记录。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    logger = AuditLogger(blocker / "audit")
    with pytest.raises(AuditError):
        logger.write(_record())


# ---------------- 读取健壮性 ----------------

def test_filter_by_thread(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    logger.write(_record(thread_id="a", tool="t"))
    logger.write(_record(thread_id="b", tool="t"))
    logger.write(_record(thread_id="a", tool="t"))

    assert len(read_records(logger.path, thread_id="a")) == 2
    assert len(read_records(logger.path, thread_id="b")) == 1
    assert len(read_records(logger.path)) == 3


def test_limit_returns_most_recent(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    for i in range(5):
        logger.write(_record(tool=f"tool{i}"))
    assert [r.tool for r in read_records(logger.path, limit=2)] == ["tool3", "tool4"]


def test_corrupt_lines_are_skipped(tmp_path) -> None:
    """日志是事后排查用的，不能因为一行坏数据就读不出来。"""
    logger = AuditLogger(tmp_path)
    logger.write(_record(tool="good1"))
    with logger.path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n\n")
    logger.write(_record(tool="good2"))

    assert [r.tool for r in read_records(logger.path)] == ["good1", "good2"]


def test_missing_file_returns_empty(tmp_path) -> None:
    assert read_records(tmp_path / "nope.jsonl") == []


# ---------------- 按大小轮转 ----------------

def test_rotation_is_off_by_default(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    for i in range(3):
        logger.write(_record(tool=f"t{i}"))
    assert logger.files_today() == [logger.path]
    assert [r.tool for r in read_records(logger.path)] == ["t0", "t1", "t2"]


def test_rotation_splits_when_file_exceeds_limit(tmp_path) -> None:
    logger = AuditLogger(tmp_path, max_bytes=1)  # 每条都超限 → 每条开一片
    for i in range(3):
        logger.write(_record(tool=f"t{i}"))
    files = logger.files_today()
    assert [p.name for p in files] == [
        f"{_today()}.jsonl",
        f"{_today()}.1.jsonl",
        f"{_today()}.2.jsonl",
    ]


def test_read_records_many_merges_pieces_in_order(tmp_path) -> None:
    logger = AuditLogger(tmp_path, max_bytes=1)
    for i in range(3):
        logger.write(_record(tool=f"t{i}"))
    merged = read_records_many(logger.files_today())
    assert [r.tool for r in merged] == ["t0", "t1", "t2"]
    assert [r.tool for r in read_records_many(logger.files_today(), limit=2)] == ["t1", "t2"]


def test_files_today_sorts_numeric_indices(tmp_path) -> None:
    """片号要按数字排序：字典序会把 .10 排到 .2 前面。"""
    day = _today()
    for name in (f"{day}.jsonl", f"{day}.2.jsonl", f"{day}.10.jsonl"):
        (tmp_path / name).write_text("", encoding="utf-8")
    names = [p.name for p in AuditLogger(tmp_path).files_today()]
    assert names == [f"{day}.jsonl", f"{day}.2.jsonl", f"{day}.10.jsonl"]


# ---------------- 脱敏 ----------------

def test_sanitize_truncates_long_values() -> None:
    """file_write 的 content 是整个文件，原样落库会让日志爆炸。"""
    args = sanitize_args({"path": "a.py", "content": "x" * 5000})
    assert args["path"] == "a.py"
    assert len(args["content"]) < 500
    assert "已截断，原长 5000" in args["content"]


def test_sanitize_keeps_short_values_intact() -> None:
    args = {"command": "ls -la", "reason": "看看目录"}
    assert sanitize_args(args) == args


def test_sanitize_preserves_non_string_values() -> None:
    args = {"offset": 3, "replace_all": True, "empty": None}
    assert sanitize_args(args) == args


def test_truncate_detail() -> None:
    assert truncate("short") == "short"
    assert "已截断" in truncate("y" * 5000)


def test_record_serializes_without_none_noise() -> None:
    payload = _record(tool="shell_exec", exit_code=0).model_dump_json(exclude_none=True)
    assert "exit_code" in payload
    assert "snapshot_id" not in payload  # None 字段不落库，日志更紧凑
