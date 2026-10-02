"""Textual 终端界面。

只消费 AgentRuntime 产出的领域事件 —— 不认识 LangGraph，也不持有工具与沙箱
（见 tests/unit/test_architecture.py 的约束测试）。因此 TUI 无法绕过命令审批。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from coding_agent.audit import read_records
from coding_agent.config import Settings, get_settings
from coding_agent.events import (
    AssistantToken,
    Event,
    FileChanged,
    PlanCreated,
    RunFailed,
    RunFinished,
    StepFinished,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from coding_agent.runtime import AgentRuntime

HELP_TEXT = """\
/help        显示本帮助
/new         开始新会话（换 thread_id，清空时间线）
/clear       清空时间线
/workspace   显示当前工作区
/audit [n]   显示本会话最近 n 条审计记录（默认 10）
/quit        退出

快捷键  Ctrl+Q 退出 · Ctrl+N 新会话 · Ctrl+L 清屏
"""


class PlanPanel(Static):
    """计划面板：显示子任务与当前进度。"""

    def render_plan(self, steps: list[str], current: int) -> None:
        if not steps:
            self.update(Text("（尚未规划）", style="dim"))
            return
        lines = Text()
        for i, step in enumerate(steps):
            if i < current:
                style, mark = "dim", "✓"
            elif i == current:
                style, mark = "bold cyan", "▶"
            else:
                style, mark = "dim", "·"
            lines.append(f" {mark} {i + 1}. {step}\n", style=style)
        self.update(lines)


class AgentTuiApp(App[None]):
    """终端原生编程智能体的 TUI。"""

    TITLE = "CodingAgent"

    CSS = """
    #body { height: 1fr; }
    #sidebar {
        width: 36;
        border-right: solid $panel;
        padding: 0 1;
    }
    #sidebar-title { text-style: bold; color: $accent; height: 1; }
    #main { width: 1fr; }
    #transcript { height: 1fr; padding: 0 1; }
    #streaming { height: auto; max-height: 50%; padding: 0 1; color: $text; }
    #prompt { dock: bottom; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+n", "new_session", "新会话"),
        ("ctrl+l", "clear_log", "清屏"),
    ]

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        workspace: str | None = None,
        allow_write: bool = False,
        runtime_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._workspace = workspace
        self._allow_write = allow_write
        self._runtime_factory = runtime_factory

        self._runtime: Any = None
        self._session_id = uuid.uuid4().hex[:8]
        self._plan: list[str] = []
        self._step_idx = 0
        self._total = 1
        self._stream: list[str] = []
        self._busy = False

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("计划", id="sidebar-title")
                yield PlanPanel(id="plan")
            with Vertical(id="main"):
                yield RichLog(id="transcript", markup=False, wrap=True, highlight=False)
                yield Static(id="streaming")
        yield Input(placeholder="输入指令，/help 查看命令…", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._plan_panel.render_plan([], 0)
        try:
            self._runtime = (
                self._runtime_factory()
                if self._runtime_factory
                else AgentRuntime(
                    self._settings or get_settings(),
                    workspace=self._workspace,
                    allow_write=self._allow_write,
                )
            )
            workspace = self._runtime.workspace
        except Exception as exc:  # noqa: BLE001 - 环境问题要在界面里说清楚，而不是崩掉
            self._write(f"初始化失败：{type(exc).__name__}: {exc}", "red")
            self._write("请先运行 agent doctor 检查环境。", "dim")
            return

        self.sub_title = f"会话 {self._session_id} · {workspace}"
        level = "L1（可写）" if self._allow_write else "L0（只读）"
        self._write(f"工作区 {workspace}    权限 {level}", "dim")
        self._write("输入指令开始，/help 查看可用命令。", "dim")
        self.query_one("#prompt", Input).focus()

    @property
    def session_id(self) -> str:
        """当前会话的 thread_id。

        刻意不叫 thread_id —— Textual 的 App 自带同名属性（运行线程 id），
        覆盖它会同时踩坏两边。
        """
        return self._session_id

    @property
    def _plan_panel(self) -> PlanPanel:
        return self.query_one("#plan", PlanPanel)

    @property
    def _transcript(self) -> RichLog:
        return self.query_one("#transcript", RichLog)

    # ------------------------------------------------------------------
    # 事件 → 界面
    # ------------------------------------------------------------------

    def _write(self, text: str, style: str = "") -> None:
        self._transcript.write(Text(text, style=style) if style else Text(text))

    def _render_stream(self) -> None:
        self.query_one("#streaming", Static).update(Text("".join(self._stream)))

    def _flush_stream(self) -> None:
        if not self._stream:
            return
        self._write("".join(self._stream), "green")
        self._stream.clear()
        self.query_one("#streaming", Static).update("")

    def _apply(self, event: Event) -> None:
        if isinstance(event, PlanCreated):
            self._plan = list(event.steps)
            self._step_idx = 0
            self._plan_panel.render_plan(self._plan, 0)

        elif isinstance(event, StepStarted):
            self._flush_stream()
            self._step_idx = event.index
            self._total = event.total
            self._plan_panel.render_plan(self._plan, event.index)
            if event.total > 1:
                self._write(f"第 {event.index + 1}/{event.total} 步  {event.text}", "cyan")

        elif isinstance(event, StepFinished):
            self._flush_stream()
            if event.budget_exhausted:
                self._write("（本步骤工具预算耗尽）", "yellow")

        elif isinstance(event, AssistantToken):
            self._stream.append(event.text)
            self._render_stream()

        elif isinstance(event, ToolCallStarted):
            self._flush_stream()
            level = f"[{event.level}] " if event.level else ""
            self._write(f"→ {level}{event.name}: {event.summary}", "dim")

        elif isinstance(event, ToolCallFinished):
            if event.rejected:
                self._write("← 已被安全策略拒绝", "red")
            elif event.ok:
                elapsed = f" {event.duration_ms / 1000:.2f}s" if event.duration_ms else ""
                self._write(f"← ✓{elapsed}", "dim")
            elif event.exit_code is not None:
                self._write(f"← ✗ exit={event.exit_code}", "yellow")
            else:
                self._write("← ✗", "yellow")

        elif isinstance(event, FileChanged):
            stamp = f" · 备份 {event.snapshot_id}" if event.snapshot_id else ""
            self._write(
                f"✎ {event.action} {event.path}  +{event.added} -{event.removed}{stamp}",
                "magenta",
            )

        elif isinstance(event, RunFailed):
            self._flush_stream()
            self._write(f"运行失败：{event.message}", "red")

        elif isinstance(event, RunFinished):
            self._flush_stream()

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def _execute(self, prompt: str) -> None:
        self._busy = True
        try:
            async for event in self._runtime.run(prompt, thread_id=self._session_id):
                self._apply(event)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要显示出来而不是静默
            self._flush_stream()
            self._write(f"运行异常：{type(exc).__name__}: {exc}", "red")
        finally:
            self._busy = False

    @on(Input.Submitted)
    def _on_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if self._runtime is None:
            self._write("运行时未就绪，无法执行。", "red")
            return
        if text.startswith("/"):
            self._command(text)
            return
        if self._busy:
            self._write("（当前任务执行中，请等待完成）", "yellow")
            return

        self._write(f"你 › {text}", "bold cyan")
        self._execute(text)

    # ------------------------------------------------------------------
    # 斜杠命令
    # ------------------------------------------------------------------

    def _command(self, text: str) -> None:
        name, _, rest = text.partition(" ")
        name = name.lower()

        if name in {"/quit", "/exit", "/q"}:
            self.exit()
        elif name == "/help":
            self._write(HELP_TEXT.rstrip(), "")
        elif name == "/clear":
            self._transcript.clear()
        elif name == "/new":
            self.action_new_session()
        elif name == "/workspace":
            self._write(f"工作区 {self._runtime.workspace}", "")
        elif name == "/audit":
            self._show_audit(rest)
        else:
            self._write(f"未知命令：{name}。输入 /help 查看可用命令。", "yellow")

    def _show_audit(self, raw_limit: str) -> None:
        try:
            limit = max(int(raw_limit.strip() or 10), 1)
        except ValueError:
            self._write("用法：/audit [条数]", "yellow")
            return

        path = self._runtime.audit_path
        records = read_records(path, thread_id=self._session_id, limit=limit)
        if not records:
            self._write(f"本会话还没有审计记录：{path}", "dim")
            return

        self._write(f"审计日志 {path}（最近 {len(records)} 条）", "dim")
        for record in records:
            style = "red" if record.decision == "rejected" else "dim"
            target = record.tool or record.path or ""
            detail = f"{record.kind:<12} {target:<16} {record.level:<10} {record.decision}"
            self._write(f"  {record.ts[11:19]}  {detail}", style)

    def action_new_session(self) -> None:
        self._session_id = uuid.uuid4().hex[:8]
        self._plan = []
        self._step_idx = 0
        self._stream.clear()
        self._plan_panel.render_plan([], 0)
        self.query_one("#streaming", Static).update("")
        self._transcript.clear()
        if self._runtime is not None:
            self.sub_title = f"会话 {self._session_id} · {self._runtime.workspace}"
        self._write(f"已开始新会话 {self._session_id}", "dim")

    def action_clear_log(self) -> None:
        self._transcript.clear()
