"""会话索引：从审计日志归纳历史会话，不依赖任何额外存储。"""

from __future__ import annotations

from pathlib import Path

from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.audit.logger import now_iso
from coding_agent.audit.models import FILE_CHANGE, PLAN, ROLLBACK, RUN_END, RUN_START, TOOL_CALL
from coding_agent.memory.sessions import SessionIndex, format_table_rows


def _write(logger: AuditLogger, **fields) -> None:
    logger.write(AuditRecord(ts=fields.pop("ts", now_iso()), **fields))


def test_empty_directory(tmp_path) -> None:
    assert SessionIndex(tmp_path).list() == []


def test_missing_directory(tmp_path) -> None:
    assert SessionIndex(tmp_path / "nope").list() == []


def test_groups_by_thread(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    _write(logger, kind=RUN_START, thread_id="a", detail="第一个会话的提问")
    _write(logger, kind=TOOL_CALL, thread_id="a", tool="shell_exec")
    _write(logger, kind=RUN_END, thread_id="a")
    _write(logger, kind=RUN_START, thread_id="b", detail="第二个会话的提问")

    sessions = SessionIndex(tmp_path).list()
    assert {s.thread_id for s in sessions} == {"a", "b"}


def test_title_comes_from_earliest_prompt(tmp_path) -> None:
    """多轮会话里，标题应当是第一次提问，不是最后一次。"""
    logger = AuditLogger(tmp_path)
    _write(
        logger, kind=RUN_START, thread_id="multi", detail="最初的问题",
        ts="2026-10-02T10:00:00+00:00",
    )
    _write(
        logger, kind=RUN_START, thread_id="multi", detail="后面的问题",
        ts="2026-10-02T11:00:00+00:00",
    )

    info = SessionIndex(tmp_path).get("multi")
    assert info.title == "最初的问题"
    assert info.prompts == 2
    assert info.started_at == "2026-10-02T10:00:00+00:00"
    assert info.last_active == "2026-10-02T11:00:00+00:00"


def test_counts_tool_calls_and_plans(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    _write(logger, kind=RUN_START, thread_id="a", detail="干活")
    _write(logger, kind=PLAN, thread_id="a", steps=["一步"])
    _write(logger, kind=TOOL_CALL, thread_id="a", tool="shell_exec")
    _write(logger, kind=TOOL_CALL, thread_id="a", tool="file_edit")
    _write(logger, kind=FILE_CHANGE, thread_id="a", path="x.py")

    info = SessionIndex(tmp_path).get("a")
    assert info.tool_calls == 2
    assert info.plans == 1
    assert info.prompts == 1


def test_sorted_by_recent_activity(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    _write(
        logger, kind=RUN_START, thread_id="old", detail="旧的", ts="2026-10-01T10:00:00+00:00"
    )
    _write(
        logger, kind=RUN_START, thread_id="new", detail="新的", ts="2026-10-03T10:00:00+00:00"
    )
    _write(
        logger, kind=TOOL_CALL, thread_id="old", tool="shell_exec",
        ts="2026-10-03T12:00:00+00:00",
    )

    sessions = SessionIndex(tmp_path).list()
    assert [s.thread_id for s in sessions] == ["old", "new"]


def test_limit(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    for i in range(5):
        _write(logger, kind=RUN_START, thread_id=f"t{i}", detail=f"问题 {i}",
               ts=f"2026-10-0{i + 1}T10:00:00+00:00")
    assert len(SessionIndex(tmp_path).list(limit=2)) == 2


def test_records_without_thread_id_are_skipped(tmp_path) -> None:
    """rollback 之类由用户直接触发的操作没有 thread_id。"""
    logger = AuditLogger(tmp_path)
    _write(logger, kind=RUN_START, thread_id="a", detail="有会话")
    _write(logger, kind=ROLLBACK, path="x.py")

    sessions = SessionIndex(tmp_path).list()
    assert [s.thread_id for s in sessions] == ["a"]


def test_workspace_is_captured(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    _write(logger, kind=RUN_START, thread_id="a", detail="x", workspace="/mnt/d/proj")
    assert SessionIndex(tmp_path).get("a").workspace == "/mnt/d/proj"


def test_title_falls_back_to_run_end(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    _write(logger, kind=RUN_END, thread_id="a", detail="最终答复内容")
    assert SessionIndex(tmp_path).get("a").title == "最终答复内容"


def test_title_is_normalized_to_one_line(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    _write(logger, kind=RUN_START, thread_id="a", detail="多行\n\n提问   带空格")
    assert SessionIndex(tmp_path).get("a").title == "多行 提问 带空格"


def test_title_is_truncated(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    _write(logger, kind=RUN_START, thread_id="a", detail="x" * 500)
    assert len(SessionIndex(tmp_path).get("a").title) <= 80


def _piece(path: Path, thread_id: str, ts: str) -> None:
    """直接往某个审计片里写一条 —— 用来造出「同一天的多个轮转片」。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            AuditRecord(ts=ts, kind=RUN_START, thread_id=thread_id, detail="提问")
            .model_dump_json(exclude_none=True)
            + "\n"
        )


def test_lookback_is_counted_in_days_not_files(tmp_path) -> None:
    """回溯的单位必须是天。

    启用轮转后一天会有多片；按文件数回溯时，一天的 3 片就能吃掉 2 的配额，
    更早那天的会话就静默消失了（审计文件还在，列表里却查不到）。
    """
    for piece in ("", ".1", ".2"):
        _piece(tmp_path / f"2026-09-01{piece}.jsonl", "old", "2026-09-01T10:00:00+00:00")
        _piece(tmp_path / f"2026-09-02{piece}.jsonl", "new", "2026-09-02T10:00:00+00:00")

    sessions = SessionIndex(tmp_path, lookback_days=2).list()
    assert {s.thread_id for s in sessions} == {"old", "new"}


def test_lookback_days_excludes_older_sessions(tmp_path) -> None:
    for piece in ("", ".1"):
        _piece(tmp_path / f"2026-09-01{piece}.jsonl", "old", "2026-09-01T10:00:00+00:00")
    _piece(tmp_path / "2026-09-02.jsonl", "new", "2026-09-02T10:00:00+00:00")

    assert [s.thread_id for s in SessionIndex(tmp_path, lookback_days=1).list()] == ["new"]


def test_pieces_of_one_day_are_all_scanned(tmp_path) -> None:
    """同一天的每一片都要算进来：会话的信息是分散在各片里的。"""
    _piece(tmp_path / "2026-09-01.jsonl", "a", "2026-09-01T10:00:00+00:00")
    _piece(tmp_path / "2026-09-01.1.jsonl", "a", "2026-09-01T11:00:00+00:00")
    _piece(tmp_path / "2026-09-01.2.jsonl", "b", "2026-09-01T12:00:00+00:00")

    info = SessionIndex(tmp_path, lookback_days=1).get("a")
    assert info is not None and info.prompts == 2
    assert {s.thread_id for s in SessionIndex(tmp_path, lookback_days=1).list()} == {"a", "b"}


def test_corrupt_lines_do_not_break_listing(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    _write(logger, kind=RUN_START, thread_id="a", detail="正常")
    with logger.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    _write(logger, kind=RUN_START, thread_id="b", detail="也正常")

    assert len(SessionIndex(tmp_path).list()) == 2


def test_format_table_rows(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    _write(logger, kind=RUN_START, thread_id="a", detail="干点活")
    _write(logger, kind=TOOL_CALL, thread_id="a", tool="shell_exec")

    rows = format_table_rows(SessionIndex(tmp_path).list())
    assert rows[0][0] == "a"
    assert rows[0][2] == "1"  # 提问数
    assert rows[0][3] == "1"  # 工具调用数
    assert rows[0][4] == "干点活"
