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
import ipaddress
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

from coding_agent.audit import (
    AuditError,
    AuditLogger,
    read_records_many,
    verify_audit_files,
)
from coding_agent.audit.logger import now_iso
from coding_agent.config import (
    PRODUCTION_PROFILES,
    Settings,
    apply_security_profile,
    get_settings,
    shadowed_env_keys,
)
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    CostReport,
    Event,
    FileChanged,
    OrchestrationFinished,
    PlanCreated,
    PlanRevised,
    RepairStarted,
    ReviewFinished,
    RunFailed,
    RunFinished,
    StepFinished,
    StepStarted,
    SubAgentFinished,
    SubAgentStarted,
    TaskGraphCreated,
    ToolCallFinished,
    ToolCallStarted,
    Verification,
    WorkerFinished,
    WorkerStarted,
    WorkflowSelected,
)
from coding_agent.llm.deepseek import MissingApiKeyError, build_llm
from coding_agent.llm.prompts import SYSTEM_PROMPT
from coding_agent.memory.sessions import SessionIndex, format_table_rows
from coding_agent.messages import text_of
from coding_agent.runtime import AgentRuntime, submit_steering_command
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.steering import SteeringCommand
from coding_agent.workspace_lock import WorkspaceLock


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


def _check_file_locks(settings: Settings) -> list[bool]:
    """探测审计目录是否支持 OS 文件锁。

    不支持时不阻断运行：审计/steering/journal 是协调数据，降级为线程互斥
    仍然可用；但必须让用户知道跨进程保护已经失效。
    """
    from coding_agent.filelock import FileLock

    path = settings.resolved_audit_dir / ".doctor.lock"
    try:
        lock = FileLock(path)
        with lock:
            degraded = lock.degraded
    except Exception as exc:  # noqa: BLE001 - 自检只报告，不抛栈
        return [
            _report(
                "跨进程文件锁",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        ]
    finally:
        try:
            path.unlink()
        except OSError:
            pass

    if degraded:
        _warn(
            "跨进程文件锁",
            "OS 锁不可用：审计/steering/journal 只有线程互斥（多进程保护降级）",
        )
        return []
    return [_report("跨进程文件锁", True, "OS 文件锁可用")]


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
    results.extend(_check_file_locks(settings))
    profile = str(settings.security_profile or "local").strip().lower()
    results.append(
        _report(
            "安全 profile",
            True,
            f"{profile} · network={'on' if settings.shell_network else 'off'} · "
            f"repo_instructions={settings.trust_repo_instructions} · "
            f"repo_memory={settings.trust_repo_memory}",
        )
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
        workspace = resolve_workspace(settings, sandbox)
        # bwrap 的 workdir 读 sandbox.settings；配置留空时写回推导值。
        sandbox.settings = settings.model_copy(
            update={"wsl_workspace": workspace}
        )

        healthy = sandbox.health(force=True)
        results.append(
            _report(
                "WSL 健康探针",
                healthy,
                f"circuit={sandbox.circuit_state()}",
            )
        )

        probe = sandbox.run(
            "echo agent-sandbox-ok && uname -r", cwd=workspace
        )
        results.append(_report("沙箱执行探针", probe.ok, probe.stdout.strip().replace("\n", " | ")))

        user = sandbox.run("whoami", cwd=workspace).stdout.strip()
        results.append(
            _report("沙箱运行用户", user not in {"", "root"}, f"{user or '未知'}（不应为 root）")
        )

        backend = sandbox.run("command -v rg", cwd=workspace).ok
        results.append(
            _report(
                "检索后端",
                True,
                "ripgrep" if backend else "grep（未装 ripgrep，大仓库会慢一些）",
            )
        )

        exists = sandbox.run(
            f"test -d {shlex.quote(workspace)} && echo yes",
            cwd=workspace,
        )
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
    llm = build_llm(settings, streaming=True, task_type="chat")
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
            if event.degraded:
                # 不能静默：多步任务退化成单步，用户会以为它真的做了规划
                console.print(
                    "[yellow]! 规划未解析，已退化为单步执行"
                    "（模型未返回可解析的步骤，请检查模型输出）[/]"
                )

        elif isinstance(event, PlanRevised):
            # 执行中重建了剩余计划：先把新计划摆出来，再继续往下走
            state["plan"] = list(event.steps)
            state["total"] = max(len(event.steps), 1)
            console.print("\n[yellow]↻ 计划已调整[/]")
            if event.steps:
                _render_plan(event.steps, event.step_idx)

        elif isinstance(event, StepStarted):
            # 单步计划下标题只是重复用户问题，省略
            if event.total > 1:
                head = f"第 {event.index + 1}/{event.total} 步"
                console.print(f"\n[cyan]{head}[/] {escape(event.text)}")
            console.print("[bold green]助手 › [/]", end="")

        elif isinstance(event, StepFinished):
            if event.budget_exhausted:
                suffix = "[dim]（工具预算耗尽）[/]"
            elif event.cancelled:
                # 被重规划放弃的步骤：不能说成「预算耗尽」——它根本没试过
                suffix = "[dim]（本步骤未执行完，已放弃）[/]"
            else:
                suffix = ""
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

        elif isinstance(event, SubAgentStarted):
            console.print(
                f"[cyan]◈ 探索子智能体开始[/] [dim]{escape(event.goal)}[/]"
            )

        elif isinstance(event, SubAgentFinished):
            style = "green" if event.status == "success" else "yellow"
            console.print(
                f"[{style}]◈ 探索子智能体 {escape(event.status)}[/] "
                f"[dim]{escape(event.summary)}[/]"
            )

        elif isinstance(event, WorkflowSelected):
            console.print(
                f"[cyan]◈ 工作流：{escape(event.workflow)}[/] "
                f"[dim]{escape(event.reason)}[/]"
            )

        elif isinstance(event, TaskGraphCreated):
            suffix = " [yellow]（已降级）[/]" if event.degraded else ""
            console.print(
                f"[cyan]◈ 任务图：{len(event.tasks)} 个只读任务[/]{suffix}"
            )
            for task in event.tasks:
                console.print(
                    f"  [dim]{escape(str(task.get('id', '')))} "
                    f"({escape(str(task.get('role', '')))}): "
                    f"{escape(str(task.get('goal', '')))}[/]"
                )

        elif isinstance(event, WorkerStarted):
            console.print(
                f"[dim]  → worker {escape(event.role)}/{escape(event.task_id)}: "
                f"{escape(event.goal)}[/]"
            )

        elif isinstance(event, WorkerFinished):
            style = (
                "green"
                if event.status == "success"
                else "yellow"
                if event.status == "partial"
                else "red"
            )
            console.print(
                f"[{style}]  ← worker {escape(event.role)}/{escape(event.task_id)} "
                f"{escape(event.status)}[/] [dim]{escape(event.summary)}[/]"
            )

        elif isinstance(event, OrchestrationFinished):
            console.print(
                f"[cyan]◈ 编排完成[/] [dim]成功 {event.completed}，"
                f"失败/阻塞 {event.failed}：{escape(event.summary)}[/]"
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
            if event.skipped:
                console.print("[dim]← 已跳过（环境未配置）[/]")
                return
            if event.interrupted:
                console.print("[yellow]← 结果未知（执行中断，未自动重放）[/]")
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

        elif isinstance(event, ReviewFinished):
            if event.status == "skipped":
                pass
            elif event.blocked:
                console.print(f"[red]代码审查未通过[/] [dim]{escape(event.summary)}[/]")
                for finding in event.findings[:5]:
                    console.print(f"  [red]·[/] {escape(finding)}")
            elif event.status == "warned":
                console.print(f"[yellow]代码审查有告警[/] [dim]{escape(event.summary)}[/]")
            else:
                console.print(f"[green]代码审查通过[/] [dim]{escape(event.summary)}[/]")

        elif isinstance(event, RunFailed):
            console.print(f"\n[red]运行失败：{escape(event.message)}[/]")

        elif isinstance(event, CostReport):
            line = f"成本 ${event.cost_usd:.4f}"
            if event.budget_usd > 0:
                line += f" / 预算 ${event.budget_usd:.2f}（{event.ratio:.0%}）"
            if event.billed_usd:
                line += f" / 账单 ${event.billed_usd:.4f}（差 ${event.billing_delta_usd:+.4f}）"
            console.print(f"[dim]{escape(line)}[/]")
            if event.billing_error:
                console.print(f"[yellow]账单对账失败：{escape(event.billing_error)}[/]")
            for alert in event.alerts:
                console.print(f"[yellow]⚠ {escape(alert)}[/]")

        elif isinstance(event, RunFinished):
            _emit("\n")

    return render


def _is_danger_level(level: str) -> bool:
    return str(level).strip().startswith("L3")


def _ask_approval(requests: list[ApprovalRequested], *, assume_yes: bool) -> dict[str, bool]:
    """逐条询问审批结果。非交互环境下默认拒绝（fail closed）。

    `--yes` 只自动批准 L2；L3（危险）始终要求人工确认。
    """
    decisions: dict[str, bool] = {}
    for request in requests:
        console.print()
        console.print(f"[yellow bold]需要确认[/] [dim]({escape(request.level)})[/]")
        console.print(f"  [bold]{escape(request.command)}[/]")
        if request.reason:
            console.print(f"  [dim]理由：{escape(request.reason)}[/]")

        if assume_yes and not _is_danger_level(request.level):
            console.print("  [green]→ 已按 --yes 自动批准[/]")
            decisions[request.request_id] = True
            continue
        if assume_yes:
            console.print("  [yellow]→ L3 危险命令始终需要确认，--yes 不自动批准[/]")

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
        help="工作区：/mnt/d/proj、wsl:/home/u/proj 或 D:\\\\proj；Git Bash 建议用 wsl: 前缀",
    ),
    write: bool = typer.Option(False, "--write", help="放开 L1 低风险写命令与文件工具写入"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="自动批准 L2 命令；L3 危险命令始终需要人工确认",
    ),
    thread_id: str = typer.Option("", "--thread-id", help="复用已有会话 id"),
) -> None:
    """走 LangGraph 图执行一轮任务。

    默认只读；变更类命令（L2/L3）会先停下来请求你确认。
    `--yes` 只跳过 L2，L3 危险命令始终逐条确认。
    """
    settings = apply_security_profile(get_settings())
    if (
        str(settings.security_profile or "").strip().lower()
        in PRODUCTION_PROFILES
        and (write or yes)
    ):
        console.print(
            "[yellow]production/ops profile 是只读审计档："
            "--write 与 --yes 不会生效，L2/L3 一律拒绝。[/]"
        )
    asyncio.run(
        _run_events(
            settings,
            prompt,
            write,
            thread_id or uuid.uuid4().hex[:8],
            workspace,
            assume_yes=yes,
        )
    )


@app.command("steer")
def steer(
    thread_id: str = typer.Option(..., "--thread-id", help="正在运行的会话 id"),
    action: str = typer.Argument(..., help="pause / cancel / add / replan"),
    text: str = typer.Option("", "--text", help="add/replan 的说明文本"),
    role: str = typer.Option("explore", "--role", help="add 的角色：explore / test / review"),
    steering_dir: str = typer.Option(
        "",
        "--steering-dir",
        help=(
            "与运行中 agent 一致的队列目录；"
            "留空用 AGENT_STEERING_DIR 或 <当前目录>/.agent/steering"
        ),
    ),
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="写入审计记录的工作区（应与运行中会话一致；不影响队列目录）",
    ),
) -> None:
    """向正在运行的会话注入一条实时干预指令。

    指令写入 `--steering-dir`（默认 `.agent/steering/`）并**先落审计**，
    运行中的进程会在下一个节点边界消费。队列目录必须与运行中的 agent
    一致：两者从不同目录启动时，请显式传 `--steering-dir` 或设置
    `AGENT_STEERING_DIR`。跨进程 resume 仍请用 TUI 或 Web。
    """
    try:
        command = SteeringCommand(
            action=action,
            text=text,
            role=role,
            source="cli",
            ts=now_iso(),
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    settings = apply_security_profile(get_settings())
    if steering_dir:
        settings = settings.model_copy(update={"steering_dir": steering_dir})
    try:
        submit_steering_command(
            settings,
            thread_id,
            command,
            workspace=workspace or None,
        )
    except (AuditError, OSError) as exc:
        console.print(f"[red]提交 steering 失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]已提交 steering[/green] {action} → {thread_id}；"
        f"队列目录 {settings.resolved_steering_dir}，"
        "运行中的 agent 会在下一个边界消费。"
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
    last_run: bool = typer.Option(
        False, "--last-run", help="回滚最近一次运行产生的全部文件改动"
    ),
    since: str = typer.Option(
        "", "--since", help="回滚该 ISO 时间（审计里的 run_start）之后的全部改动"
    ),
    thread_id: str = typer.Option(
        "", "--thread-id", help="配合 --last-run 定位会话"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="只列出将回滚的文件，不写盘"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
) -> None:
    """把文件回滚到某次修改前的状态。"""
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)

    if last_run or since:
        if since:
            prefix = runtime._snapshot_prefix(since)
            if not prefix:
                console.print(f"[red]无法解析时间：{escape(since)}[/]")
                raise typer.Exit(code=1)
        else:
            started = runtime.last_run_started_at(thread_id or None)
            if not started:
                console.print("[red]找不到最近一次运行记录。[/]")
                raise typer.Exit(code=1)
            prefix = runtime._snapshot_prefix(started)

        planned = runtime.restore_since(prefix, dry_run=True)
        if not planned:
            console.print("[dim]该时间之后没有产生任何快照。[/]")
            return

        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("快照 id")
        table.add_column("文件")
        for item in planned:
            table.add_row(item.snapshot_id, escape(item.path))
        console.print(
            f"[dim]将按新→旧回滚 {len(planned)} 处改动：[/]\n"
        )
        console.print(table)
        console.print(
            "[dim]按时间过滤；同一工作区若有并发运行，可能包含其改动。"
            "回滚前每个文件都会先留底，仍可再次 undo。[/]"
        )
        if dry_run:
            return
        if not yes and not typer.confirm("确认回滚？", default=False):
            raise typer.Exit(code=1)

        results = runtime.restore_since(prefix)
        failed = [item for item in results if not item.ok]
        for item in results:
            color = "green" if item.ok else "red"
            console.print(f"[{color}]{escape(item.message)}[/]")
        if failed:
            raise typer.Exit(code=1)
        return

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
def checkpoints(
    thread_id: str = typer.Option(..., "--thread-id", "-t", help="会话 id"),
    limit: int = typer.Option(20, "--limit", "-n", help="最多列出多少个"),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """列出某个会话的 checkpoint（新的在前），供 fork/rewind 选落点。"""

    async def _go() -> list[dict[str, Any]]:
        runtime = AgentRuntime(get_settings(), workspace=workspace or None)
        try:
            return await runtime.checkpoints(thread_id, limit=limit)
        finally:
            await runtime.aclose()

    try:
        items = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001 - 会话/checkpoint 不存在要给可读提示
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if not items:
        console.print("[dim]没有 checkpoint。[/]")
        return
    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("checkpoint id", "step", "内容"):
        table.add_column(column)
    for item in items:
        table.add_row(
            item["checkpoint_id"],
            str(item["step"]),
            escape(item["preview"]),
        )
    console.print(table)


@app.command()
def fork(
    thread_id: str = typer.Option(..., "--thread-id", "-t", help="源会话 id"),
    checkpoint_id: str = typer.Option(
        "", "--checkpoint", "-c", help="从指定 checkpoint 分叉；省略用当前状态"
    ),
    new_thread_id: str = typer.Option(
        "", "--new-thread-id", help="新会话 id；省略自动生成"
    ),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """从当前或指定 checkpoint 分叉出一个新会话。"""

    async def _go() -> str:
        runtime = AgentRuntime(get_settings(), workspace=workspace or None)
        try:
            return await runtime.fork(
                thread_id,
                checkpoint_id=checkpoint_id,
                new_thread_id=new_thread_id,
            )
        finally:
            await runtime.aclose()

    try:
        target = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]已分叉到 {escape(target)}[/]")
    console.print(f'[dim]继续该会话：agent run --thread-id {escape(target)} "…"[/]')


@app.command()
def rewind(
    thread_id: str = typer.Option(..., "--thread-id", "-t", help="会话 id"),
    steps: int = typer.Option(1, "--steps", "-n", help="回退多少个 checkpoint"),
    checkpoint_id: str = typer.Option(
        "", "--checkpoint", "-c", help="回退到指定 checkpoint；优先于 --steps"
    ),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """把会话回退到更早的 checkpoint（历史保留，可再次找回）。"""

    async def _go() -> str:
        runtime = AgentRuntime(get_settings(), workspace=workspace or None)
        try:
            return await runtime.rewind(
                thread_id, checkpoint_id=checkpoint_id, steps=steps
            )
        finally:
            await runtime.aclose()

    try:
        new_checkpoint = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]已回退，新 checkpoint：{escape(new_checkpoint)}[/]")


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
            replaced = getattr(runtime.memory, "last_replaced", [])
            suffix = (
                f"（已替换旧版本：{'；'.join(replaced)}）" if replaced else ""
            )
            console.print(f"[green]已记住{suffix}[/]")
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
    verify: bool = typer.Option(False, "--verify", help="验证审计哈希链"),
) -> None:
    """查看或验证审计日志。"""
    settings = get_settings()

    if path:
        paths = [Path(path)]
        label = str(Path(path))
    else:
        # 默认看当天全部片段（轮转后一天可能不止一个文件）
        logger = AuditLogger(settings.resolved_audit_dir)
        paths = logger.files_today()
        label = f"{settings.resolved_audit_dir}（{datetime.now(UTC):%Y-%m-%d}*.jsonl）"

    if verify:
        ok, message = verify_audit_files(paths)
        style = "green" if ok else "red"
        console.print(f"[{style}]审计哈希链：{escape(message)}[/]")
        if not ok:
            raise typer.Exit(code=1)
        return

    records = read_records_many(
        paths, thread_id=thread_id or None, limit=limit
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


def _is_loopback_bind(host: str) -> bool:
    text = str(host or "").strip().lower()
    if text in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8765, "--port", help="监听端口"),
    workspace: str = _WORKSPACE_OPTION,
    security_profile: str = typer.Option(
        "untrusted",
        "--security-profile",
        help="local | untrusted（Web 默认 untrusted：bwrap + 禁网 + ask）",
    ),
    ssl_keyfile: str = typer.Option("", "--ssl-keyfile", help="TLS 私钥路径"),
    ssl_certfile: str = typer.Option("", "--ssl-certfile", help="TLS 证书路径"),
    allow_remote: bool = typer.Option(
        False, "--allow-remote", help="允许非 loopback 监听（必须配置 token）"
    ),
    behind_proxy: bool = typer.Option(
        False, "--behind-proxy", help="TLS/认证由可信反向代理终止"
    ),
    allow_insecure_remote: bool = typer.Option(
        False,
        "--allow-insecure-remote",
        help="显式允许无 TLS 的远程监听（不推荐）",
    ),
) -> None:
    """启动 Web 界面。

    默认只监听 loopback 且使用不可信仓库 profile。公网部署必须配置
    `AGENT_WEB_AUTH_TOKENS`，并通过 TLS（本进程或反向代理）。
    """
    try:
        import uvicorn
    except ImportError as exc:
        hint = escape('pip install -e ".[web]"')
        console.print(f"[red]未安装 Web 依赖。请运行：{hint}[/]")
        raise typer.Exit(code=2) from exc

    from coding_agent.web import create_app
    from coding_agent.web.auth import AuthConfig

    settings = get_settings()
    if workspace:
        settings = settings.model_copy(update={"wsl_workspace": workspace})
    settings = apply_security_profile(
        settings.model_copy(update={"security_profile": security_profile})
    )

    try:
        auth = AuthConfig.from_settings(settings)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=2) from exc

    if behind_proxy and not auth.enabled:
        console.print(
            "[red]--behind-proxy 必须配置 AGENT_WEB_AUTH_TOKENS。[/] "
            "否则反向代理转发来的请求会被当成 127.0.0.1 匿名访问。"
        )
        raise typer.Exit(code=2)

    loopback = _is_loopback_bind(host)
    if not loopback:
        if not allow_remote:
            console.print(
                "[red]拒绝监听非 loopback 地址。[/] "
                "公网部署请显式加 --allow-remote，并配置 AGENT_WEB_AUTH_TOKENS。"
            )
            raise typer.Exit(code=2)
        if not auth.enabled:
            console.print(
                "[red]远程监听必须配置 AGENT_WEB_AUTH_TOKENS"
                "（或 AGENT_WEB_AUTH_TOKENS_FILE）。[/]"
            )
            raise typer.Exit(code=2)
        has_tls = bool(ssl_keyfile and ssl_certfile)
        if not has_tls and not behind_proxy and not allow_insecure_remote:
            console.print(
                "[red]远程监听需要 TLS：[/] 提供 --ssl-keyfile/--ssl-certfile，"
                "或声明 --behind-proxy，或显式 --allow-insecure-remote。"
            )
            raise typer.Exit(code=2)

    if bool(ssl_keyfile) != bool(ssl_certfile):
        console.print("[red]--ssl-keyfile 与 --ssl-certfile 必须同时提供。[/]")
        raise typer.Exit(code=2)

    data_root = (
        Path(settings.web_data_root)
        if settings.web_data_root
        else Path.cwd() / ".agent" / "web" / "users"
    )
    lock = WorkspaceLock(data_root, f"web-server:{data_root.resolve()}")
    try:
        acquired = lock.acquire()
    except OSError as exc:
        console.print(f"[red]无法创建 Web 单进程锁：{escape(str(exc))}[/]")
        raise typer.Exit(code=2) from exc
    if not acquired:
        console.print(
            "[red]已有一个 Web 进程在使用同一数据目录。[/] "
            "当前 Web 按单进程共享资源池运行；请先停止另一个进程，"
            "或通过 AGENT_WEB_DATA_ROOT 指定不同目录。"
        )
        raise typer.Exit(code=2)

    mode = "Bearer Token" if auth.enabled else "loopback 匿名"
    console.print(
        f"[dim]打开 http://{host}:{port}（profile={security_profile}，"
        f"认证={mode}，Ctrl+C 停止）[/]"
    )
    try:
        uvicorn.run(
            create_app(
                settings,
                auth,
                hsts=bool(ssl_keyfile or behind_proxy),
            ),
            host=host,
            port=port,
            log_level="warning",
            ssl_keyfile=ssl_keyfile or None,
            ssl_certfile=ssl_certfile or None,
            proxy_headers=behind_proxy,
        )
    finally:
        lock.release()


@app.command()
def tui(
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="工作区：/mnt/d/proj、wsl:/home/u/proj 或 D:\\\\proj；Git Bash 建议用 wsl: 前缀",
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
