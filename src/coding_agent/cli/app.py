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
from coding_agent.llm.deepseek import MissingApiKeyError, build_llm
from coding_agent.llm.prompts import SYSTEM_PROMPT
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


@app.command()
def doctor() -> None:
    """检查运行环境：Python、依赖、API Key、WSL 沙箱。"""
    settings = get_settings()
    results: list[bool] = []

    console.print("[bold]环境自检[/]\n")

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
    tracing = "开启" if settings.langsmith_tracing else "关闭"
    results.append(
        _report("LangSmith 追踪", True, f"{tracing} · project={settings.langsmith_project}")
    )

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
    state: dict[str, Any] = {"plan": [], "total": 1}

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
            _emit(event.text)

        elif isinstance(event, ToolCallStarted):
            label = f"[{event.level}] " if event.level else ""
            console.print(
                f"\n[dim]→ {escape(label)}{escape(event.name)}: {escape(event.summary)}[/]"
            )

        elif isinstance(event, ToolCallFinished):
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

        elif isinstance(event, RunFailed):
            console.print(f"\n[red]运行失败：{escape(event.message)}[/]")

        elif isinstance(event, RunFinished):
            _emit("\n")

    return render


async def _run_events(
    settings: Settings, prompt: str, allow_write: bool, thread_id: str, workspace: str
) -> None:
    runtime = AgentRuntime(settings, workspace=workspace or None, allow_write=allow_write)
    kind = "SQLite 持久化" if runtime.persistent else "进程内"
    console.print(f"[dim]会话 {thread_id} · 工作区 {runtime.workspace} · checkpoint {kind}[/]\n")

    render = _make_renderer()
    try:
        async for event in runtime.run(prompt, thread_id=thread_id):
            render(event)
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
    write: bool = typer.Option(False, "--write", help="放开 L1 低风险写命令（mkdir/cp 等）"),
    thread_id: str = typer.Option("", "--thread-id", help="复用已有会话 id"),
) -> None:
    """走 LangGraph 图执行一轮任务（默认仅允许只读命令）。"""
    asyncio.run(
        _run_events(
            get_settings(), prompt, write, thread_id or uuid.uuid4().hex[:8], workspace
        )
    )


@app.command()
def audit(
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近多少条记录"),
    thread_id: str = typer.Option("", "--thread-id", help="只显示指定会话"),
    path: str = typer.Option("", "--path", help="审计文件路径，默认用配置推导"),
) -> None:
    """查看审计日志。"""
    settings = get_settings()
    today = f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
    target = Path(path) if path else settings.resolved_audit_dir / today

    records = read_records(target, thread_id=thread_id or None, limit=limit)
    if not records:
        console.print(f"[dim]没有审计记录：{target}[/]")
        return

    console.print(f"[dim]{target}（最近 {len(records)} 条）[/]\n")
    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("时间", "类型", "工具/对象", "级别", "判定", "结果"):
        table.add_column(column)

    for record in records:
        style = "red" if record.decision == "rejected" or record.kind == "run_error" else ""
        table.add_row(
            record.ts[11:19],
            record.kind,
            record.tool or record.path or "",
            record.level,
            record.decision,
            _audit_outcome(record),
            style=style,
        )
    console.print(table)


def _audit_outcome(record) -> str:
    if record.kind == "tool_call":
        if record.decision == "rejected":
            return "已拒绝"
        if record.ok is None:
            return ""
        if not record.ok:
            # 文件工具没有退出码，失败原因在工具返回文本里，这里只标失败
            return f"失败 exit={record.exit_code}" if record.exit_code is not None else "失败"
        return f"OK {record.duration_ms}ms" if record.duration_ms is not None else "OK"
    if record.kind == "file_change":
        return f"+{record.added} -{record.removed}"
    if record.kind == "plan":
        return f"{len(record.steps)} 步"
    if record.kind == "run_error":
        return escape(record.detail[:60])
    return ""


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
        console.print('[red]未安装 UI 依赖。请运行：pip install -e ".[ui]"[/]')
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
