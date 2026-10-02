"""TUI 无头测试：注入假的 runtime，验证「事件 → 界面」的映射。

不需要 API Key，也不需要 WSL —— 这正是事件层解耦换来的可测性。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Input, RichLog

from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.audit.logger import now_iso
from coding_agent.events import (
    AssistantToken,
    FileChanged,
    PlanCreated,
    RunFailed,
    RunFinished,
    StepFinished,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from coding_agent.tui.app import AgentTuiApp

PLAN = ["列出工作区文件", "读取 policy.py", "总结分级规则"]


class FakeRuntime:
    """按脚本回放事件，并可断言收到的 prompt。"""

    workspace = "/mnt/d/proj"

    def __init__(self, events: list, *, audit_path: Path | None = None) -> None:
        self._events = events
        self.audit_path = audit_path or Path("nonexistent.jsonl")
        self.calls: list[tuple[str, str]] = []

    async def run(self, prompt: str, *, thread_id: str):
        self.calls.append((prompt, thread_id))
        for event in self._events:
            yield event


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
