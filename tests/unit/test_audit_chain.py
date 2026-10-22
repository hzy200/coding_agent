"""审计哈希链：正常、轮转、篡改与 legacy。"""

from __future__ import annotations

from coding_agent.audit import AuditLogger, verify_audit_files
from coding_agent.audit.models import AuditRecord


def _record(detail: str) -> AuditRecord:
    return AuditRecord(ts="2026-01-01T00:00:00+00:00", kind="tool_call", detail=detail)


def test_chain_verifies_after_writes(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    for index in range(5):
        logger.write(_record(f"r{index}"))

    ok, message = verify_audit_files(logger.files_today())

    assert ok, message
    assert "5" in message


def test_chain_verifies_across_rotation(tmp_path) -> None:
    logger = AuditLogger(tmp_path, max_bytes=1)
    for index in range(4):
        logger.write(_record(f"r{index}"))

    paths = logger.files_today()
    assert len(paths) >= 2

    ok, message = verify_audit_files(paths)
    assert ok, message


def test_tampering_is_detected(tmp_path) -> None:
    logger = AuditLogger(tmp_path)
    for index in range(4):
        logger.write(_record(f"r{index}"))

    path = logger.path
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace("r2", "tampered")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, message = verify_audit_files([path])

    assert not ok
    assert "篡改" in message or "不匹配" in message


def test_legacy_prefix_is_skipped(tmp_path) -> None:
    path = tmp_path / "2026-01-01.jsonl"
    legacy = AuditRecord(
        ts="2026-01-01T00:00:00+00:00", kind="run_start", detail="legacy"
    )
    path.write_text(legacy.model_dump_json(exclude_none=True) + "\n", encoding="utf-8")

    logger = AuditLogger(tmp_path)
    logger._day = lambda: "2026-01-01"  # type: ignore[method-assign]
    logger.write(_record("new"))

    ok, message = verify_audit_files([path])

    assert ok, message
