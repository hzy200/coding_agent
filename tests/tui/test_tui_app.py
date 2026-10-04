"""TUI 无头测试：注入假的 runtime，验证「事件 → 界面」的映射。

不需要 API Key，也不需要 WSL —— 这正是事件层解耦换来的可测性。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from textual.widgets import Input, RichLog

from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.audit.logger import now_iso
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
from coding_agent.memory.sessions import SessionInfo
from coding_agent.runtime import HistoryMessage
from coding_agent.sandbox.snapshots import RestoreResult, SnapshotEntry
from coding_agent.tui.app import AgentTuiApp

PLAN = ["列出工作区文件", "读取 policy.py", "总结分级规则"]


class FakeSessions:
    def __init__(self, items: list) -> None:
        self._items = items

    def list(self, *, limit: int = 20):
        return self._items[:limit]


class FakeRuntime:
    """按脚本回放事件，并可断言收到的 prompt。"""

    workspace = "/mnt/d/proj"

    def __init__(
        self,
        events: list,
        *,
        audit_path: Path | None = None,
        resume_events: list | None = None,
        snapshots: list | None = None,
        diff_text: str = "",
        restore_result: RestoreResult | None = None,
        sessions: list | None = None,
        memories: list | None = None,
        history: list | None = None,
    ) -> None:
        self._events = events
        self._resume_events = resume_events or []
        self.audit_path = audit_path or Path("nonexistent.jsonl")
        self._snapshots = snapshots or []
        self._diff_text = diff_text
        self._restore_result = restore_result or RestoreResult(ok=True, message="已回滚")
        self.sessions = FakeSessions(sessions or [])
        self.memory = SimpleNamespace(path="/mnt/d/proj/.agent/memory.md")
        self._memories = list(memories or [])
        self._history = history or []
        self.switched: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.resumed: list[tuple[str, dict[str, bool]]] = []
        self.restore_calls: list[tuple[str | None, str | None]] = []

    def memories(self) -> list[str]:
        return list(self._memories)

    def remember(self, text: str) -> list[str]:
        self._memories.append(text)
        return self.memories()

    def forget(self, index: int) -> list[str]:
        if index < 1 or index > len(self._memories):
            raise ValueError(f"序号超出范围：{index}")
        self._memories.pop(index - 1)
        return self.memories()

    async def history(self, thread_id: str):
        self.switched.append(thread_id)
        return self._history

    async def run(self, prompt: str, *, thread_id: str):
        self.calls.append((prompt, thread_id))
        for event in self._events:
            yield event

    async def resume(self, thread_id: str, decisions: dict[str, bool]):
        self.resumed.append((thread_id, decisions))
        for event in self._resume_events:
            yield event

    def audit_files(self) -> list[Path]:
        return [self.audit_path]

    def list_snapshots(self, *, limit: int = 20):
        return self._snapshots[:limit]

    def diff_snapshot(self, *, path=None, snapshot_id=None) -> str:
        return self._diff_text

    def restore(self, *, path=None, snapshot_id=None) -> RestoreResult:
        self.restore_calls.append((snapshot_id, path))
        return self._restore_result


SCRIPT = [
    PlanCreated(steps=PLAN),
    StepStarted(index=0, total=3, text=PLAN[0]),
    AssistantToken(node="act", text="我先看看"),
    ToolCallStarted(name="shell_exec", args={"command": "ls"}, summary="ls", level="L0 只读"),
    ToolCallFinished(name="shell_exec", ok=True, exit_code=0, duration_ms=40),
    AssistantToken(node="act", text="目录为空。"),
    StepFinished(index=0, text="目录为空。"),
    StepStarted(index=1, total=3, text=PLAN[1]),
    ToolCallStarted(name="shell_exec", args={"command": "cat policy.py"}, summary="cat policy.py",
                    level="L0 只读"),
    ToolCallFinished(name="shell_exec", ok=False, rejected=True, level="L3 危险"),
    StepFinished(index=1, text="", budget_exhausted=True),
    StepStarted(index=2, total=3, text=PLAN[2]),
    AssistantToken(node="respond", text="共四级。"),
    RunFinished(thread_id="t", answer="共四级。"),
]


def _transcript_text(app: AgentTuiApp) -> str:
    log = app.query_one("#transcript", RichLog)
    return "\n".join(strip.text for strip in log.lines)


def _widget_text(app: AgentTuiApp, selector: str) -> str:
    """Static.render() 返回 textual 的 Content，其 __str__ 即纯文本。"""
    return str(app.query_one(selector).render())


def _plan_text(app: AgentTuiApp) -> str:
    return _widget_text(app, "#plan")


def _stream_text(app: AgentTuiApp) -> str:
    return _widget_text(app, "#streaming")


def _run(coro) -> None:
    asyncio.run(coro)


def test_scripted_run_renders_everything() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(SCRIPT)
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "策略分几级？"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            text = _transcript_text(app)

            assert "你 › 策略分几级？" in text
            assert "第 1/3 步" in text
            assert "第 3/3 步" in text
            assert "→ [L0 只读] shell_exec: ls" in text
            assert "← ✓ 0.04s" in text
            assert "← 已被安全策略拒绝" in text
            assert "（本步骤工具预算耗尽）" in text
            assert "我先看看" in text  # 流式 token 最终落到时间线
            assert "共四级。" in text

            assert runtime.calls[0][0] == "策略分几级？"

    _run(scenario())


def test_plan_panel_marks_progress() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(SCRIPT)
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "go"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            plan = _plan_text(app)
            assert "1. 列出工作区文件" in plan
            assert "3. 总结分级规则" in plan
            assert "✓" in plan  # 已完成的步骤
            assert "▶" in plan  # 当前步骤

    _run(scenario())


def test_streaming_buffer_is_flushed_not_left_dangling() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(SCRIPT))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "go"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            # 收尾后不应有残留的半截文本
            assert _stream_text(app) == ""

    _run(scenario())


def test_file_changed_is_rendered_with_diff_stat() -> None:
    async def scenario() -> None:
        events = [
            ToolCallStarted(name="file_edit", args={"path": "a.py"}, summary="a.py"),
            ToolCallFinished(name="file_edit", ok=True, level="文件工具"),
            FileChanged(path="a.py", action="edit", added=3, removed=1, snapshot_id="20261002-abc"),
            RunFinished(thread_id="t"),
        ]
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(events))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "改一下"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            text = _transcript_text(app)
            assert "✎ edit a.py" in text
            assert "+3 -1" in text
            assert "备份 20261002-abc" in text

    _run(scenario())


def test_verification_failure_is_rendered_with_issues() -> None:
    async def scenario() -> None:
        events = [
            Verification(
                status="failed",
                command="python3 -m pytest -q",
                ok=False,
                summary="1 failed, 2 passed in 0.05s",
                issues=["tests/test_math.py:5 assert 2 == 3"],
            ),
            RunFinished(thread_id="t"),
        ]
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(events))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "改一下"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            text = _transcript_text(app)
            assert "验证失败" in text
            assert "1 failed, 2 passed" in text
            assert "tests/test_math.py:5" in text

    _run(scenario())


def test_verification_pass_is_rendered() -> None:
    async def scenario() -> None:
        events = [
            Verification(status="ok", command="pytest -q", ok=True, summary="3 passed"),
            RunFinished(thread_id="t"),
        ]
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(events))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "改一下"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert "验证通过  3 passed" in _transcript_text(app)

    _run(scenario())


def test_verification_not_configured_is_rendered() -> None:
    async def scenario() -> None:
        events = [Verification(status="not_configured"), RunFinished(thread_id="t")]
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(events))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "改一下"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert "未检测到可用的测试命令" in _transcript_text(app)

    _run(scenario())


def test_repair_attempt_is_rendered() -> None:
    async def scenario() -> None:
        events = [
            Verification(status="failed", command="pytest -q", ok=False,
                         summary="1 failed", issues=["a.py:1 boom"]),
            RepairStarted(attempt=1, limit=3, summary="1 failed", issues=["a.py:1 boom"]),
            Verification(status="ok", command="pytest -q", ok=True, summary="1 passed"),
            RunFinished(thread_id="t"),
        ]
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(events))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "修好它"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            text = _transcript_text(app)
            assert "↻ 第 1/3 次修复" in text
            assert "验证通过  1 passed" in text

    _run(scenario())


# ---------------- 最终答复的分界 ----------------

def test_final_answer_gets_a_separator() -> None:
    """respond 与 act 内容常有重叠（每步小结 + 最终汇总），
    没有分界读者会当成重复输出。"""

    async def scenario() -> None:
        events = [
            StepStarted(index=0, total=1, text="看代码"),
            AssistantToken(node="act", text="这一步看到 load_config。"),
            StepFinished(index=0, text="这一步看到 load_config。"),
            AssistantToken(node="respond", text="结论是 load_config。"),
            RunFinished(thread_id="t"),
        ]
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(events))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "看看"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            text = _transcript_text(app)
            assert "── 最终答复 ──" in text
            # 分界在最终答复之前
            assert text.index("── 最终答复 ──") < text.index("结论是 load_config")

    _run(scenario())


def test_separator_appears_once() -> None:
    async def scenario() -> None:
        events = [
            AssistantToken(node="act", text="甲"),
            AssistantToken(node="respond", text="乙"),
            AssistantToken(node="respond", text="丙"),
            RunFinished(thread_id="t"),
        ]
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(events))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "看看"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert _transcript_text(app).count("── 最终答复 ──") == 1

    _run(scenario())


def test_no_separator_when_only_act_streams() -> None:
    async def scenario() -> None:
        events = [
            AssistantToken(node="act", text="只有步骤小结"),
            RunFinished(thread_id="t"),
        ]
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(events))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "看看"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert "── 最终答复 ──" not in _transcript_text(app)

    _run(scenario())


def test_separator_resets_for_a_new_session() -> None:
    """新会话后应能再次出现分界，而不是被上一轮的标记吃掉。"""

    async def scenario() -> None:
        events = [AssistantToken(node="respond", text="第一次答复"), RunFinished(thread_id="t")]
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime(events))

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "第一次"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert _transcript_text(app).count("── 最终答复 ──") == 1

            prompt.value = "/new"
            await pilot.press("enter")
            await pilot.pause()
            prompt.value = "第二次"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert _transcript_text(app).count("── 最终答复 ──") == 1

    _run(scenario())


def test_run_failed_is_surfaced() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([RunFailed(message="boom")]))

        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "go"
            await pilot.press("enter")
            await pilot.pause()

            assert "运行失败：boom" in _transcript_text(app)

    _run(scenario())


def test_slash_commands() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime([])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)

            prompt.value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            assert "/workspace" in _transcript_text(app)

            prompt.value = "/workspace"
            await pilot.press("enter")
            await pilot.pause()
            assert "/mnt/d/proj" in _transcript_text(app)

            prompt.value = "/nope"
            await pilot.press("enter")
            await pilot.pause()
            assert "未知命令：/nope" in _transcript_text(app)

            prompt.value = "/new"
            await pilot.press("enter")
            await pilot.pause()
            assert "已开始新会话" in _transcript_text(app)

            # 斜杠命令不应触发模型调用
            assert runtime.calls == []

    _run(scenario())


def test_audit_command_shows_current_thread_records(tmp_path) -> None:
    async def scenario() -> None:
        logger = AuditLogger(tmp_path)
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([], audit_path=logger.path))

        async with app.run_test() as pilot:
            # 本会话与另一会话各写一条，只应显示本会话的
            for thread_id, tool in ((app.session_id, "shell_exec"), ("other", "file_edit")):
                logger.write(
                    AuditRecord(
                        ts=now_iso(),
                        kind="tool_call",
                        thread_id=thread_id,
                        tool=tool,
                        level="L0 只读",
                        decision="auto",
                        ok=True,
                    )
                )

            prompt = app.query_one("#prompt", Input)
            prompt.value = "/audit"
            await pilot.press("enter")
            await pilot.pause()

            text = _transcript_text(app)
            assert "审计日志" in text
            assert "shell_exec" in text
            assert "file_edit" not in text

    _run(scenario())


def test_audit_command_with_no_records(tmp_path) -> None:
    async def scenario() -> None:
        app = AgentTuiApp(
            runtime_factory=lambda: FakeRuntime([], audit_path=tmp_path / "empty.jsonl")
        )
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/audit"
            await pilot.press("enter")
            await pilot.pause()
            assert "还没有审计记录" in _transcript_text(app)

    _run(scenario())


# ---------------- 审批弹窗 ----------------

APPROVAL = ApprovalRequested(
    request_id="c1",
    tool="shell_exec",
    command="pip install requests",
    level="L2 变更性",
    reason="装依赖",
)


async def _trigger_approval(pilot, app: AgentTuiApp, answer: str):
    prompt = app.query_one("#prompt", Input)
    prompt.value = "装个包"
    await pilot.press("enter")
    await pilot.pause()
    await pilot.pause()
    assert type(app.screen).__name__ == "ApprovalScreen", "应当弹出审批确认"
    await pilot.press(answer)
    await pilot.pause()
    await pilot.pause()


def test_approval_modal_approves_and_resumes() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(
            [APPROVAL, RunFinished(thread_id="t")],
            resume_events=[
                ToolCallFinished(call_id="c1", name="shell_exec", ok=True, decision="approved"),
                RunFinished(thread_id="t"),
            ],
        )
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _trigger_approval(pilot, app, "y")

            assert runtime.resumed == [(app.session_id, {"c1": True})]
            text = _transcript_text(app)
            assert "需要确认（L2 变更性）：pip install requests" in text
            assert "← ✓" in text

    _run(scenario())


def test_approval_modal_denies_and_resumes() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(
            [APPROVAL],
            resume_events=[
                ToolCallFinished(
                    call_id="c1", name="shell_exec", ok=False, rejected=True, decision="denied"
                ),
                RunFinished(thread_id="t"),
            ],
        )
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _trigger_approval(pilot, app, "n")

            assert runtime.resumed == [(app.session_id, {"c1": False})]
            assert "← 已被用户拒绝" in _transcript_text(app)

    _run(scenario())


def test_escape_key_denies() -> None:
    """默认动作必须是拒绝 —— 危险操作不该靠一次误触放行。"""

    async def scenario() -> None:
        runtime = FakeRuntime([APPROVAL])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _trigger_approval(pilot, app, "escape")
            assert runtime.resumed == [(app.session_id, {"c1": False})]

    _run(scenario())


def test_multiple_requests_prompt_sequentially() -> None:
    async def scenario() -> None:
        second = ApprovalRequested(
            request_id="c2", tool="shell_exec", command="git commit -m x", level="L2 变更性"
        )
        runtime = FakeRuntime([APPROVAL, second])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "干活"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            await pilot.press("y")  # 第一条批准
            await pilot.pause()
            await pilot.pause()
            assert type(app.screen).__name__ == "ApprovalScreen", "第二条也应弹窗"
            await pilot.press("n")  # 第二条拒绝
            await pilot.pause()
            await pilot.pause()

            assert runtime.resumed == [(app.session_id, {"c1": True, "c2": False})]

    _run(scenario())


# ---------------- 回滚命令 ----------------

def _drive_command(app: AgentTuiApp, pilot, command: str):
    prompt = app.query_one("#prompt", Input)

    async def _run_it():
        prompt.value = command
        await pilot.press("enter")
        await pilot.pause()

    return _run_it()


def test_snapshots_command_lists_entries() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(
            [],
            snapshots=[
                SnapshotEntry(snapshot_id="20261003T024018-aaa111", path="a.py"),
                SnapshotEntry(snapshot_id="20261003T024000-bbb222", path="b.py"),
            ],
        )
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/snapshots")
            text = _transcript_text(app)
            assert "20261003T024018-aaa111" in text
            assert "b.py" in text

    _run(scenario())


def test_snapshots_command_when_empty() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([]))
        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/snapshots")
            assert "还没有任何快照" in _transcript_text(app)

    _run(scenario())


def test_diff_command_renders_diff() -> None:
    async def scenario() -> None:
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([], diff_text=diff))

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/diff")
            text = _transcript_text(app)
            assert "-old" in text
            assert "+new" in text
            assert "@@" in text

    _run(scenario())


def test_diff_command_when_no_changes() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([], diff_text=""))
        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/diff")
            assert "没有可显示的快照" in _transcript_text(app)

    _run(scenario())


def test_undo_without_args_rolls_back_latest() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(
            [],
            restore_result=RestoreResult(
                ok=True,
                path="a.py",
                snapshot_id="20261003T024018-aaa111",
                undo_snapshot_id="20261003T024100-ccc333",
                added=1,
                removed=1,
                diff="--- a/a.py\n+++ b/a.py\n-old\n+new\n",
                message="已回滚 a.py",
            ),
        )
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/undo")
            assert runtime.restore_calls == [(None, None)]
            text = _transcript_text(app)
            assert "已回滚 a.py" in text
            assert "回滚前的状态已留底：20261003T024100-ccc333" in text
            assert "-old" in text

    _run(scenario())


def test_undo_parses_snapshot_id_vs_path() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime([])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/undo 20261003T024018-aaa111")
            await _drive_command(app, pilot, "/undo src/a.py")

            assert runtime.restore_calls == [
                ("20261003T024018-aaa111", None),
                (None, "src/a.py"),
            ]

    _run(scenario())


def test_undo_reports_failure() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(
            [], restore_result=RestoreResult(ok=False, message="工作区里还没有任何快照，无法回滚。")
        )
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/undo")
            assert "还没有任何快照" in _transcript_text(app)

    _run(scenario())


# ---------------- 会话与记忆 ----------------

def _session(thread_id: str, title: str, last: str = "2026-10-03 03:00:00") -> SessionInfo:
    return SessionInfo(
        thread_id=thread_id,
        title=title,
        workspace="/mnt/d/proj",
        started_at="2026-10-03T03:00:00+00:00",
        last_active="2026-10-03T03:00:00+00:00",
        prompts=1,
        tool_calls=2,
    )


def test_sessions_command_lists_with_current_marker() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime([])
        app = AgentTuiApp(runtime_factory=lambda: runtime)
        # 会话 id 在构造时就确定了，据此构造列表才能验证「当前会话」标记
        runtime.sessions = FakeSessions(
            [_session(app.session_id, "当前会话"), _session("aaa111", "改一下 README")]
        )

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/sessions")
            text = _transcript_text(app)
            assert "aaa111" in text
            assert "改一下 README" in text
            assert "▶" in text  # 当前会话标记

    _run(scenario())


def test_sessions_command_when_empty() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([]))
        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/sessions")
            assert "没有历史会话记录" in _transcript_text(app)

    _run(scenario())


def test_switch_loads_history_into_transcript() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(
            [],
            history=[
                HistoryMessage("user", "之前问的问题"),
                HistoryMessage("assistant", "之前的回答"),
            ],
        )
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/switch aaa111")
            await pilot.pause()
            await pilot.pause()

            assert app.session_id == "aaa111"
            assert runtime.switched == ["aaa111"]
            text = _transcript_text(app)
            assert "你 › 之前问的问题" in text
            assert "助手 › 之前的回答" in text

    _run(scenario())


def test_switch_without_argument_shows_usage() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([]))
        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/switch")
            assert "用法：/switch" in _transcript_text(app)

    _run(scenario())


def test_switch_to_empty_session() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([], history=[]))
        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/switch empty123")
            await pilot.pause()
            await pilot.pause()
            assert "暂无历史记录" in _transcript_text(app)

    _run(scenario())


def test_memory_command_lists_facts() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime([], memories=["用 pytest", "别动 legacy/"])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/memory")
            text = _transcript_text(app)
            assert "1. 用 pytest" in text
            assert "2. 别动 legacy/" in text
            assert "memory.md" in text

    _run(scenario())


def test_memory_command_when_empty() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([]))
        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/memory")
            assert "当前没有项目记忆" in _transcript_text(app)

    _run(scenario())


def test_remember_adds_fact() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime([])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/remember 这个仓库用 uv")
            assert runtime.memories() == ["这个仓库用 uv"]
            assert "已记住" in _transcript_text(app)

    _run(scenario())


def test_remember_without_text_shows_usage() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([]))
        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/remember")
            assert "用法：/remember" in _transcript_text(app)

    _run(scenario())


def test_forget_removes_fact() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime([], memories=["第一条", "第二条"])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/forget 1")
            assert runtime.memories() == ["第二条"]
            assert "已删除第 1 条" in _transcript_text(app)

    _run(scenario())


def test_forget_bad_index_is_reported() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime([], memories=["只有一条"])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            await _drive_command(app, pilot, "/forget 9")
            assert "序号超出范围" in _transcript_text(app)

            await _drive_command(app, pilot, "/forget abc")
            assert "用法：/forget" in _transcript_text(app)

    _run(scenario())


# ---------------- 会话信息与忙碌状态 ----------------

def test_sidebar_shows_session_workspace_and_permission() -> None:
    """「我现在有没有写权限」是每次操作前都该看得见的信息。"""

    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([]))
        async with app.run_test() as pilot:
            await pilot.pause()
            info = _widget_text(app, "#session-info")
            assert app.session_id in info
            assert "/mnt/d/proj" in info
            assert "L0 只读" in info

    _run(scenario())


def test_sidebar_reflects_write_permission() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([]), allow_write=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "L1 可写" in _widget_text(app, "#session-info")

    _run(scenario())


def test_sidebar_updates_on_new_session() -> None:
    async def scenario() -> None:
        app = AgentTuiApp(runtime_factory=lambda: FakeRuntime([]))
        async with app.run_test() as pilot:
            await pilot.pause()
            before = app.session_id

            await _drive_command(app, pilot, "/new")

            assert app.session_id != before
            assert app.session_id in _widget_text(app, "#session-info")

    _run(scenario())


def test_busy_state_marks_the_input() -> None:
    """执行中要看得见 —— 否则用户会以为界面卡住了。"""

    async def scenario() -> None:
        release = asyncio.Event()

        class SlowRuntime(FakeRuntime):
            async def run(self, prompt: str, *, thread_id: str):
                self.calls.append((prompt, thread_id))
                await release.wait()
                yield RunFinished(thread_id=thread_id)

        app = AgentTuiApp(runtime_factory=lambda: SlowRuntime([]))
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "跑个长任务"
            await pilot.press("enter")
            await pilot.pause()

            assert "执行中" in app.sub_title
            assert prompt.has_class("busy")

            release.set()
            await pilot.pause()
            assert "执行中" not in app.sub_title
            assert not prompt.has_class("busy")

    _run(scenario())


def test_input_cleared_and_blank_ignored() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime([])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)

            prompt.value = "   "
            await pilot.press("enter")
            await pilot.pause()
            assert runtime.calls == []

            prompt.value = "真指令"
            await pilot.press("enter")
            await pilot.pause()
            assert prompt.value == ""
            assert runtime.calls[0][0] == "真指令"

    _run(scenario())


def test_busy_state_rejects_second_prompt() -> None:
    async def scenario() -> None:
        blocking = asyncio.Event()

        class BlockingRuntime(FakeRuntime):
            async def run(self, prompt: str, *, thread_id: str):
                self.calls.append((prompt, thread_id))
                await blocking.wait()
                yield RunFinished(thread_id=thread_id)

        runtime = BlockingRuntime([])
        app = AgentTuiApp(runtime_factory=lambda: runtime)

        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)

            prompt.value = "第一条"
            await pilot.press("enter")
            await pilot.pause()

            prompt.value = "第二条"
            await pilot.press("enter")
            await pilot.pause()
            assert "当前任务执行中" in _transcript_text(app)
            assert len(runtime.calls) == 1

            blocking.set()
            await pilot.pause()

    _run(scenario())
