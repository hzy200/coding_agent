"""终端 CLI：环境自检、纯对话、事件流渲染。

    agent doctor        环境自检（不需要 API Key）
    agent sandbox-init  创建沙箱工作区
    agent chat          纯流式对话，验证 API 联通
    agent run           走 AgentRuntime 执行任务

run 命令只消费 AgentRuntime 产出的领域事件，不直接接触图和工具。
TUI / Web 前端将复用同一套事件。
"""

from __future__ import annotations

import asyncio
import importlib.metadata as metadata
import shlex
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from coding_agent.audit import AuditLogger, read_records, read_records_many
from coding_agent.config import Settings, get_settings, shadowed_env_keys
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    Event,
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
from coding_agent.llm.deepseek import MissingApiKeyError, build_llm
from coding_agent.llm.prompts import SYSTEM_PROMPT
from coding_agent.memory.sessions import SessionIndex, format_table_rows
from coding_agent.messages import text_of
from coding_agent.runtime import AgentRuntime
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace


def _configure_stdio() -> None:
    """Windows 的标准流默认走 GBK：管道/重定向时中文会乱码，stdin 还会产生
    代理字符导致下游 JSON 序列化直接崩溃。统一按 UTF-8 处理。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_stdio()

app = typer.Typer(add_completion=False, no_args_is_help=True, help="终端原生编程智能体")
console = Console()

_DISTRIBUTIONS = (
    "langgraph",
    "langchain-core",
    "langchain-openai",
    "langsmith",
    "langgraph-checkpoint-sqlite",
)


# --------------------------------------------------------------------------
# 渲染辅助
# --------------------------------------------------------------------------

def _emit(text: str) -> None:
    # markup=False 很关键：模型输出里的 [..] 会被 rich 当成标记吞掉
    console.print(text, end="", markup=False, highlight=False, soft_wrap=True)


def _render_plan(steps: list[str], step_idx: int) -> None:
    console.print("[dim]计划：[/]")
    for i, step in enumerate(steps):
        if i < step_idx:
            style, box = "dim", "✓"
        elif i == step_idx:
            style, box = "cyan", "▶"
        else:
            style, box = "dim", "·"
        console.print(f"[{style}]  {box} {i + 1}. {escape(step)}[/]")


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def _report(label: str, passed: bool, detail: str = "") -> bool:
    tag = "[green]PASS[/]" if passed else "[red]FAIL[/]"
    suffix = f" — {escape(detail)}" if detail else ""
    console.print(f"{tag}  {label}{suffix}")
    return passed


def _warn(label: str, detail: str = "") -> None:
    """提示性输出：影响使用但不该让自检直接失败。"""
    suffix = f" — {escape(detail)}" if detail else ""
    console.print(f"[yellow]WARN[/]  {label}{suffix}")


def _check_langsmith(settings: Settings) -> list[bool]:
    """追踪要么真正可用，要么提前说清楚。

    凭据无效时 langsmith 会在**每次调用后**往 stderr 打一串 ingestion 失败 ——
    与其让用户在一堆噪音里猜，不如在这里一次性验掉。
    """
    if not settings.langsmith_tracing:
        return [_report("LangSmith 追踪", True, f"关闭 · project={settings.langsmith_project}")]

    if not settings.langsmith_api_key:
        return [_report("LangSmith 追踪", False, "已开启但缺 LANGSMITH_API_KEY")]

    try:
        from langsmith import Client

        client = Client()
        project = next(iter(client.list_projects(limit=1)), None)
    except Exception as exc:  # noqa: BLE001 - 凭据/网络问题都要给出可读提示
        return [
            _report(
                "LangSmith 追踪",
                False,
                f"凭据或网络不可用（{type(exc).__name__}）。"
                f"追踪会静默失败并往 stderr 打错误，建议先 LANGSMITH_TRACING=false",
            )
        ]

    detail = f"可用 · project={settings.langsmith_project}"
    if project is not None and getattr(project, "name", "") != settings.langsmith_project:
        detail += f"（该凭据下可见的第一个项目是 {project.name}）"
    return [_report("LangSmith 追踪", True, detail)]


@app.command()
def doctor() -> None:
    """检查运行环境：Python、依赖、API Key、WSL 沙箱。"""
    settings = get_settings()
    results: list[bool] = []

    console.print("[bold]环境自检[/]\n")

    shadowed = shadowed_env_keys()
    if shadowed:
        _warn(
            "环境变量被 .env 覆盖",
            f"{', '.join(shadowed)} 在环境变量里也有，但优先级是「.env > 环境变量」，"
            f"当前生效的是 .env 里那份。若你以为在生效的是环境变量，请改 .env",
        )
        console.print()

    v = sys.version_info
    results.append(_report("Python 版本", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}"))

    for dist in _DISTRIBUTIONS:
        try:
            version = metadata.version(dist)
            results.append(_report(f"依赖 {dist}", True, version))
        except metadata.PackageNotFoundError:
            results.append(_report(f"依赖 {dist}", False, "未安装，请 pip install -e ."))

    results.append(
        _report(
            "DEEPSEEK_API_KEY",
            bool(settings.deepseek_api_key),
            "已配置" if settings.deepseek_api_key else "未配置（agent chat/run 不可用）",
        )
    )
    results.extend(_check_langsmith(settings))

    distros = WslSandbox.list_distros()
    has_wsl = settings.wsl_distro in distros
    results.append(
        _report(
            f"WSL 发行版 {settings.wsl_distro}",
            has_wsl,
            f"已安装：{', '.join(distros) or '无'}" if distros else "WSL 不可用或未安装发行版",
        )
    )

    if has_wsl:
        sandbox = WslSandbox(settings)

        probe = sandbox.run("echo agent-sandbox-ok && uname -r")
        results.append(_report("沙箱执行探针", probe.ok, probe.stdout.strip().replace("\n", " | ")))

        user = sandbox.run("whoami").stdout.strip()
        results.append(
            _report("沙箱运行用户", user not in {"", "root"}, f"{user or '未知'}（不应为 root）")
        )

        backend = sandbox.run("command -v rg").ok
        results.append(
            _report(
                "检索后端",
                True,
                "ripgrep" if backend else "grep（未装 ripgrep，大仓库会慢一些）",
            )
        )

        workspace = resolve_workspace(settings, sandbox)
        exists = sandbox.run(f"test -d {shlex.quote(workspace)} && echo yes")
        found = "yes" in exists.stdout
        results.append(
            _report(
                f"工作区 {workspace}",
                found,
                "存在" if found else "不存在，运行 agent sandbox-init 创建",
            )
        )

        if settings.shell_sandbox == "bwrap":
            available = sandbox.bwrap_available()
            results.append(
                _report(
                    "shell 隔离 (bwrap)",
                    available,
                    "可用" if available else "已配置但不可用：agent run 会拒绝执行",
                )
            )
        else:
            results.append(_report("shell 隔离", True, "off（仅工作区 cwd 约束）"))

    console.print()
    if all(results):
        console.print("[green bold]全部通过[/]")
    else:
        console.print("[red bold]存在未通过项[/]")
        raise typer.Exit(code=1)


@app.command("sandbox-init")
def sandbox_init() -> None:
    """创建并校验沙箱工作区目录。"""
    settings = get_settings()
    sandbox = WslSandbox(settings)

    if not WslSandbox.available(settings.wsl_distro):
        console.print(f"[red]WSL 发行版 {settings.wsl_distro} 不可用[/]")
        raise typer.Exit(code=1)

    user = sandbox.run("whoami").stdout.strip()
    if user in {"", "root"}:
        console.print("[red]沙箱当前以 root 运行，隔离形同虚设。请改用普通用户。[/]")
        raise typer.Exit(code=1)

    workspace = resolve_workspace(settings, sandbox)
    quoted = shlex.quote(workspace)
    result = sandbox.run(f"mkdir -p {quoted} && realpath {quoted}")
    if not result.ok:
        console.print(f"[red]创建工作区失败[/]\n{escape(result.render(2000))}")
        raise typer.Exit(code=1)

    resolved = result.stdout.strip().splitlines()[-1]
    console.print(f"[green]工作区就绪[/] {escape(resolved)}（用户 {escape(user)}）")
    if not settings.wsl_workspace:
        console.print("[dim]当前按 $HOME 自动推导。如需固定，请写入 .env：[/]")
        console.print(f"[dim]AGENT_WSL_WORKSPACE={escape(resolved)}[/]")


# --------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------

async def _chat_loop(settings: Settings) -> None:
    llm = build_llm(settings, streaming=True)
    history: list[Any] = [SystemMessage(content=SYSTEM_PROMPT)]
    console.print("[dim]纯对话模式（无工具）。输入 exit 退出。[/]\n")

    while True:
        try:
            text = console.input("[bold cyan]你 › [/]")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if text.strip().lower() in {"exit", "quit", ":q"}:
            return
        if not text.strip():
            continue

        history.append(HumanMessage(content=text))
        console.print("[bold green]助手 › [/]", end="")
        buf: list[str] = []
        try:
            async for chunk in llm.astream(history):
                piece = text_of(chunk)
                if piece:
                    buf.append(piece)
                    _emit(piece)
        except KeyboardInterrupt:
            console.print("\n[dim]已中断[/]")
        _emit("\n\n")
        history.append(AIMessage(content="".join(buf)))


@app.command()
def chat() -> None:
    """纯流式对话，不走工具 —— 用于验证 API 联通。"""
    asyncio.run(_chat_loop(get_settings()))


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def _make_renderer() -> Callable[[Event], None]:
    """把事件映射为终端输出。

    这是「前端只消费事件」的一个具体实现；TUI / Web 会各写一份自己的映射表。
    """
    state: dict[str, Any] = {"plan": [], "total": 1, "answering": False}

    def render(event: Event) -> None:
        if isinstance(event, PlanCreated):
            state["plan"] = list(event.steps)
            state["total"] = max(len(event.steps), 1)
            if event.steps:
                _render_plan(event.steps, 0)
                console.print("\n[dim]开始执行…[/]")

        elif isinstance(event, StepStarted):
            # 单步计划下标题只是重复用户问题，省略
            if event.total > 1:
                head = f"第 {event.index + 1}/{event.total} 步"
                console.print(f"\n[cyan]{head}[/] {escape(event.text)}")
            console.print("[bold green]助手 › [/]", end="")

        elif isinstance(event, StepFinished):
            suffix = "[dim]（工具预算耗尽）[/]" if event.budget_exhausted else ""
            _emit(f"\n{suffix}\n")

        elif isinstance(event, AssistantToken):
            # respond 与 act 说的内容常有重叠（架构上就是「每步小结 + 最终汇总」）。
            # 给最终答复一个明确的起点，读者才不会以为是重复输出。
            if event.node == "respond" and not state["answering"]:
                state["answering"] = True
                console.print("\n[dim]── 最终答复 ──[/]")
            _emit(event.text)

        elif isinstance(event, ToolCallStarted):
            label = f"[{event.level}] " if event.level else ""
            console.print(
                f"\n[dim]→ {escape(label)}{escape(event.name)}: {escape(event.summary)}[/]"
            )

        elif isinstance(event, RepairStarted):
            console.print(
                f"[yellow]↻ 第 {event.attempt}/{event.limit} 次修复[/] "
                f"[dim]{escape(event.summary)}[/]"
            )

        elif isinstance(event, ApprovalRequested):
            pass  # 由 _run_events 负责提问，渲染层不重复输出

        elif isinstance(event, ToolCallFinished):
            if event.decision == "denied":
                console.print("[red]← 已被用户拒绝[/]")
                return
            if event.rejected:
                console.print("[red]← 已被安全策略拒绝[/]")
                return
            if event.ok:
                mark, style = "✓", "dim"
            elif event.exit_code is not None:
                mark, style = f"✗ exit={event.exit_code}", "yellow"
            else:
                mark, style = "✗", "yellow"
            elapsed = f" {event.duration_ms / 1000:.2f}s" if event.duration_ms else ""
            console.print(f"[{style}]← {mark}{elapsed}[/]")

        elif isinstance(event, FileChanged):
            stat = f"+{event.added} -{event.removed}"
            stamp = f" · {event.snapshot_id}" if event.snapshot_id else ""
            console.print(f"[magenta]✎ {escape(event.path)}[/] [dim]{stat}{stamp}[/]")

        elif isinstance(event, Verification):
            if event.status == "not_configured":
                console.print("[dim]验证：未检测到可用的测试命令[/]")
            elif event.status == "skipped":
                pass
            elif event.ok:
                console.print(f"[green]验证通过[/] [dim]{escape(event.summary)}[/]")
            else:
                console.print(f"[red]验证失败[/] [dim]{escape(event.summary)}[/]")
                for issue in event.issues[:5]:
                    console.print(f"  [red]·[/] {escape(issue)}")

        elif isinstance(event, RunFailed):
            console.print(f"\n[red]运行失败：{escape(event.message)}[/]")

        elif isinstance(event, RunFinished):
            _emit("\n")

    return render


def _ask_approval(requests: list[ApprovalRequested], *, assume_yes: bool) -> dict[str, bool]:
    """逐条询问审批结果。非交互环境下默认拒绝（fail closed）。"""
    decisions: dict[str, bool] = {}
    for request in requests:
        console.print()
        console.print(f"[yellow bold]需要确认[/] [dim]({escape(request.level)})[/]")
        console.print(f"  [bold]{escape(request.command)}[/]")
        if request.reason:
            console.print(f"  [dim]理由：{escape(request.reason)}[/]")

        if assume_yes:
            console.print("  [green]→ 已按 --yes 自动批准[/]")
            decisions[request.request_id] = True
            continue

        try:
            answer = console.input("  执行？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [red]→ 非交互环境，按拒绝处理[/]")
            decisions[request.request_id] = False
            continue
        decisions[request.request_id] = answer in {"y", "yes"}

    return decisions


async def _run_events(
    settings: Settings,
    prompt: str,
    allow_write: bool,
    thread_id: str,
    workspace: str,
    *,
    assume_yes: bool = False,
) -> None:
    runtime = AgentRuntime(settings, workspace=workspace or None, allow_write=allow_write)
    kind = "SQLite 持久化" if runtime.persistent else "进程内"
    console.print(f"[dim]会话 {thread_id} · 工作区 {runtime.workspace} · checkpoint {kind}[/]\n")

    render = _make_renderer()
    try:
        queue: list[ApprovalRequested] = []
        async for event in runtime.run(prompt, thread_id=thread_id):
            render(event)
            if isinstance(event, ApprovalRequested):
                queue.append(event)

        # 图每次挂起后都要重新续跑，可能连续挂起多次
        while queue:
            decisions = _ask_approval(queue, assume_yes=assume_yes)
            queue = []
            async for event in runtime.resume(thread_id, decisions):
                render(event)
                if isinstance(event, ApprovalRequested):
                    queue.append(event)
    except KeyboardInterrupt:
        console.print("\n[dim]已中断[/]")
    finally:
        await runtime.aclose()


@app.command()
def run(
    prompt: str = typer.Argument(..., help="交给智能体的自然语言指令"),
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="工作区，WSL 绝对路径（/mnt/d/proj）或 Windows 路径（D:\\\\proj）均可，默认用配置值",
    ),
    write: bool = typer.Option(False, "--write", help="放开 L1 低风险写命令与文件工具写入"),
    yes: bool = typer.Option(False, "--yes", "-y", help="对 L2/L3 命令一律自动批准（跳过确认）"),
    thread_id: str = typer.Option("", "--thread-id", help="复用已有会话 id"),
) -> None:
    """走 LangGraph 图执行一轮任务。

    默认只读；变更类命令（L2/L3）会先停下来请求你确认。
    """
    asyncio.run(
        _run_events(
            get_settings(),
            prompt,
            write,
            thread_id or uuid.uuid4().hex[:8],
            workspace,
            assume_yes=yes,
        )
    )


_WORKSPACE_OPTION = typer.Option(
    "",
    "--workspace",
    "-C",
    help="工作区，WSL 绝对路径（/mnt/d/proj）或 Windows 路径（D:\\\\proj）均可，默认用配置值",
)


@app.command()
def snapshots(
    workspace: str = _WORKSPACE_OPTION,
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近多少条"),
) -> None:
    """列出文件快照（每次写入/编辑前的留底）。"""
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)
    entries = runtime.list_snapshots(limit=limit)
    if not entries:
        console.print(f"[dim]还没有任何快照。工作区 {runtime.workspace}[/]")
        return

    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("快照 id", "文件"):
        table.add_column(column)
    for entry in entries:
        table.add_row(entry.snapshot_id, escape(entry.path))
    console.print(f"[dim]工作区 {runtime.workspace}（最近 {len(entries)} 条）[/]\n")
    console.print(table)


@app.command()
def undo(
    workspace: str = _WORKSPACE_OPTION,
    snapshot_id: str = typer.Option("", "--snapshot", help="指定快照 id，默认回滚最近一次改动"),
    path: str = typer.Option("", "--path", help="只回滚这个文件"),
) -> None:
    """把文件回滚到某次修改前的状态。"""
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)
    result = runtime.restore(snapshot_id=snapshot_id or None, path=path or None)

    if not result.ok:
        console.print(f"[red]{escape(result.message)}[/]")
        raise typer.Exit(code=1)

    console.print(f"[green]{escape(result.message)}[/]")
    if result.undo_snapshot_id:
        console.print(f"[dim]回滚前的状态已留底：{result.undo_snapshot_id}[/]")
    if result.diff:
        console.print()
        console.print(_diff_text(result.diff))


@app.command()
def diff(
    workspace: str = _WORKSPACE_OPTION,
    snapshot_id: str = typer.Option("", "--snapshot", help="指定快照 id，默认最近一次改动"),
    path: str = typer.Option("", "--path", help="只看这个文件"),
) -> None:
    """查看某次改动之后文件变成了什么样（快照 → 当前）。"""
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)
    text = runtime.diff_snapshot(snapshot_id=snapshot_id or None, path=path or None)
    if not text:
        console.print("[dim]没有可显示的快照，或内容与快照一致。[/]")
        return
    console.print(_diff_text(text))


def _diff_text(raw: str) -> Text:
    """给 diff 上色：加行绿、删行红、文件头加粗。"""
    styled = Text()
    for line in raw.splitlines():
        if line.startswith(("+++", "---")):
            styled.append(line + "\n", style="bold")
        elif line.startswith("+"):
            styled.append(line + "\n", style="green")
        elif line.startswith("-"):
            styled.append(line + "\n", style="red")
        elif line.startswith("@@"):
            styled.append(line + "\n", style="cyan")
        else:
            styled.append(line + "\n")
    return styled


@app.command()
def sessions(
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近多少个会话"),
) -> None:
    """列出历史会话（从审计日志归纳）。"""
    settings = get_settings()
    index = SessionIndex(settings.resolved_audit_dir)
    found = index.list(limit=limit)

    if not found:
        console.print(f"[dim]没有历史会话记录：{settings.resolved_audit_dir}[/]")
        return

    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("会话 id", "最近活跃", "提问", "工具调用", "内容"):
        table.add_column(column)
    for row in format_table_rows(found):
        table.add_row(row[0], row[1], row[2], row[3], escape(row[4]))
    console.print(table)
    console.print("\n[dim]用 agent run --thread-id <会话 id> \"…\" 继续该会话[/]")


@app.command()
def memory(
    add: str = typer.Option("", "--add", help="新增一条项目记忆"),
    forget: int = typer.Option(0, "--forget", help="删除第 N 条（序号从 1 开始）"),
    clear: bool = typer.Option(False, "--clear", help="清空全部记忆"),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """查看或维护跨会话的项目记忆。"""
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)

    try:
        if clear:
            runtime.memory.clear()
            console.print("[green]已清空项目记忆[/]")
        elif add:
            runtime.remember(add)
            console.print("[green]已记住[/]")
        elif forget:
            runtime.forget(forget)
            console.print(f"[green]已删除第 {forget} 条[/]")
    except Exception as exc:  # noqa: BLE001 - 记忆文件读写失败要给可读提示
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc

    facts = runtime.memories()
    if not facts:
        console.print(f"[dim]当前没有项目记忆。文件位置：{runtime.memory.path}[/]")
        return

    console.print(f"[dim]{runtime.memory.path}（{len(facts)} 条）[/]\n")
    for index, fact in enumerate(facts, start=1):
        console.print(f"  {index}. {escape(fact)}")


@app.command()
def audit(
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近多少条记录"),
    thread_id: str = typer.Option("", "--thread-id", help="只显示指定会话"),
    path: str = typer.Option("", "--path", help="审计文件路径，默认用配置推导"),
) -> None:
    """查看审计日志。"""
    settings = get_settings()

    if path:
        label = str(Path(path))
        records = read_records(Path(path), thread_id=thread_id or None, limit=limit)
    else:
        # 默认看当天全部片段（轮转后一天可能不止一个文件）
        logger = AuditLogger(settings.resolved_audit_dir)
        label = f"{settings.resolved_audit_dir}（{datetime.now(UTC):%Y-%m-%d}*.jsonl）"
        records = read_records_many(
            logger.files_today(), thread_id=thread_id or None, limit=limit
        )

    if not records:
        console.print(f"[dim]没有审计记录：{label}[/]")
        return

    console.print(f"[dim]{label}（最近 {len(records)} 条）[/]\n")
    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("时间", "类型", "工具/对象", "级别", "判定", "结果"):
        table.add_column(column)

    for record in records:
        failed_verify = record.kind == "verify" and record.ok is False
        bad = (
            record.decision in ("rejected", "denied")
            or record.kind == "run_error"
            or failed_verify
        )
        table.add_row(
            record.ts[11:19],
            record.kind,
            record.tool or record.path or "",
            record.level,
            record.decision,
            _audit_outcome(record),
            style="red" if bad else "",
        )
    console.print(table)


def _audit_outcome(record) -> str:
    if record.kind == "tool_call":
        if record.decision in ("rejected", "denied"):
            # denied 是人工拒绝，rejected 是策略/路径守卫拒绝，都没执行
            return "已拒绝"
        if record.ok is None:
            return ""
        if not record.ok:
            # 文件工具没有退出码，失败原因在工具返回文本里，这里只标失败
            return f"失败 exit={record.exit_code}" if record.exit_code is not None else "失败"
        mark = "已批准" if record.decision == "approved" else "OK"
        return f"{mark} {record.duration_ms}ms" if record.duration_ms is not None else mark
    if record.kind in ("file_change", "rollback"):
        return f"+{record.added} -{record.removed}"
    if record.kind == "plan":
        return f"{len(record.steps)} 步"
    if record.kind == "verify":
        mark = "通过" if record.ok else "失败"
        return f"{mark} {escape(record.detail[:48])}"
    if record.kind == "repair":
        return escape(record.detail[:60])
    if record.kind == "run_error":
        return escape(record.detail[:60])
    return ""


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8765, "--port", help="监听端口"),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """启动最小 Web 验证界面（流式对话 + 多会话 + API 联通测试）。"""
    try:
        import uvicorn
    except ImportError as exc:
        hint = escape('pip install -e ".[web]"')
        console.print(f"[red]未安装 Web 依赖。请运行：{hint}[/]")
        raise typer.Exit(code=2) from exc

    from coding_agent.web import create_app

    settings = get_settings()
    if workspace:
        settings = settings.model_copy(update={"wsl_workspace": workspace})
    console.print(f"[dim]打开 http://{host}:{port}（Ctrl+C 停止）[/]")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


@app.command()
def tui(
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="工作区，WSL 绝对路径（/mnt/d/proj）或 Windows 路径（D:\\\\proj）均可，默认用配置值",
    ),
    write: bool = typer.Option(False, "--write", help="放开 L1 低风险写命令（mkdir/cp 等）"),
) -> None:
    """启动终端界面（Textual）。"""
    try:
        from coding_agent.tui import AgentTuiApp
    except ImportError as exc:
        # 必须转义 [ui]：不转义的话 rich 会把它当样式标记吞掉，
        # 用户看到的是一条坏掉的安装命令
        hint = escape('pip install -e ".[ui]"')
        console.print(f"[red]未安装 UI 依赖。请运行：{hint}[/]")
        raise typer.Exit(code=2) from exc

    AgentTuiApp(get_settings(), workspace=workspace or None, allow_write=write).run()


def main() -> None:
    try:
        app()
    except MissingApiKeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc


if __name__ == "__main__":
    main()
