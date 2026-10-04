from __future__ import annotations

import json

import pytest
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from coding_agent.audit import AuditLogger, AuditRecord, read_records
from coding_agent.audit.logger import now_iso
from coding_agent.config import Settings
from coding_agent.events import (
    FileChanged,
    PlanCreated,
    RunFailed,
    RunFinished,
    RunStarted,
    StepFinished,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from coding_agent.runtime import AgentRuntime
from coding_agent.tools.artifacts import FileArtifact, ShellArtifact, pack, unpack


def _runtime() -> AgentRuntime:
    # 显式给 workspace，避免构造时去探测 WSL
    return AgentRuntime(Settings(_env_file=None), workspace="/tmp/ws")


# ---------------- 事件模型 ----------------

def test_events_are_json_serializable_for_sse() -> None:
    for event in (
        PlanCreated(steps=["a"]),
        StepStarted(index=0, total=2, text="a"),
        StepFinished(index=0, text="done", budget_exhausted=True),
        ToolCallStarted(name="shell_exec", args={"command": "ls"}, level="L0 只读"),
        ToolCallFinished(name="shell_exec", ok=True, exit_code=0),
        RunStarted(thread_id="t"),
        RunFinished(thread_id="t", answer="ok"),
        RunFailed(message="boom"),
    ):
        payload = json.dumps(event.model_dump(), ensure_ascii=False)
        assert json.loads(payload)["type"] == event.type


def test_event_type_discriminator() -> None:
    assert PlanCreated().type == "plan_created"
    assert RunStarted().type == "run_started"
    assert RunFinished().type == "run_finished"


def test_events_are_frozen() -> None:
    """事件不可变：前端拿到手的事件不应被别的 handler 改写。"""
    event = RunFinished(thread_id="t")
    with pytest.raises(ValidationError):
        event.thread_id = "other"  # type: ignore[misc]


# ---------------- runtime 翻译 ----------------

def test_tool_started_classifies_shell_command() -> None:
    event = _runtime()._tool_started("shell_exec", {"command": "ls -la", "reason": "r"})
    assert isinstance(event, ToolCallStarted)
    assert event.level == "L0 只读"
    assert event.summary == "ls -la"


def test_tool_started_flags_dangerous_command_for_ui() -> None:
    """着色归着色：等级只用于前端展示，不放行任何东西。"""
    event = _runtime()._tool_started("shell_exec", {"command": "rm -rf /"})
    assert event.level == "L3 危险"


def test_tool_started_without_level_for_non_shell_tools() -> None:
    event = _runtime()._tool_started("file_read", {"path": "a.py"})
    assert event.level is None
    assert event.summary == "a.py"


def test_tool_finished_reads_shell_artifact() -> None:
    artifact = ShellArtifact(
        command="echo hi", ok=True, exit_code=0, duration_ms=42, level=0, level_label="L0 只读"
    )
    message = ToolMessage(
        content="[L0 只读] r\nexit_code=0", tool_call_id="1", name="shell_exec",
        artifact=artifact.model_dump(),
    )
    event = _runtime()._tool_finished(message)
    assert isinstance(event, ToolCallFinished)
    assert event.ok is True
    assert event.exit_code == 0
    assert event.duration_ms == 42
    assert event.level == "L0 只读"
    assert event.rejected is False


def test_tool_finished_marks_rejection() -> None:
    artifact = ShellArtifact(ok=False, rejected=True, level=3, level_label="L3 危险")
    message = ToolMessage(
        content="命令被安全策略拒绝。", tool_call_id="1", name="shell_exec",
        artifact=artifact.model_dump(),
    )
    event = _runtime()._tool_finished(message)
    assert event.rejected is True
    assert event.exit_code is None


def test_tool_finished_without_artifact_falls_back_to_text() -> None:
    ok = _runtime()._tool_finished(ToolMessage(content="正常输出", tool_call_id="1", name="t"))
    assert ok.ok is True

    bad = _runtime()._tool_finished(
        ToolMessage(content="工具执行异常：RuntimeError: x", tool_call_id="1", name="t")
    )
    assert bad.ok is False


def test_tool_finished_preview_is_truncated() -> None:
    message = ToolMessage(content="x" * 5000, tool_call_id="1", name="t")
    event = _runtime()._tool_finished(message)
    assert len(event.preview) <= 241
    assert event.preview.endswith("…")


def test_tool_finished_survives_malformed_artifact() -> None:
    message = ToolMessage(content="out", tool_call_id="1", name="t", artifact={"ok": "not-a-bool"})
    event = _runtime()._tool_finished(message)
    assert event.name == "t"


# ---------------- workspace 解析 ----------------

def _file_message(**artifact_fields) -> ToolMessage:
    artifact = FileArtifact(**artifact_fields).model_dump()
    return ToolMessage(content="…", tool_call_id="1", name="file_edit", artifact=artifact)


def test_file_changed_emitted_for_mutating_actions() -> None:
    for action in ("create", "overwrite", "edit"):
        event = AgentRuntime._file_changed(
            _file_message(path="a.py", action=action, ok=True, added=3, removed=1,
                          diff="--- a\n+++ b\n", snapshot_id="snap1")
        )
        assert event is not None, action
        assert event.path == "a.py"
        assert event.added == 3
        assert event.removed == 1
        assert event.snapshot_id == "snap1"


def test_file_changed_not_emitted_for_read() -> None:
    assert AgentRuntime._file_changed(_file_message(path="a.py", action="read", ok=True)) is None


def test_file_changed_not_emitted_when_rejected_or_failed() -> None:
    assert AgentRuntime._file_changed(
        _file_message(path="a.py", action="edit", ok=False, rejected=True)
    ) is None
    assert AgentRuntime._file_changed(
        _file_message(path="a.py", action="edit", ok=False)
    ) is None


def test_file_changed_ignores_shell_artifact() -> None:
    message = ToolMessage(
        content="x",
        tool_call_id="1",
        name="shell_exec",
        artifact=ShellArtifact(command="ls", ok=True).model_dump(),
    )
    assert AgentRuntime._file_changed(message) is None


def test_tool_finished_maps_file_artifact() -> None:
    event = _runtime()._tool_finished(
        _file_message(path="a.py", action="edit", ok=True, added=1, removed=1)
    )
    assert isinstance(event, ToolCallFinished)
    assert event.ok is True
    assert event.level == "文件工具"
    assert event.exit_code is None


def test_tool_finished_marks_rejected_file_operation() -> None:
    event = _runtime()._tool_finished(_file_message(path="a.py", action="edit", rejected=True))
    assert event.rejected is True


# ---------------- 审计记录 ----------------

def _audited_runtime(tmp_path) -> AgentRuntime:
    return AgentRuntime(
        Settings(_env_file=None), workspace="/tmp/ws", audit=AuditLogger(tmp_path)
    )


def test_audit_records_rejected_tool_call(tmp_path) -> None:
    runtime = _audited_runtime(tmp_path)
    started = runtime._tool_started("shell_exec", {"command": "rm -rf /", "reason": "清理"}, "c1")
    finished = runtime._tool_finished(
        ToolMessage(
            content="命令被安全策略拒绝",
            tool_call_id="c1",
            name="shell_exec",
            artifact=ShellArtifact(
                ok=False, rejected=True, level=3, level_label="L3 危险"
            ).model_dump(),
        )
    )
    runtime._audit_tool_call(started, finished, "t1")

    record = read_records(runtime.audit_path)[0]
    assert record.kind == "tool_call"
    assert record.call_id == "c1"
    assert record.tool == "shell_exec"
    assert record.decision == "rejected"
    assert record.level == "L3 危险"
    assert record.args["command"] == "rm -rf /"
    assert record.ok is False


def test_audit_records_executed_tool_call(tmp_path) -> None:
    runtime = _audited_runtime(tmp_path)
    started = runtime._tool_started("shell_exec", {"command": "ls -la"}, "c2")
    finished = runtime._tool_finished(
        ToolMessage(
            content="ok",
            tool_call_id="c2",
            name="shell_exec",
            artifact=ShellArtifact(
                ok=True, exit_code=0, duration_ms=12, level=0, level_label="L0 只读"
            ).model_dump(),
        )
    )
    runtime._audit_tool_call(started, finished, "t1")

    record = read_records(runtime.audit_path)[0]
    assert record.decision == "auto"
    assert record.exit_code == 0
    assert record.duration_ms == 12


def test_audit_never_stores_full_file_content(tmp_path) -> None:
    """审计日志不能把整个文件内容抄一遍。"""
    runtime = _audited_runtime(tmp_path)
    started = runtime._tool_started(
        "file_write", {"path": "a.py", "content": "x" * 5000, "reason": "写"}, "c3"
    )
    finished = runtime._tool_finished(
        ToolMessage(content="已创建", tool_call_id="c3", name="file_write",
                    artifact=FileArtifact(path="a.py", action="create", ok=True).model_dump())
    )
    runtime._audit_tool_call(started, finished, "t1")

    record = read_records(runtime.audit_path)[0]
    assert len(record.args["content"]) < 500
    assert "已截断" in record.args["content"]


def test_audit_records_file_change(tmp_path) -> None:
    runtime = _audited_runtime(tmp_path)
    changed = FileChanged(
        path="a.py", action="edit", added=3, removed=1, snapshot_id="snap1"
    )
    runtime._audit_file_change(changed, "t1")

    record = read_records(runtime.audit_path)[0]
    assert record.kind == "file_change"
    assert record.path == "a.py"
    assert (record.added, record.removed) == (3, 1)
    assert record.snapshot_id == "snap1"


def test_audit_records_are_thread_scoped(tmp_path) -> None:
    runtime = _audited_runtime(tmp_path)
    runtime._audit(
        AuditRecord(ts=now_iso(), kind="run_start", thread_id="a", detail="hi")
    )
    runtime._audit(
        AuditRecord(ts=now_iso(), kind="run_start", thread_id="b", detail="hi")
    )
    assert len(read_records(runtime.audit_path, thread_id="a")) == 1
    assert len(read_records(runtime.audit_path)) == 2


def test_tool_started_and_finished_pair_by_call_id() -> None:
    runtime = _runtime()
    first = runtime._tool_started("shell_exec", {"command": "a"}, "id-1")
    second = runtime._tool_started("shell_exec", {"command": "b"}, "id-2")
    finished = runtime._tool_finished(
        ToolMessage(content="out", tool_call_id="id-2", name="shell_exec")
    )
    assert {first.call_id, second.call_id} == {"id-1", "id-2"}
    assert finished.call_id == "id-2"


def test_runtime_is_not_persistent_without_a_path(tmp_path) -> None:
    settings = Settings(_env_file=None, checkpoint_path=":memory:")
    runtime = AgentRuntime(settings, workspace="/tmp/ws", audit=AuditLogger(tmp_path))
    assert not runtime.persistent


# ---------------- 验证结果 → 事件 ----------------

def test_verification_maps_failure_with_issues() -> None:
    event = AgentRuntime._verification(
        {
            "status": "failed",
            "command": "python3 -m pytest -q",
            "summary": "1 failed",
            "issues": [{"location": "tests/a.py:3", "message": "assert 1 == 2"}],
        }
    )
    assert event.status == "failed"
    assert event.ok is False
    assert event.issues == ["tests/a.py:3 assert 1 == 2"]


def test_verification_non_failure_statuses_count_as_ok() -> None:
    for status in ("ok", "skipped", "not_configured"):
        assert AgentRuntime._verification({"status": status}).ok is True
    assert AgentRuntime._verification({"status": "failed"}).ok is False


def test_verification_survives_missing_fields() -> None:
    event = AgentRuntime._verification({})
    assert event.status == "skipped"
    assert event.issues == []


def test_explicit_workspace_is_normalized_without_touching_wsl() -> None:
    runtime = AgentRuntime(Settings(_env_file=None), workspace="D:\\proj\\ws")
    assert runtime.workspace == "/mnt/d/proj/ws"


def test_workspace_override_reaches_tool_pathguard() -> None:
    """回归：工具的路径守卫从 settings 读工作区。

    若 --workspace 覆盖不写回 settings，工具会按旧根目录判边界，
    导致所有命令都被判「工作目录非法」。
    """
    settings = Settings(_env_file=None)
    assert settings.wsl_workspace == ""
    runtime = AgentRuntime(settings, workspace="/mnt/d/proj/ws")
    assert runtime.effective_settings.wsl_workspace == "/mnt/d/proj/ws"
    # 原配置对象不被就地修改
    assert settings.wsl_workspace == ""


def test_packed_artifact_survives_tool_message_roundtrip() -> None:
    """ToolMessage 要能进 checkpoint，artifact 必须是普通 dict。"""
    _, artifact = unpack(pack("text", ShellArtifact(command="ls", ok=True)))
    message = ToolMessage(content="text", tool_call_id="1", name="shell_exec", artifact=artifact)
    assert isinstance(message.artifact, dict)
