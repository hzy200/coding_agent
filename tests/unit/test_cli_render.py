"""CLI 渲染层与辅助函数。

渲染器此前完全没有测试覆盖，但它是最主要的用户界面 ——
之前几轮改动（最终答复分界、验证/修复渲染、文件改动行）全是"改完看一眼"，
没有回归保护。这里把它锁住。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console

from coding_agent.audit import AuditRecord
from coding_agent.cli import app as cli_app
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    FileChanged,
    PlanCreated,
    RepairStarted,
    RunFailed,
    RunFinished,
    StepFinished,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
    Verification,
)


@pytest.fixture(autouse=True)
def output(monkeypatch) -> Console:
    """把 CLI 的 rich Console 换成记录型，收集纯文本输出。

    autouse：渲染器读的是模块级 console，每个用例都必须换掉它。
    """
    console = Console(record=True, width=200, force_terminal=False, no_color=True)
    monkeypatch.setattr(cli_app, "console", console)
    return console


def _render(events) -> str:
    render = cli_app._make_renderer()
    for event in events:
        render(event)
    return _text()


def _text() -> str:
    # clear=False：默认会清空记录缓冲，同一次输出读两遍就变成空串
    return cli_app.console.export_text(clear=False)


# ---------------- 计划与步进 ----------------

def test_plan_is_rendered_with_marker() -> None:
    text = _render([PlanCreated(steps=["第一步", "第二步"])])
    assert "计划：" in text
    assert "▶ 1. 第一步" in text          # 当前步骤
    assert "· 2. 第二步" in text          # 未开始
    assert "开始执行…" in text


def test_empty_plan_prints_nothing() -> None:
    text = _render([PlanCreated(steps=[])])
    assert "计划：" not in text
    assert "开始执行" not in text


def test_single_step_header_is_suppressed() -> None:
    """单步计划的标题只是重复用户问题，省略。"""
    text = _render([StepStarted(index=0, total=1, text="看看文件")])
    assert "第 1/1 步" not in text
    assert "助手" in text


def test_multi_step_header_is_shown() -> None:
    text = _render([StepStarted(index=1, total=3, text="读配置")])
    assert "第 2/3 步" in text
    assert "读配置" in text


def test_step_finished_ends_the_line() -> None:
    text = _render([AssistantToken(node="act", text="做完了"), StepFinished(index=0, text="x")])
    assert "做完了" in text
    assert text.endswith("\n\n")


def test_budget_exhaustion_is_flagged() -> None:
    text = _render([StepFinished(index=0, text="x", budget_exhausted=True)])
    assert "工具预算耗尽" in text


# ---------------- 流式输出 ----------------

def test_assistant_tokens_are_concatenated() -> None:
    text = _render([
        AssistantToken(node="act", text="你"),
        AssistantToken(node="act", text="好"),
    ])
    assert "你好" in text


def test_final_answer_gets_a_separator_once() -> None:
    """act 与 respond 内容常有重叠，没有分界读者会当成重复输出。"""
    text = _render([
        AssistantToken(node="act", text="步骤小结"),
        AssistantToken(node="respond", text="甲"),
        AssistantToken(node="respond", text="乙"),
    ])
    assert text.count("── 最终答复 ──") == 1
    assert text.index("── 最终答复 ──") < text.index("甲乙")


def test_no_separator_when_only_act_streams() -> None:
    text = _render([AssistantToken(node="act", text="只有小结")])
    assert "最终答复" not in text


def test_markup_in_model_output_is_not_swallowed() -> None:
    """模型输出里的方括号会被 rich 当标记吞掉，必须原样显示。"""
    text = _render([AssistantToken(node="act", text="见 [docs/PLAN.md] 一节")])
    assert "[docs/PLAN.md]" in text


# ---------------- 工具调用 ----------------

def test_tool_call_shows_level_and_name() -> None:
    text = _render([ToolCallStarted(name="shell_exec", args={}, summary="ls -la",
                                    level="L0 只读")])
    assert "→ [L0 只读] shell_exec: ls -la" in text


def test_tool_call_without_level() -> None:
    text = _render([ToolCallStarted(name="file_read", args={}, summary="a.py")])
    assert "→ file_read: a.py" in text
    assert "[]" not in text


def test_successful_tool_result_shows_elapsed() -> None:
    text = _render([
        ToolCallFinished(name="shell_exec", ok=True, exit_code=0, duration_ms=250)
    ])
    assert "← ✓" in text
    assert "0.25s" in text


def test_successful_tool_result_without_duration() -> None:
    text = _render([ToolCallFinished(name="file_read", ok=True)])
    assert "← ✓" in text


def test_failed_tool_result_shows_exit_code() -> None:
    text = _render([ToolCallFinished(name="shell_exec", ok=False, exit_code=2)])
    assert "exit=2" in text


def test_failed_tool_result_without_exit_code() -> None:
    """文件工具没有退出码。"""
    text = _render([ToolCallFinished(name="file_edit", ok=False, exit_code=None)])
    assert "✗" in text
    assert "exit=" not in text


def test_rejected_tool_result_says_policy() -> None:
    text = _render([ToolCallFinished(name="shell_exec", ok=False, rejected=True)])
    assert "已被安全策略拒绝" in text


def test_denied_tool_result_says_user() -> None:
    """用户拒绝与策略拒绝要区分开 —— 责任方不同。"""
    text = _render([
        ToolCallFinished(name="shell_exec", ok=False, rejected=True, decision="denied")
    ])
    assert "已被用户拒绝" in text
    assert "安全策略" not in text


# ---------------- 文件改动 ----------------

def test_file_change_shows_stat_and_snapshot() -> None:
    text = _render([
        FileChanged(path="src/a.py", action="edit", added=3, removed=1, snapshot_id="snap1")
    ])
    assert "✎ src/a.py" in text
    assert "+3 -1" in text
    assert "snap1" in text


def test_file_change_without_snapshot() -> None:
    text = _render([FileChanged(path="a.py", action="create", added=5, removed=0)])
    assert "✎ a.py" in text
    assert "+5 -0" in text


# ---------------- 验证 ----------------

def test_verification_passed() -> None:
    text = _render([Verification(status="ok", command="pytest", ok=True, summary="3 passed")])
    assert "验证通过" in text
    assert "3 passed" in text


def test_verification_failed_lists_issues() -> None:
    text = _render([Verification(
        status="failed", command="pytest", ok=False, summary="1 failed",
        issues=["a.py:3 boom", "b.py:7 crash"],
    )])
    assert "验证失败" in text
    assert "a.py:3 boom" in text
    assert "b.py:7 crash" in text


def test_verification_failed_caps_issue_list() -> None:
    """问题多的时候只列前几条，剩下的交给审计日志。"""
    issues = [f"f{i}.py:{i} boom" for i in range(20)]
    text = _render([Verification(status="failed", ok=False, issues=issues)])
    assert "f4.py:4" in text
    assert "f5.py:5" not in text


def test_verification_not_configured() -> None:
    text = _render([Verification(status="not_configured")])
    assert "未检测到可用的测试命令" in text


def test_verification_skipped_is_silent() -> None:
    text = _render([Verification(status="skipped")])
    assert "验证" not in text


# ---------------- 修复与审批 ----------------

def test_repair_attempt_is_rendered() -> None:
    text = _render([RepairStarted(attempt=2, limit=3, summary="1 failed")])
    assert "↻ 第 2/3 次修复" in text
    assert "1 failed" in text


def test_approval_event_renders_nothing() -> None:
    """审批的提问由 _run_events 负责，渲染器不重复输出。"""
    text = _render([ApprovalRequested(request_id="c1", command="pip install x")])
    assert text == ""


# ---------------- 收尾 ----------------

def test_run_failed_is_red_and_readable() -> None:
    text = _render([RunFailed(message="RuntimeError: 模型炸了")])
    assert "运行失败" in text
    assert "RuntimeError: 模型炸了" in text


def test_run_finished_ends_with_newline() -> None:
    text = _render([AssistantToken(node="respond", text="答案"), RunFinished(thread_id="t")])
    assert text.endswith("\n")


def test_full_sequence_renders_everything() -> None:
    text = _render([
        PlanCreated(steps=["甲"]),
        StepStarted(index=0, total=1, text="甲"),
        ToolCallStarted(name="shell_exec", args={}, summary="ls", level="L0 只读"),
        ToolCallFinished(name="shell_exec", ok=True, exit_code=0, duration_ms=10),
        FileChanged(path="a.py", action="edit", added=1, removed=1, snapshot_id="s"),
        StepFinished(index=0, text="小结"),
        Verification(status="ok", ok=True, summary="1 passed"),
        AssistantToken(node="respond", text="结论"),
        RunFinished(thread_id="t"),
    ])
    for expected in ("计划：", "shell_exec: ls", "← ✓", "✎ a.py", "验证通过",
                     "── 最终答复 ──", "结论"):
        assert expected in text, expected


# ---------------- 辅助函数 ----------------

def _record(**fields) -> AuditRecord:
    base = {"ts": "2026-10-03T10:00:00+00:00", "kind": "tool_call", "thread_id": "t"}
    return AuditRecord(**{**base, **fields})


def test_audit_outcome_for_tool_call() -> None:
    assert "OK" in cli_app._audit_outcome(_record(ok=True, duration_ms=12))
    assert "已批准" in cli_app._audit_outcome(_record(ok=True, decision="approved"))
    assert "失败" in cli_app._audit_outcome(_record(ok=False, exit_code=2))
    assert "已拒绝" in cli_app._audit_outcome(_record(ok=False, decision="denied"))
    assert "已拒绝" in cli_app._audit_outcome(_record(ok=False, decision="rejected"))


def test_audit_outcome_handles_missing_exit_code() -> None:
    """文件工具没有退出码，不能显示成 exit=None。"""
    outcome = cli_app._audit_outcome(_record(kind="tool_call", ok=False, exit_code=None))
    assert "None" not in outcome
    assert outcome == "失败"


def test_audit_outcome_for_other_kinds() -> None:
    assert "+3 -1" in cli_app._audit_outcome(_record(kind="file_change", added=3, removed=1))
    assert "+1 -0" in cli_app._audit_outcome(_record(kind="rollback", added=1, removed=0))
    assert "2 步" in cli_app._audit_outcome(_record(kind="plan", steps=["a", "b"]))
    assert "通过" in cli_app._audit_outcome(_record(kind="verify", ok=True, detail="pytest"))
    assert "失败" in cli_app._audit_outcome(_record(kind="verify", ok=False, detail="pytest"))
    assert "第 1/3 次修复" in cli_app._audit_outcome(
        _record(kind="repair", detail="第 1/3 次修复（1 failed）")
    )
    assert "boom" in cli_app._audit_outcome(_record(kind="run_error", detail="RuntimeError: boom"))


def test_audit_outcome_unknown_kind_is_blank() -> None:
    assert cli_app._audit_outcome(_record(kind="run_start")) == ""


def test_diff_text_colours_by_line_type() -> None:
    from rich.text import Text

    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n context\n"
    styled = cli_app._diff_text(diff)
    assert isinstance(styled, Text)
    plain = styled.plain
    assert "-old" in plain and "+new" in plain and "@@" in plain
    assert len(styled.spans) >= 4  # 各类行都有对应样式


def test_diff_text_handles_empty_input() -> None:
    assert cli_app._diff_text("").plain == ""


def test_format_table_rows_shape() -> None:
    from coding_agent.memory.sessions import SessionInfo

    info = SessionInfo(
        thread_id="abc", title="改一下 README", workspace="/w",
        started_at="2026-10-03T10:00:00+00:00", last_active="2026-10-03T11:00:00+00:00",
        prompts=2, tool_calls=5,
    )
    from coding_agent.memory.sessions import format_table_rows

    rows = format_table_rows([info])
    assert rows[0] == ("abc", "2026-10-03 11:00:00", "2", "5", "改一下 README")


# ---------------- 配置来源告警 ----------------

def test_shadowed_keys_reports_dotenv_winner(tmp_path, monkeypatch, output) -> None:
    from coding_agent.config import shadowed_env_keys

    env_file = tmp_path / ".env"
    env_file.write_text("LANGSMITH_API_KEY=from_dotenv\n", encoding="utf-8")
    monkeypatch.setenv("LANGSMITH_API_KEY", "from_environment")

    assert shadowed_env_keys(str(env_file)) == {"LANGSMITH_API_KEY": ".env"}


def test_report_failure_returns_false(output) -> None:
    assert cli_app._report("某检查", False, "坏了") is False
    assert "FAIL" in _text()


def test_report_success_returns_true(output) -> None:
    assert cli_app._report("某检查", True, "正常") is True
    assert "PASS" in _text()


def test_warn_does_not_print_pass_or_fail(output) -> None:
    cli_app._warn("提示项", "注意点")
    text = _text()
    assert "WARN" in text
    assert "PASS" not in text and "FAIL" not in text


# ---------------- 审批提问 ----------------

def _approval(request_id: str = "c1") -> ApprovalRequested:
    return ApprovalRequested(
        request_id=request_id, tool="shell_exec", command="pip install x",
        level="L2 变更性", reason="装依赖",
    )


def test_ask_approval_assume_yes(monkeypatch, output) -> None:
    decisions = cli_app._ask_approval([_approval()], assume_yes=True)
    assert decisions == {"c1": True}
    assert "--yes" in _text()


def test_ask_approval_records_answers(monkeypatch, output) -> None:
    answers = iter(["y", "n", "YES"])
    monkeypatch.setattr(cli_app.console, "input", lambda *a, **k: next(answers))

    decisions = cli_app._ask_approval(
        [_approval("c1"), _approval("c2"), _approval("c3")], assume_yes=False
    )
    assert decisions == {"c1": True, "c2": False, "c3": True}


def test_ask_approval_non_interactive_denies(monkeypatch, output) -> None:
    """非交互环境（管道、CI）必须 fail closed。"""

    def boom(*args, **kwargs):
        raise EOFError

    monkeypatch.setattr(cli_app.console, "input", boom)
    decisions = cli_app._ask_approval([_approval()], assume_yes=False)
    assert decisions == {"c1": False}
    assert "按拒绝处理" in _text()


def test_ask_approval_shows_command_and_reason(output) -> None:
    cli_app._ask_approval([_approval()], assume_yes=True)
    text = _text()
    assert "pip install x" in text
    assert "装依赖" in text
    assert "L2 变更性" in text


def test_ask_approval_without_reason(output) -> None:
    request = ApprovalRequested(request_id="c1", command="ls", level="L2 变更性")
    cli_app._ask_approval([request], assume_yes=True)
    assert "理由" not in _text()


# ---------------- LangSmith 自检 ----------------

def test_langsmith_check_when_disabled(output) -> None:
    from coding_agent.config import Settings

    results = cli_app._check_langsmith(Settings(_env_file=None, langsmith_tracing=False))
    assert results == [True]
    assert "关闭" in _text()


def test_langsmith_check_missing_key(output) -> None:
    from coding_agent.config import Settings

    settings = Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="")
    assert cli_app._check_langsmith(settings) == [False]
    assert "缺 LANGSMITH_API_KEY" in _text()


def test_langsmith_check_unreachable_credentials(monkeypatch, output) -> None:
    """凭据无效要提前说清楚，而不是让 langsmith 每次调用后刷错误。

    让 Client 直接抛异常：真去连不可达端点的话，langsmith 的**重试退避**
    会让这条用例跑满 50 秒（测的还是它的重试策略，不是我们的错误处理）。
    """
    import langsmith

    from coding_agent.config import Settings

    class _BoomClient:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("凭据被拒绝")

    monkeypatch.setattr(langsmith, "Client", _BoomClient)
    settings = Settings(
        _env_file=None, langsmith_tracing=True, langsmith_api_key="lsv2_pt_invalid"
    )

    assert cli_app._check_langsmith(settings) == [False]
    text = _text()
    assert "不可用" in text
    assert "RuntimeError" in text


def test_configure_stdio_forces_utf8(monkeypatch) -> None:
    """Windows 标准流默认 GBK，管道里传中文会乱码甚至崩在代理字符上。"""
    import io

    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    monkeypatch.setattr(cli_app.sys, "stdout", stream)
    cli_app._configure_stdio()
    assert stream.encoding.lower().replace("-", "") == "utf8"


def test_datetime_import_is_available() -> None:
    """审计路径里用到了 UTC 时间戳。"""
    assert datetime.now(UTC).year >= 2026
    assert Path(cli_app.__file__).exists()
