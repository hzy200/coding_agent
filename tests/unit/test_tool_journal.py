"""有副作用工具的执行日志。"""

from __future__ import annotations

from coding_agent.tool_journal import (
    ToolJournal,
    replayed_artifact,
    unknown_result_artifact,
)


def test_started_without_finished_is_unknown(tmp_path) -> None:
    journal = ToolJournal(tmp_path / "journal.jsonl")
    journal.begin("t1", "c1", "shell_exec", {"command": "rm x"})

    record = journal.lookup("t1", "c1")

    assert record is not None
    assert record["event"] == "started"


def test_finished_result_can_be_replayed(tmp_path) -> None:
    journal = ToolJournal(tmp_path / "journal.jsonl")
    journal.begin("t1", "c1", "shell_exec", {"command": "touch x"})
    # 先查一次建立索引，再验证 finish 会更新同一份索引。
    assert journal.lookup("t1", "c1")["event"] == "started"
    journal.finish(
        "t1",
        "c1",
        "shell_exec",
        ok=True,
        content="done",
        artifact={"ok": True, "tool": "shell_exec"},
    )

    record = journal.lookup("t1", "c1")

    assert record is not None
    assert record["event"] == "finished"
    assert record["content"] == "done"
    artifact = replayed_artifact(record, "shell_exec")
    assert artifact["replayed"] is True
    assert artifact["ok"] is True


def test_calls_are_scoped_by_thread_and_call_id(tmp_path) -> None:
    journal = ToolJournal(tmp_path / "journal.jsonl")
    journal.finish(
        "t1", "c1", "shell_exec", ok=True, content="one", artifact=None
    )
    journal.finish(
        "t2", "c1", "shell_exec", ok=True, content="two", artifact=None
    )

    assert journal.lookup("t1", "c1")["content"] == "one"
    assert journal.lookup("t2", "c1")["content"] == "two"
    assert journal.lookup("t3", "c1") is None


def test_disabled_journal_is_a_noop(tmp_path) -> None:
    journal = ToolJournal(tmp_path / "journal.jsonl", enabled=False)
    journal.begin("t1", "c1", "shell_exec", {})

    assert journal.lookup("t1", "c1") is None
    assert not (tmp_path / "journal.jsonl").exists()


def test_unknown_artifact_is_explicitly_not_a_success() -> None:
    artifact = unknown_result_artifact("shell_exec")
    assert artifact["ok"] is None
    assert artifact["interrupted"] is True


def test_finished_journal_redacts_secrets(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = ToolJournal(path)
    secret = "sk-" + "a" * 32
    journal.begin("t1", "c1", "shell_exec", {"command": "env"})
    journal.finish(
        "t1",
        "c1",
        "shell_exec",
        ok=True,
        content=f"API_KEY={secret}",
        artifact={"ok": True, "output": f"password=hunter2hunter2 {secret}"},
    )

    text = path.read_text(encoding="utf-8")

    assert secret not in text
    assert "hunter2hunter2" not in text
    assert "[REDACTED" in text

    # 重放仍然拿到合法 JSON（脱敏后 artifact 仍可解析）。
    record = journal.lookup("t1", "c1")
    artifact = replayed_artifact(record, "shell_exec")
    assert artifact["ok"] is True
    assert artifact["replayed"] is True
