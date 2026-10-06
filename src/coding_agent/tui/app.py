"""Textual 终端界面。

只消费 AgentRuntime 产出的领域事件 —— 不认识 LangGraph，也不持有工具与沙箱
（见 tests/unit/test_architecture.py 的约束测试）。因此 TUI 无法绕过命令审批。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, RichLog, Static

from coding_agent.audit import read_records_many
from coding_agent.config import Settings, get_settings
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    Event,
    FileChanged,
    PlanCreated,
    PlanRevised,
    RepairStarted,
    ReviewFinished,
    RunFailed,
    RunFinished,
    RunStarted,
    StepFinished,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
    Verification,
)
from coding_agent.runtime import AgentRuntime

HELP_TEXT = """\
/help              显示本帮助
/new               开始新会话（换 thread_id，清空时间线）
/clear             清空时间线
/workspace         显示当前工作区
/audit [n]         显示本会话最近 n 条审计记录（默认 10）
/snapshots [n]     列出文件快照（每次写入/编辑前的留底）
/diff [快照id|路径] 看某次改动之后文件变成了什么样
/undo [快照id|路径] 回滚到某次改动前（不带参数则回滚最近一次）
/sessions [n]      列出历史会话
/switch <会话id>   切换到某个历史会话（会载入它的对话记录）
/memory            查看项目记忆
/remember <事实>   记下一条跨会话的项目事实
/forget <序号>     删除第 N 条记忆
/quit              退出

快捷键  Ctrl+Q 退出 · Ctrl+N 新会话 · Ctrl+L 清屏
"""

# 快照 id 形如 20261003T024018123456-a1b2c3（时间戳精确到微秒）；不符合就当路径处理
_SNAPSHOT_ID_RE = re.compile(r"^\d{8}T\d{6,18}-[0-9a-f]{6}$")


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


class ApprovalScreen(ModalScreen[bool]):
    """单条审批请求。返回 True 表示批准。

    默认动作是拒绝（Esc 也是拒绝）—— 危险操作不该靠一次误触放行。
    """

    BINDINGS = [
        ("y", "answer(True)", "批准"),
        ("n", "answer(False)", "拒绝"),
        ("escape", "answer(False)", "拒绝"),
    ]

    CSS = """
    ApprovalScreen { align: center middle; }
    #approval-box {
        width: 80%;
        max-width: 100;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #approval-level { color: $warning; text-style: bold; }
    #approval-command { background: $boost; padding: 0 1; margin: 1 0; }
    #approval-hint { color: $text-muted; }
    """

    def __init__(self, request: ApprovalRequested, position: str = "") -> None:
        super().__init__()
        self.request = request
        self.position = position

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            yield Static(f"需要确认 {self.position}", id="approval-level")
            yield Static(self.request.command, id="approval-command")
            if self.request.reason:
                yield Static(f"理由：{self.request.reason}")
            yield Static(f"等级：{self.request.level}    工具：{self.request.tool}")
            yield Static("[y] 批准    [n] / Esc 拒绝", id="approval-hint")

    def action_answer(self, approved: bool) -> None:
        self.dismiss(approved)


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
    .sidebar-title { text-style: bold; color: $accent; height: 1; }
    #session-info { color: $text-muted; height: auto; padding-bottom: 1; }
    #main { width: 1fr; }
    #transcript { height: 1fr; padding: 0 1; }
    #streaming { height: auto; max-height: 50%; padding: 0 1; color: $text; }
    #prompt { dock: bottom; }
    #prompt.busy { border: tall $warning; }
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
        self._answering = False
        self._busy = False

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("会话", classes="sidebar-title")
                yield Static("", id="session-info")
                yield Static("计划", classes="sidebar-title")
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

        self.sub_title = f"会话 {self._session_id}"
        self._render_session_info(workspace)
        self._write("输入指令开始，/help 查看可用命令。", "dim")
        self.query_one("#prompt", Input).focus()

    def _render_session_info(self, workspace: str | None = None) -> None:
        """把会话、工作区、权限放进侧栏。

        之前只写在 Header 的副标题里，容易被忽略 ——
        而"我现在到底有没有写权限"是每次操作前都该看得见的信息。
        """
        if workspace is None:
            workspace = getattr(self._runtime, "workspace", "") or ""
        level = "L1 可写" if self._allow_write else "L0 只读"
        self._session_workspace = workspace
        info = Text()
        info.append(f"{self._session_id}\n", style="")
        info.append(f"{workspace}\n", style="dim")
        info.append(level, style="bold yellow" if self._allow_write else "dim")
        self.query_one("#session-info", Static).update(info)

    def _set_busy(self, value: bool) -> None:
        """忙碌状态要看得见：输入框描边变色，并在副标题上标出来。"""
        self._busy = value
        prompt = self.query_one("#prompt", Input)
        prompt.set_class(value, "busy")
        self.sub_title = f"会话 {self._session_id}" + ("（执行中…）" if value else "")

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
        if isinstance(event, RunStarted):
            # 每轮运行的新起点：清掉上一轮遗留的流式缓冲与「最终答复」标记。
            # 不复位的话，同一会话的第二轮起 respond 的文本就不再打分隔标题，
            # 会和 act 的每步小结混成一片。
            self._flush_stream()
            self._answering = False
            self._plan = []
            self._step_idx = 0
            self._total = 1
            self._plan_panel.render_plan([], 0)

        elif isinstance(event, PlanCreated):
            self._plan = list(event.steps)
            self._step_idx = 0
            self._plan_panel.render_plan(self._plan, 0)
            if event.degraded:
                self._write("! 规划未解析，已退化为单步执行", "yellow")

        elif isinstance(event, PlanRevised):
            self._flush_stream()
            self._plan = list(event.steps)
            self._step_idx = event.step_idx
            self._total = max(len(self._plan), 1)
            self._plan_panel.render_plan(self._plan, event.step_idx)
            self._write("↻ 计划已调整", "yellow")

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
            elif event.cancelled:
                self._write("（本步骤未执行完，已放弃）", "yellow")

        elif isinstance(event, AssistantToken):
            # respond 与 act 的内容常有重叠（架构上就是「每步小结 + 最终汇总」），
            # 给最终答复一个明确起点，读者才不会当成重复输出
            if event.node == "respond" and not self._answering:
                self._answering = True
                self._flush_stream()
                self._write("── 最终答复 ──", "dim")
            self._stream.append(event.text)
            self._render_stream()

        elif isinstance(event, ToolCallStarted):
            self._flush_stream()
            level = f"[{event.level}] " if event.level else ""
            self._write(f"→ {level}{event.name}: {event.summary}", "dim")

        elif isinstance(event, ToolCallFinished):
            if event.decision == "denied":
                self._write("← 已被用户拒绝", "red")
            elif event.rejected:
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

        elif isinstance(event, Verification):
            if event.status == "not_configured":
                self._write("验证：未检测到可用的测试命令", "dim")
            elif event.status == "skipped":
                pass
            elif event.ok:
                self._write(f"验证通过  {event.summary}", "green")
            else:
                self._write(f"验证失败  {event.summary}", "red")
                for issue in event.issues[:5]:
                    self._write(f"  · {issue}", "red")

        elif isinstance(event, ReviewFinished):
            if event.status == "skipped":
                pass
            elif event.blocked:
                self._write(f"代码审查未通过  {event.summary}", "red")
                for finding in event.findings[:5]:
                    self._write(f"  · {finding}", "red")
            elif event.status == "warned":
                self._write(f"代码审查有告警  {event.summary}", "yellow")
            else:
                self._write(f"代码审查通过  {event.summary}", "green")

        elif isinstance(event, RepairStarted):
            self._write(
                f"↻ 第 {event.attempt}/{event.limit} 次修复  {event.summary}", "yellow"
            )

        elif isinstance(event, ApprovalRequested):
            self._flush_stream()
            self._write(f"⏸ 需要确认（{event.level}）：{event.command}", "yellow")

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
        self._set_busy(True)
        try:
            await self._consume(self._runtime.run(prompt, thread_id=self._session_id))
        except Exception as exc:  # noqa: BLE001 - 任何异常都要显示出来而不是静默
            self._flush_stream()
            self._write(f"运行异常：{type(exc).__name__}: {exc}", "red")
        finally:
            self._set_busy(False)

    async def _consume(self, stream) -> None:
        """消费事件流；遇到审批挂起就弹窗收集答复后 resume，直到真正跑完。"""
        queue: list[ApprovalRequested] = []
        async for event in stream:
            self._apply(event)
            if isinstance(event, ApprovalRequested):
                queue.append(event)

        while queue:
            decisions: dict[str, bool] = {}
            for index, request in enumerate(queue, start=1):
                position = f"({index}/{len(queue)})" if len(queue) > 1 else ""
                decisions[request.request_id] = bool(
                    await self.push_screen_wait(ApprovalScreen(request, position))
                )
            queue = []
            async for event in self._runtime.resume(self._session_id, decisions):
                self._apply(event)
                if isinstance(event, ApprovalRequested):
                    queue.append(event)

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
        elif name == "/snapshots":
            self._show_snapshots(rest)
        elif name == "/diff":
            self._show_diff(rest)
        elif name == "/undo":
            self._undo(rest)
        elif name == "/sessions":
            self._show_sessions(rest)
        elif name == "/switch":
            self._switch_session(rest.strip())
        elif name == "/memory":
            self._show_memory()
        elif name == "/remember":
            self._remember(rest)
        elif name == "/forget":
            self._forget(rest)
        else:
            self._write(f"未知命令：{name}。输入 /help 查看可用命令。", "yellow")

    # ------------------------------------------------------------------
    # 会话与记忆
    # ------------------------------------------------------------------

    def _show_sessions(self, raw_limit: str) -> None:
        try:
            limit = max(int(raw_limit.strip() or 15), 1)
        except ValueError:
            self._write("用法：/sessions [条数]", "yellow")
            return

        found = self._runtime.sessions.list(limit=limit)
        if not found:
            self._write("没有历史会话记录。", "dim")
            return

        self._write(f"历史会话（最近 {len(found)} 个）", "dim")
        for info in found:
            marker = "▶" if info.thread_id == self._session_id else " "
            self._write(
                f" {marker} {info.thread_id}  {info.last_seen}  "
                f"{info.prompts} 次提问 / {info.tool_calls} 次工具  {info.title}",
                "cyan" if marker.strip() else "dim",
            )
        self._write("用 /switch <会话id> 切换过去。", "dim")

    def _switch_session(self, thread_id: str) -> None:
        if not thread_id:
            self._write("用法：/switch <会话id>（用 /sessions 查看）", "yellow")
            return
        if self._busy:
            self._write("（当前任务执行中，无法切换会话）", "yellow")
            return
        self._load_session(thread_id)

    @work(exclusive=True)
    async def _load_session(self, thread_id: str) -> None:
        self._session_id = thread_id
        self._plan = []
        self._step_idx = 0
        self._stream.clear()
        self._answering = False
        self._plan_panel.render_plan([], 0)
        self.query_one("#streaming", Static).update("")
        self._transcript.clear()

        self._render_session_info()

        try:
            history = await self._runtime.history(thread_id)
        except Exception as exc:  # noqa: BLE001 - 历史载入失败不该影响继续对话
            self._write(f"已切换到 {thread_id}（历史记录载入失败：{exc}）", "yellow")
            return

        if not history:
            self._write(f"已切换到会话 {thread_id}（暂无历史记录）", "dim")
            return

        self._write(f"已切换到会话 {thread_id}，{len(history)} 条历史记录：\n", "dim")
        for message in history:
            if message.role == "user":
                self._write(f"你 › {message.text}", "bold cyan")
            else:
                self._write(f"助手 › {message.text}", "green")
            self._write("")

    def _show_memory(self) -> None:
        facts = self._runtime.memories()
        if not facts:
            self._write("当前没有项目记忆。用 /remember <事实> 添加。", "dim")
            return
        self._write(f"项目记忆 {self._runtime.memory.path}（{len(facts)} 条）", "dim")
        for index, fact in enumerate(facts, start=1):
            self._write(f"  {index}. {fact}", "")

    def _remember(self, raw: str) -> None:
        text = raw.strip()
        if not text:
            self._write("用法：/remember <要记住的项目事实>", "yellow")
            return
        try:
            facts = self._runtime.remember(text)
        except Exception as exc:  # noqa: BLE001 - 上限/写盘失败要提示而不是崩
            self._write(str(exc), "red")
            return
        self._write(f"已记住（共 {len(facts)} 条）：{text}", "green")

    def _forget(self, raw: str) -> None:
        try:
            index = int(raw.strip())
        except ValueError:
            self._write("用法：/forget <序号>（用 /memory 查看序号）", "yellow")
            return
        try:
            facts = self._runtime.forget(index)
        except Exception as exc:  # noqa: BLE001 - 序号越界/写盘失败都要提示而不是崩
            self._write(str(exc), "red")
            return
        self._write(f"已删除第 {index} 条（剩余 {len(facts)} 条）", "green")

    # ------------------------------------------------------------------
    # 回滚相关命令
    # ------------------------------------------------------------------

    @staticmethod
    def _split_target(raw: str) -> tuple[str | None, str | None]:
        """把 /undo 的参数拆成 (snapshot_id, path)。"""
        target = raw.strip()
        if not target:
            return None, None
        if _SNAPSHOT_ID_RE.match(target):
            return target, None
        return None, target

    def _show_snapshots(self, raw_limit: str) -> None:
        try:
            limit = max(int(raw_limit.strip() or 15), 1)
        except ValueError:
            self._write("用法：/snapshots [条数]", "yellow")
            return

        entries = self._runtime.list_snapshots(limit=limit)
        if not entries:
            self._write("还没有任何快照。", "dim")
            return
        self._write(f"文件快照（最近 {len(entries)} 条）", "dim")
        for entry in entries:
            self._write(f"  {entry.snapshot_id}  {entry.path}", "dim")

    def _show_diff(self, raw: str) -> None:
        snapshot_id, path = self._split_target(raw)
        text = self._runtime.diff_snapshot(snapshot_id=snapshot_id, path=path)
        if not text:
            self._write("没有可显示的快照，或内容与快照一致。", "dim")
            return
        self._write_diff(text)

    def _undo(self, raw: str) -> None:
        snapshot_id, path = self._split_target(raw)
        result = self._runtime.restore(snapshot_id=snapshot_id, path=path)
        if not result.ok:
            self._write(result.message, "red")
            return
        self._write(result.message, "green")
        if result.undo_snapshot_id:
            self._write(f"回滚前的状态已留底：{result.undo_snapshot_id}", "dim")
        if result.diff:
            self._write_diff(result.diff)

    def _write_diff(self, raw: str) -> None:
        for line in raw.splitlines():
            if line.startswith(("+++", "---")):
                style = "bold"
            elif line.startswith("+"):
                style = "green"
            elif line.startswith("-"):
                style = "red"
            elif line.startswith("@@"):
                style = "cyan"
            else:
                style = ""
            self._write(line, style)

    def _show_audit(self, raw_limit: str) -> None:
        try:
            limit = max(int(raw_limit.strip() or 10), 1)
        except ValueError:
            self._write("用法：/audit [条数]", "yellow")
            return

        path = self._runtime.audit_path
        records = read_records_many(
            self._runtime.audit_files(), thread_id=self._session_id, limit=limit
        )
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
        self._answering = False
        self._plan_panel.render_plan([], 0)
        self.query_one("#streaming", Static).update("")
        self._transcript.clear()
        if self._runtime is not None:
            self._render_session_info()
        self._write(f"已开始新会话 {self._session_id}", "dim")

    def action_clear_log(self) -> None:
        self._transcript.clear()
