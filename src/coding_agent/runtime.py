"""AgentRuntime —— 唯一编排入口。

所有前端（CLI / TUI / Web）都只跟这一层打交道：
它持有图、沙箱与安全策略，把 LangGraph 的原始流翻译成语义化的领域事件。
前端拿不到工具，也就无法绕过命令分级审批。

    async for event in AgentRuntime(settings).run("列出文件", thread_id="x"):
        match event.type: ...
"""

from __future__ import annotations

import posixpath
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from coding_agent.agents.policy import (
    RepoProfile,
    delegation_policy,
    profile_workspace,
)
from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.audit import models as audit_models
from coding_agent.audit.logger import now_iso, sanitize_args, truncate
from coding_agent.config import Settings, get_settings
from coding_agent.diffing import unified_diff
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    Event,
    FileChanged,
    OrchestrationFinished,
    PlanCreated,
    PlanRevised,
    RepairStarted,
    ReviewFinished,
    RunFailed,
    RunFinished,
    RunStarted,
    SteeringApplied,
    StepFinished,
    StepStarted,
    SubAgentFinished,
    SubAgentStarted,
    TaskGraphCreated,
    TaskGraphExtended,
    ToolCallFinished,
    ToolCallStarted,
    Verification,
    WorkerFinished,
    WorkerStarted,
    WorkflowRevised,
    WorkflowSelected,
)
from coding_agent.graph.build import build_graph, estimate_recursion_limit
from coding_agent.graph.nodes.approve import tool_level
from coding_agent.memory.checkpointer import CheckpointStore
from coding_agent.memory.longterm import LongTermMemory
from coding_agent.memory.sessions import SessionIndex
from coding_agent.messages import text_of
from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.pathguard import normalize_root
from coding_agent.sandbox.policy import APPROVED, DENIED, SessionPolicy, classify
from coding_agent.sandbox.snapshots import (
    ACTION_RESTORE,
    RestoreResult,
    SnapshotEntry,
    SnapshotStore,
)
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.steering import SteeringCommand, SteeringController
from coding_agent.tools.artifacts import (
    CallArtifact,
    FileArtifact,
    ShellArtifact,
    SubAgentArtifact,
    parse_artifact,
)
from coding_agent.tools.delegate import DELEGATE_EXPLORE
from coding_agent.tools.shell import SHELL_TOOL_NAME

# 只有这几个 action 才真正改动了文件，read 与空回滚不发 FileChanged
MUTATING_FILE_ACTIONS = frozenset({"create", "overwrite", "edit", ACTION_RESTORE})

# 只有这两个节点的模型输出是给用户看的文本；planner 的结构化调用不流式渲染
STREAMING_NODES = frozenset({"act", "respond"})

_PREVIEW_CHARS = 240
_FAILURE_MARKERS = ("工具执行异常：", "错误：")
_SUMMARY_KEYS = ("command", "path", "pattern", "query")
# planner 没解析出步骤、退化成单步时记进审计的 detail，供事后复盘
_PLAN_DEGRADED_DETAIL = "规划未解析，已退化为单步执行"
# 验证是否「放行」的唯一口径。与路由判定同源（graph/routing.route_after_verify
# 只在 failed 时拦下任务）：skipped / not_configured 表示**没验证**，不是**验证
# 失败**。曾经事件层按前者记、审计层按后者记，于是同一次 skipped 在两边结论相反，
# 事后对账对不上 —— 审计本该是可信来源，所以两处都必须走这个函数。
_VERIFICATION_BLOCKING_STATUS = "failed"


def _verification_passed(raw: dict[str, Any]) -> bool:
    return str(raw.get("status", "skipped")) != _VERIFICATION_BLOCKING_STATUS


def _verification_verdict(raw: dict[str, Any]) -> bool | None:
    """**审计**里的验证结论：True 通过 / False 失败 / **None 没验证**。

    与 `_verification_passed`（路由口径）刻意分开：路由只需要"要不要拦"，所以
    `not_configured` / `skipped` 都算"不拦"；但审计要回答的是"这次到底验证了没有"，
    把"没做"记成 `ok=True` 会让**验证有缺口在账上看不出来** —— 而审计本该是可信
    来源，这正是本项目最不能接受的失败模式之一（IMPROVEMENT_PLAN 的 P0-2）。
    """
    status = str(raw.get("status", "skipped"))
    if status == _VERIFICATION_BLOCKING_STATUS:
        return False
    if status == "ok":
        return True
    return None  # not_configured / skipped：没做，不是通过


# 审查是否「拦下了这一步」的唯一口径。与路由同源：只有 blocked 才改控制流，
# warned / clean / skipped 都只是记录。审查没能执行（status=warned 且带
# review-unavailable）**不算阻断** —— 工具坏了不该把用户的任务卡死。
_REVIEW_BLOCKING_STATUS = "blocked"


def _review_blocked(raw: dict[str, Any]) -> bool:
    return str(raw.get("status", "skipped")) == _REVIEW_BLOCKING_STATUS


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    """还原历史会话时用的一轮对话。"""

    role: str  # user | assistant
    text: str


def _last_message(update: dict[str, Any]) -> Any:
    messages = update.get("messages") or []
    return messages[-1] if messages else None


def _worker_key(result: dict[str, Any]) -> str:
    """去重键：同一任务的重试是不同的 worker 运行。"""
    return (
        f"{result.get('worker_id', '')}:"
        f"{int(result.get('attempt', 1) or 1)}"
    )


def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _summarize_args(args: dict[str, Any]) -> str:
    for key in _SUMMARY_KEYS:
        if args.get(key):
            return str(args[key])
    return ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])


def _add_usage(message: Any, input_tokens: int, output_tokens: int) -> tuple[int, int]:
    """把模型回报的用量累加进来（provider 没给就是 0）。

    用 provider 的 `usage_metadata` 而不是 tokenizer：无需额外依赖，
    口径也就是实际计费口径。
    """
    meta = getattr(message, "usage_metadata", None) or {}
    return (
        input_tokens + int(meta.get("input_tokens") or 0),
        output_tokens + int(meta.get("output_tokens") or 0),
    )


class AgentRuntime:
    """跑一轮任务，产出事件流。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        workspace: str | None = None,
        allow_write: bool = False,
        checkpointer: Any = None,
        audit: AuditLogger | None = None,
        approval_mode: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.allow_write = allow_write
        self.policy = SessionPolicy(
            allow_write=allow_write,
            approval_mode=approval_mode or self.settings.approval_mode,
        )
        self._sandbox = WslSandbox(self.settings)
        self._workspace = normalize_root(workspace) if workspace else None
        self._graph: Any = None
        # Phase 3D：用户干预队列。图节点与 runtime 共用同一实例；
        # 不放进 state —— 它是外部输入，且 worker 需要在运行中非破坏性轮询。
        self.steering = SteeringController()
        # 仓库画像每个 runtime 只探一次；工作区在本实例生命周期内固定。
        self._repo_profile_cache: RepoProfile | None = None
        self._snapshots: SnapshotStore | None = None
        self._memory: LongTermMemory | None = None
        self._fs: SandboxFs | None = None
        # thread_id -> {call_id: ToolCallStarted}。
        # 必须活在实例上而不是 `_stream` 局部：需要审批的调用在挂起时结束一次
        # `_stream`，resume 时才在另一次 `_stream` 里执行。若只存局部变量，
        # 审批恢复后的 tool_call 审计会丢掉命令参数（最高风险的那批调用）。
        self._pending_calls: dict[str, dict[str, ToolCallStarted]] = {}
        # thread_id -> {plan, step_idx, last_verification}。
        # 同理：挂起与恢复是两次 `_stream`，这些"用于翻译事件"的状态若只存局部，
        # 恢复后的 StepStarted 会退化成空文案、repair 事件会丢 summary。
        self._progress: dict[str, dict[str, Any]] = {}
        # thread_id -> 已 resume 次数（fresh run 归零），用于封顶挂起-恢复循环
        self._resume_counts: dict[str, int] = {}

        # 显式传入的 checkpointer 由调用方负责生命周期；否则按配置自行管理
        self._external_checkpointer = checkpointer
        self._store = (
            None
            if checkpointer is not None
            else CheckpointStore(self.settings.resolved_checkpoint_path)
        )
        self.audit = audit or AuditLogger(
            self.settings.resolved_audit_dir,
            enabled=self.settings.audit_enabled,
            max_bytes=self.settings.audit_max_mb * 1024 * 1024,
        )

    @property
    def workspace(self) -> str:
        """沙箱内的工作区绝对路径，按需解析并缓存。"""
        if self._workspace is None:
            self._workspace = resolve_workspace(self.settings, self._sandbox)
        return self._workspace

    def _repo_profile(self) -> RepoProfile:
        """仓库画像（文件数/测试文件数），按 runtime 缓存。"""
        if self._repo_profile_cache is None:
            self._repo_profile_cache = profile_workspace(self._sandbox, self.workspace)
        return self._repo_profile_cache

    @property
    def audit_path(self) -> Path:
        return self.audit.path

    def audit_files(self) -> list[Path]:
        """当天审计文件的全部片段（含轮转），供 `/audit` 等读取。"""
        return self.audit.files_today()

    def _trace_metadata(self, thread_id: str) -> dict[str, Any]:
        """挂在 trace 上的运行上下文。

        只放标识与开关，**不放提示词或文件内容** —— 追踪数据会离开本机。
        """
        return {
            "thread_id": thread_id,
            "workspace": self.workspace,
            "model": self.settings.deepseek_model,
            "allow_write": self.allow_write,
            "approval_mode": self.policy.approval_mode,
            "verify": self.settings.verify_command or "auto",
            "persistent": self.persistent,
            "orchestrator": self.settings.orchestrator_enabled,
        }

    @property
    def snapshots(self) -> SnapshotStore:
        """文件快照存储，供 `/undo` `/diff` 使用。"""
        if self._snapshots is None:
            self._snapshots = SnapshotStore(
                self._sandbox, self._sandbox_fs(), self.workspace
            )
        return self._snapshots

    @property
    def memory(self) -> LongTermMemory:
        """跨会话的项目记忆。"""
        if self._memory is None:
            self._memory = LongTermMemory(self._sandbox_fs(), self.workspace)
        return self._memory

    @property
    def sessions(self) -> SessionIndex:
        """历史会话列表（从审计日志归纳）。"""
        return SessionIndex(self.settings.resolved_audit_dir)

    def remember(self, text: str) -> list[str]:
        return self.memory.add(text)

    def forget(self, index: int) -> list[str]:
        return self.memory.remove(index)

    def memories(self) -> list[str]:
        return self.memory.load()

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        """从 checkpoint 还原某个会话的对话（供 `/switch` 展示）。

        只取人和助手的自然语言往来；工具调用与工具结果不展示 ——
        它们是过程噪声，重新渲染只会淹没真正的对话。

        刻意**直读 checkpoint 而不建图**：回放历史不该要求 API Key 或可用的沙箱
        （`_ensure_graph` 会拉起 `build_llm` 与 WSL 探测）。
        耦合点：直接取 `channel_values["messages"]`，与 `AgentState.messages` 对应。
        """
        saver = await self._checkpointer()
        fetch = getattr(saver, "aget_tuple", None)
        if fetch is None:
            return []
        tuple_ = await fetch({"configurable": {"thread_id": thread_id}})
        if tuple_ is None:
            return []
        checkpoint = getattr(tuple_, "checkpoint", None) or {}
        messages = (checkpoint.get("channel_values") or {}).get("messages") or []

        history: list[HistoryMessage] = []
        for message in messages:
            if isinstance(message, HumanMessage):
                text = text_of(message)
                if text.strip():
                    history.append(HistoryMessage("user", text))
            elif isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
                text = text_of(message)
                if text.strip():
                    history.append(HistoryMessage("assistant", text))
        return history

    @property
    def persistent(self) -> bool:
        """会话是否跨进程可恢复。"""
        return self._store is not None and self._store.persistent

    async def aclose(self) -> None:
        """释放 sqlite 连接。显式传入 checkpointer 时由调用方负责。

        前端可以不调用：每次写入都已 commit，进程退出不会丢已落盘的 checkpoint。
        """
        if self._store is not None:
            await self._store.aclose()
            self._graph = None

    def _forget_thread(self, thread_id: str) -> None:
        """运行收尾后清掉按 thread_id 累积的状态。

        `_pending_calls` / `_progress` 只为「挂起与恢复是两次 `_stream`」而存在
        （见 `__init__` 注释），一旦这轮以 RunFinished / RunFailed 收场就再无用处。
        长会话（TUI 一个进程跑几十轮）里不清会一直累积，而且 `_pending_calls`
        留着还会让下一轮误以为有旧的待配对调用。

        `_resume_counts` **故意不清**：它封顶的是「同一 thread 反复挂起-恢复」，
        跑完一轮就清零等于把护栏拆了（前端跑完再 resume 就能无限续）。它每
        thread 只占一个 int，本来也不是内存问题的所在。
        """
        self._pending_calls.pop(thread_id, None)
        self._progress.pop(thread_id, None)
        # 一轮结束：未消费的迟到指令与暂停标记都作废。
        self.steering.clear(thread_id)

    @property
    def effective_settings(self) -> Settings:
        """把解析后的工作区写回配置。

        工具的路径守卫是从 settings 里读工作区的，若这里不写回，
        `--workspace` 覆盖只会影响图状态里的 cwd，工具仍按旧根目录判边界，
        结果是所有命令都被判「工作目录非法」。
        """
        return self.settings.model_copy(update={"wsl_workspace": self.workspace})

    async def _checkpointer(self) -> Any:
        """本次运行用的 checkpoint 后端：外部传入的优先，否则自管 sqlite。"""
        if self._external_checkpointer is not None:
            return self._external_checkpointer
        return await self._store.aopen()

    async def _ensure_graph(self) -> Any:
        """编译后的图，首次使用时才构建。

        构造 AgentRuntime 不应触发建图 —— 否则只想读 workspace 或渲染 UI 的
        前端也会被迫要求 API Key、并做一次无谓的沙箱探测。
        """
        if self._graph is None:
            self._graph = build_graph(
                self.effective_settings,
                checkpointer=await self._checkpointer(),
                allow_write=self.allow_write,
                policy=self.policy,
                # 与 runtime 共用一份沙箱：探测缓存挂在实例上，各建一份就各探一遍
                sandbox=self._sandbox,
                steering=self.steering,
            )
        return self._graph

    # ------------------------------------------------------------------
    # 事件流
    # ------------------------------------------------------------------

    async def run(self, prompt: str, *, thread_id: str) -> AsyncIterator[Event]:
        """跑一轮任务。

        事件流以 `RunFinished` / `RunFailed` 收尾；若以 `ApprovalRequested` 收尾，
        说明图已挂起等待人工确认，前端应收集答复后调用 `resume()`。
        """
        # 读记忆（工作区 `.agent/memory.md`）需要沙箱；失败也必须以事件收尾，
        # 否则异常会在生成器第一步裸抛给前端（CLI 会以 traceback 收场）。
        try:
            memories = self.memories()
        except Exception as exc:  # noqa: BLE001 - 启动即失败同样是编排层错误
            detail = f"{type(exc).__name__}: {exc}"
            try:
                self._audit(
                    AuditRecord(
                        ts=now_iso(),
                        kind=audit_models.RUN_ERROR,
                        thread_id=thread_id,
                        detail=detail,
                    )
                )
            except Exception:  # noqa: BLE001, S110 - 已在错误路径上，不再二次抛出
                pass
            self._forget_thread(thread_id)
            yield RunFailed(message=detail)
            return

        # 成本策略 / 编排策略都需要仓库画像；只开启其中任一功能时才探一次。
        profile = (
            self._repo_profile()
            if self.settings.explore_enabled or self.settings.orchestrator_enabled
            else RepoProfile()
        )
        policy = delegation_policy(profile, self.settings)
        execution_mode = "direct"
        if (
            self.settings.orchestrator_enabled
            and profile.known
            and profile.file_count >= self.settings.orchestrator_min_files
        ):
            execution_mode = "orchestrated"

        inputs = {
            "messages": [HumanMessage(content=prompt)],
            # 独立 orchestrator 子图只读这一项，避免把整段消息历史复制进子图。
            "request": prompt,
            "cwd": self.workspace,
            "tool_rounds": 0,
            "budget_exhausted": False,
            "dirty": False,
            "run_dirty": False,
            "approvals": {},
            # 每轮都从文件重新读：会话中途 /remember 加的事实应当立刻生效
            "memories": memories,
            "repo_profile": {
                "file_count": profile.file_count,
                "test_file_count": profile.test_file_count,
            },
            "delegation_allowed": policy.allowed,
            "delegation_budget": policy.max_calls,
            # 每次 run 重置编排字段，避免同一 thread 的上一轮结果串味
            "execution_mode": execution_mode,
            "workflow": "",
            "workflow_reason": "",
            "repo_summary": {},
            "tasks": [],
            "task_status": {},
            "task_attempts": {},
            "worker_results": [],
            "orchestrator_round": 0,
            "orchestrator_degraded": False,
            "orchestrator_summary": "",
            "orchestrator_completed": 0,
            "orchestrator_failed": 0,
            "worker_context": "",
            "task_extensions_seen": 0,
            "task_extensions_applied": [],
            "task_extension_errors": [],
            # 每次 run 复位 steering；指令队列本身也清空（上一轮的迟到命令
            # 不该影响新一轮）。
            "steering_cancelled": False,
            "steering_replan": False,
            "steering_replans": 0,
            "steering_applied": [],
            "workflow_revised_reason": "",
        }
        self.steering.clear(thread_id)
        async for event in self._stream(
            inputs, thread_id=thread_id, fresh_run=True, prompt=prompt
        ):
            yield event

    def _resume_limit_error(self, thread_id: str) -> str:
        """恢复次数护栏；返回空串表示还可以恢复。

        `recursion_limit` 每次图调用都会重置，所以「挂起-恢复」必须在编排层
        封顶，否则前端可以无限续命。
        """
        count = self._resume_counts.get(thread_id, 0) + 1
        self._resume_counts[thread_id] = count
        if count <= self.settings.max_resumes:
            return ""
        return (
            f"会话 {thread_id} 的中断/恢复次数已超过上限 "
            f"{self.settings.max_resumes}，已停止。"
        )

    def _fail_resume(self, thread_id: str, detail: str) -> None:
        try:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.RUN_ERROR,
                    thread_id=thread_id,
                    detail=detail,
                )
            )
        except Exception:  # noqa: BLE001, S110 - 已在错误路径上，不再二次抛出
            pass
        self._forget_thread(thread_id)

    async def resume(self, thread_id: str, decisions: dict[str, bool]) -> AsyncIterator[Event]:
        """带着审批结果继续被挂起的图。

        decisions: call_id -> 是否批准。缺失的按拒绝处理（fail closed）。
        """
        detail = self._resume_limit_error(thread_id)
        if detail:
            self._fail_resume(thread_id, detail)
            yield RunFailed(message=detail)
            return

        command = Command(resume=dict(decisions))
        async for event in self._stream(command, thread_id=thread_id, fresh_run=False):
            yield event

    def steer(
        self,
        thread_id: str,
        action: str,
        *,
        text: str = "",
        role: str = "explore",
        source: str = "user",
    ) -> None:
        """提交一条运行中干预指令（TUI 的 /pause /cancel /add /replan）。

        指令在**节点边界**消费；pause/resume 通过 checkpoint 挂起与恢复。
        审计在**提交时**落账（来源/时间/动作），即使进程随后重启也能追溯。
        """
        command = SteeringCommand(
            action=action,
            text=text,
            role=role,
            source=source,
            ts=now_iso(),
        )
        detail = action if not text else f"{action}: {text}"
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.STEERING,
                thread_id=thread_id,
                detail=truncate(detail),
                source=source,
            )
        )
        self.steering.submit(thread_id, command)

    def is_paused(self, thread_id: str) -> bool:
        """图是否停在 steering 的 pause 上（同一进程内）。"""
        return self.steering.is_paused(thread_id)

    async def resume_steering(self, thread_id: str) -> AsyncIterator[Event]:
        """从 steering pause 恢复。审批恢复请走 `resume()`。"""
        detail = self._resume_limit_error(thread_id)
        if detail:
            self._fail_resume(thread_id, detail)
            yield RunFailed(message=detail)
            return

        yield SteeringApplied(action="resume", detail="从暂停处继续")
        command = Command(resume={"steering": "resume"})
        async for event in self._stream(command, thread_id=thread_id, fresh_run=False):
            yield event

    async def _stream(
        self, payload: Any, *, thread_id: str, fresh_run: bool, prompt: str = ""
    ) -> AsyncIterator[Event]:
        settings = self.settings
        config = {
            "configurable": {"thread_id": thread_id},
            # 按图拓扑推导，别再把系数算歪（见 estimate_recursion_limit）
            "recursion_limit": estimate_recursion_limit(
                settings.max_plan_steps,
                settings.max_tool_rounds,
                settings.max_repair_rounds,
                settings.max_replans,
                orchestrator_enabled=settings.orchestrator_enabled,
                orchestrator_max_rounds=settings.orchestrator_max_rounds,
                orchestrator_subgraph=settings.orchestrator_subgraph_enabled,
            ),
            # 这些会随 trace 一起上报，LangSmith 里可按会话/工作区/权限筛选
            "run_name": f"agent:{thread_id}",
            "tags": ["coding-agent", f"mode:{self.policy.approval_mode}"],
            "metadata": self._trace_metadata(thread_id),
        }

        answer = ""
        streamed: list[str] = []
        # 跨 run/resume 的状态；每次全新 run 清空，resume 时保留（见 __init__ 说明）
        pending = self._pending_calls.setdefault(thread_id, {})
        progress = self._progress.setdefault(thread_id, {})
        if fresh_run:
            pending.clear()
            progress.clear()
            self._resume_counts[thread_id] = 0
        # 模型回报的用量累计。**存 progress 而不是 _stream 的局部变量**：挂起与
        # 恢复是两次 `_stream`，存局部会让挂起之前那段用量整个丢掉 —— 而挂起
        # 恰恰常见于 L2/L3 审批，那是最费 token 的路径。provider 未提供时保持 0，
        # 收尾落成 None（0 与「未回报」必须能区分）。
        input_tokens = int(progress.get("input_tokens", 0))
        output_tokens = int(progress.get("output_tokens", 0))
        plan: list[str] = progress.setdefault("plan", [])
        plan_kinds: list[str] = progress.setdefault("plan_kinds", [])
        step_idx: int = progress.get("step_idx", 0)
        interrupted = False
        # 最近一次验证结果：repair 事件要带上它说明「在修什么」
        last_verification: dict[str, Any] = progress.setdefault("last_verification", {})
        # 最近一次代码审查结果。同理要跨挂起保留，且**由新的验证结果作废** ——
        # 否则上一步审查的阻断会被当成这一步的修复原因，repair 事件给出错误摘要。
        last_review: dict[str, Any] = progress.setdefault("last_review", {})
        # 步骤事件的配对状态：StepStarted 发出后必须有一个 StepFinished 收口。
        # 存在 progress（按 thread 留存）而不是局部变量 —— 挂起与恢复是两次
        # _stream，存局部会丢掉「上一段还开着一步」这件事，收尾时就不补发了。
        step_open: bool = progress.get("step_open", False)

        def _open_step(index: int, total: int, text: str) -> Iterator[Event]:
            """发 StepStarted。上一步若还开着，先补一个收口再开新的。"""
            nonlocal step_open
            if step_open:
                step_open = False
                yield StepFinished(index=step_idx, cancelled=True, text="")
            step_open = True
            progress["step_open"] = True
            yield StepStarted(index=index, total=total, text=text)

        def _close_step(*, cancelled: bool = False) -> Iterator[Event]:
            """收口当前步骤。已经关了就是空操作，不会多发一个事件。"""
            nonlocal step_open
            if not step_open:
                return
            step_open = False
            progress["step_open"] = False
            yield StepFinished(index=step_idx, cancelled=cancelled, text="")

        try:
            if fresh_run:
                self._audit(
                    AuditRecord(
                        ts=now_iso(),
                        kind=audit_models.RUN_START,
                        thread_id=thread_id,
                        workspace=self.workspace,
                        detail=truncate(prompt),
                    )
                )
                # 事件流起点。所有前端都能据此复位本轮状态（TUI 就靠它重置
                # 「最终答复」标记与流式缓冲）。只有全新 run 算新的一轮，
                # resume 不重复发 —— 那是同一次运行的延续。
                yield RunStarted(thread_id=thread_id)

            graph = await self._ensure_graph()
            # 只在一个 stream 调用内去重：子图中断会同时在带 namespace 的
            # 更新和父图更新里出现一次，但跨 resume 的重复暂停不能被吞掉。
            seen_interrupts: set[str] = set()
            async for chunk in graph.astream(
                payload,
                config,
                stream_mode=["messages", "updates", "custom"],
                # 独立子图的内层节点更新/自定义事件只有打开它才可见。
                subgraphs=True,
            ):
                if len(chunk) == 3:
                    _namespace, mode, data = chunk
                else:
                    mode, data = chunk
                if mode == "custom":
                    # Phase 4：dispatch 通过 get_stream_writer 实时推送 worker
                    # 开始/结束；updates 分支只补齐没被流式发过的事件。
                    payload_data = data if isinstance(data, dict) else {}
                    kind = str(payload_data.get("type", ""))
                    if kind == "worker_started":
                        key = _worker_key(payload_data)
                        seen = progress.setdefault("worker_started_keys", set())
                        if key not in seen:
                            seen.add(key)
                            yield WorkerStarted(
                                worker_id=str(
                                    payload_data.get("worker_id", "")
                                ),
                                task_id=str(payload_data.get("task_id", "")),
                                role=str(payload_data.get("role", "")),
                                goal=str(payload_data.get("goal", "")),
                                attempt=int(payload_data.get("attempt", 1)),
                            )
                    elif kind == "worker_finished":
                        result = payload_data.get("result") or {}
                        key = _worker_key(result)
                        seen = progress.setdefault(
                            "worker_finished_keys", set()
                        )
                        if key not in seen:
                            seen.add(key)
                            for event in self._worker_events(
                                result, thread_id, progress
                            ):
                                yield event
                    continue

                if mode == "messages":
                    chunk, meta = data
                    node = meta.get("langgraph_node", "")
                    text = text_of(chunk)
                    if node not in STREAMING_NODES or not text:
                        continue
                    if node == "respond":
                        streamed.append(text)
                    yield AssistantToken(node=node, text=text)
                    continue

                suspension = data.get("__interrupt__")
                if suspension:
                    interrupted = True
                    for item in suspension:
                        key = str(getattr(item, "id", "") or "")
                        if key:
                            if key in seen_interrupts:
                                continue
                            seen_interrupts.add(key)
                        payload = getattr(item, "value", None) or {}
                        if payload.get("steering") == "pause":
                            yield SteeringApplied(
                                action="pause",
                                detail=str(payload.get("message", "")),
                            )
                        else:
                            for request in self._interrupt_requests([item]):
                                yield request
                    continue

                for node, update in data.items():
                    if not isinstance(update, dict):
                        continue

                    # Phase 3D：四个 steering 消费点共用同一套事件载荷。
                    if node.startswith("steering"):
                        for applied in update.get("steering_applied") or []:
                            yield SteeringApplied(
                                action=str(applied.get("action", "")),
                                task_id=str(applied.get("task_id", "")),
                                detail=str(applied.get("detail", "")),
                            )
                        if update.get("steering_cancelled"):
                            for event in _close_step(cancelled=True):
                                yield event
                        continue

                    if node == "planner":
                        plan = [s for s in (update.get("plan") or []) if s]
                        plan_kinds = [
                            str(k) for k in (update.get("plan_kinds") or [])
                        ]
                        step_idx = 0
                        progress["plan"] = plan
                        progress["plan_kinds"] = plan_kinds
                        progress["step_idx"] = 0
                        if plan:
                            # 降级必须同时进事件与审计：只在事件里说的话，
                            # 事后复盘仍然查不到「这次规划其实是失败的」。
                            degraded = bool(update.get("plan_degraded"))
                            self._audit(
                                AuditRecord(
                                    ts=now_iso(),
                                    kind=audit_models.PLAN,
                                    thread_id=thread_id,
                                    steps=plan,
                                    kinds=plan_kinds or None,
                                    detail=_PLAN_DEGRADED_DETAIL if degraded else "",
                                )
                            )
                            yield PlanCreated(
                                steps=plan, kinds=plan_kinds, degraded=degraded
                            )
                        for event in _open_step(
                            0, max(len(plan), 1), plan[0] if plan else ""
                        ):
                            yield event

                    elif node == "select_workflow":
                        if "workflow" not in update:
                            continue
                        workflow = str(update.get("workflow", ""))
                        reason = str(update.get("workflow_reason", ""))
                        progress["workflow"] = workflow
                        self._audit(
                            AuditRecord(
                                ts=now_iso(),
                                kind=audit_models.WORKFLOW,
                                thread_id=thread_id,
                                workflow=workflow,
                                detail=truncate(reason),
                            )
                        )
                        yield WorkflowSelected(workflow=workflow, reason=reason)

                    elif node == "plan_tasks":
                        tasks = update.get("tasks") or []
                        if "tasks" not in update:
                            continue
                        degraded = bool(update.get("orchestrator_degraded"))
                        degradation_note = str(
                            update.get("orchestrator_summary", "")
                        ).strip()
                        self._audit(
                            AuditRecord(
                                ts=now_iso(),
                                kind=audit_models.TASK_GRAPH,
                                thread_id=thread_id,
                                workflow=progress.get("workflow"),
                                steps=[
                                    str(task.get("goal", "")) for task in tasks
                                ],
                                detail=(
                                    "任务图已降级"
                                    + (f"：{degradation_note}" if degradation_note else "")
                                    if degraded
                                    else ""
                                ),
                            )
                        )
                        yield TaskGraphCreated(tasks=tasks, degraded=degraded)
                        revised_reason = str(
                            update.get("workflow_revised_reason") or ""
                        )
                        if revised_reason:
                            yield WorkflowRevised(
                                reason=revised_reason, tasks=tasks
                            )

                    elif node == "extend_tasks":
                        added = update.get("task_extensions_applied") or []
                        rejected = update.get("task_extension_errors") or []
                        if added or rejected:
                            if added:
                                self._audit(
                                    AuditRecord(
                                        ts=now_iso(),
                                        kind=audit_models.TASK_GRAPH,
                                        thread_id=thread_id,
                                        workflow=progress.get("workflow"),
                                        steps=[
                                            str(task.get("goal", ""))
                                            for task in added
                                        ],
                                        detail=(
                                            f"运行中扩展 {len(added)} 个只读任务"
                                            + (
                                                f"；拒绝 {len(rejected)} 个"
                                                if rejected
                                                else ""
                                            )
                                        ),
                                    )
                                )
                            yield TaskGraphExtended(
                                added=added, rejected=rejected
                            )

                    elif node in {"dispatch", "scheduler"}:
                        results = update.get("worker_results") or []
                        emitted = int(progress.get("workers_emitted", 0))
                        fresh = results[emitted:]
                        # custom 流已经实时发过的事件不再重复；直接调用节点
                        # （单测）没有 custom writer 时，这里负责补齐。
                        started_seen = progress.setdefault(
                            "worker_started_keys", set()
                        )
                        finished_seen = progress.setdefault(
                            "worker_finished_keys", set()
                        )
                        for result in fresh:
                            key = _worker_key(result)
                            if key in started_seen:
                                continue
                            started_seen.add(key)
                            yield WorkerStarted(
                                worker_id=str(result.get("worker_id", "")),
                                task_id=str(result.get("task_id", "")),
                                role=str(result.get("role", "")),
                                goal=str(result.get("goal", "")),
                                attempt=int(result.get("attempt", 1)),
                                queue_wait_ms=int(result.get("queue_wait_ms", 0)),
                            )
                        for result in fresh:
                            key = _worker_key(result)
                            if key in finished_seen:
                                continue
                            finished_seen.add(key)
                            for event in self._worker_events(
                                result, thread_id, progress
                            ):
                                yield event
                        progress["workers_emitted"] = len(results)
                        # 事件驱动 scheduler 在同一个节点里完成动态扩展，
                        # 没有独立的 extend_tasks 更新可翻译。
                        added = update.get("task_extensions_applied") or []
                        rejected = update.get("task_extension_errors") or []
                        if added or rejected:
                            if added:
                                self._audit(
                                    AuditRecord(
                                        ts=now_iso(),
                                        kind=audit_models.TASK_GRAPH,
                                        thread_id=thread_id,
                                        workflow=progress.get("workflow"),
                                        steps=[
                                            str(task.get("goal", ""))
                                            for task in added
                                        ],
                                        detail=(
                                            f"运行中扩展 {len(added)} 个只读任务"
                                            + (
                                                f"；拒绝 {len(rejected)} 个"
                                                if rejected
                                                else ""
                                            )
                                        ),
                                    )
                                )
                            yield TaskGraphExtended(
                                added=added, rejected=rejected
                            )

                    elif node == "synthesize":
                        completed = int(update.get("orchestrator_completed", 0))
                        failed = int(update.get("orchestrator_failed", 0))
                        summary = str(update.get("orchestrator_summary", ""))
                        self._audit(
                            AuditRecord(
                                ts=now_iso(),
                                kind=audit_models.ORCHESTRATION,
                                thread_id=thread_id,
                                workflow=progress.get("workflow"),
                                ok=failed == 0,
                                detail=truncate(summary),
                            )
                        )
                        yield OrchestrationFinished(
                            completed=completed, failed=failed, summary=summary
                        )

                    elif node == "advance":
                        # 先收口再推进：补发的 StepFinished 必须带**旧**的 index，
                        # 而 step_idx 下一行就要被改掉
                        for event in _close_step():
                            yield event
                        step_idx = int(update.get("step_idx", step_idx + 1))
                        progress["step_idx"] = step_idx
                        for event in _open_step(
                            step_idx, len(plan), plan[step_idx] if step_idx < len(plan) else ""
                        ):
                            yield event

                    elif node == "replan":
                        # 节点不修订时返回空更新，只有真改了才发事件
                        if "plan" not in update:
                            continue
                        revised = [s for s in (update.get("plan") or []) if s]
                        plan_kinds = [
                            str(k) for k in (update.get("plan_kinds") or [])
                        ]
                        detail = (
                            f"剩余 {max(len(plan) - step_idx, 0)} 步"
                            f" → {max(len(revised) - step_idx, 0)} 步"
                        )
                        plan = revised
                        progress["plan"] = plan
                        progress["plan_kinds"] = plan_kinds
                        self._audit(
                            AuditRecord(
                                ts=now_iso(),
                                kind=audit_models.REPLAN,
                                thread_id=thread_id,
                                steps=plan,
                                kinds=plan_kinds or None,
                                detail=detail,
                            )
                        )
                        yield PlanRevised(
                            steps=plan, kinds=plan_kinds, step_idx=step_idx
                        )
                        # 计划被砍到当前这步之前（含当前这步）时，路由会直接去收尾，
                        # act 再也不跑 —— 这一步必须在这里收口，否则前端的进度条
                        # 永远停在「进行中」
                        if step_idx >= len(plan):
                            for event in _close_step(cancelled=True):
                                yield event

                    elif node == "act":
                        last = _last_message(update)
                        input_tokens, output_tokens = _add_usage(
                            last, input_tokens, output_tokens
                        )
                        progress["input_tokens"] = input_tokens
                        progress["output_tokens"] = output_tokens
                        calls = getattr(last, "tool_calls", None) or []
                        for call in calls:
                            started = self._tool_started(
                                str(call.get("name", "")),
                                dict(call.get("args") or {}),
                                str(call.get("id", "")),
                            )
                            pending[started.call_id] = started
                            yield started
                            # 与 ToolCallStarted 并列：delegate 本身也是一次工具调用，
                            # 但前端需要知道「这是一次委派、目标是什么」。
                            if started.name == DELEGATE_EXPLORE:
                                yield SubAgentStarted(
                                    agent_id=started.call_id,
                                    parent_call_id=started.call_id,
                                    role="explore",
                                    goal=str(dict(call.get("args") or {}).get("goal", "")),
                                )
                        if not calls:
                            step_open = False
                            progress["step_open"] = False
                            yield StepFinished(
                                index=step_idx,
                                budget_exhausted=bool(update.get("budget_exhausted")),
                                text=text_of(last),
                            )

                    elif node == "tools":
                        for message in update.get("messages") or []:
                            finished = self._tool_finished(message)
                            started = pending.pop(finished.call_id, None)
                            self._audit_tool_call(started, finished, thread_id)
                            yield finished

                            if started is not None and started.name == DELEGATE_EXPLORE:
                                artifact = parse_artifact(
                                    getattr(message, "artifact", None)
                                )
                                if isinstance(artifact, SubAgentArtifact):
                                    # 子智能体的 token 也算进本轮总量：不记的话，
                                    # 开启委派后成本会系统性偏低。
                                    input_tokens += artifact.input_tokens
                                    output_tokens += artifact.output_tokens
                                    progress["input_tokens"] = input_tokens
                                    progress["output_tokens"] = output_tokens
                                    self._audit_subagent(artifact, thread_id)
                                    yield self._subagent_finished(artifact)
                                else:
                                    # 被拒 / 预算耗尽 / 子智能体异常：没有结构化产物，
                                    # 但 started 已经发过，必须配对收口。
                                    # `rejected` 与 `failed` 必须分开：前者是宿主
                                    # 没让它跑（预算/审批），后者是跑了但没成。
                                    yield SubAgentFinished(
                                        agent_id=started.call_id,
                                        status=(
                                            "rejected" if finished.rejected else "failed"
                                        ),
                                        summary=_preview(
                                            str(getattr(message, "content", ""))
                                        ),
                                    )

                            changed = self._file_changed(message)
                            if changed is not None:
                                self._audit_file_change(changed, thread_id)
                                yield changed

                    elif node == "verify":
                        raw = update.get("verification") or {}
                        # 新的验证结果让上一次的审查结论作废：审查总是紧跟在验证
                        # 之后，所以"当前卡在哪一关"只需要看最新的一对。
                        last_review = {}
                        progress["last_review"] = {}
                        if raw:
                            last_verification = raw
                            progress["last_verification"] = raw
                            event = self._verification(raw)
                            self._audit(
                                AuditRecord(
                                    ts=now_iso(),
                                    kind=audit_models.VERIFY,
                                    thread_id=thread_id,
                                    # 没做验证时记 None（未验证），不记 True（通过）
                                    ok=_verification_verdict(raw),
                                    detail=truncate(
                                        f"[{raw.get('kind', 'none')}] "
                                        f"{raw.get('command', '')} → {raw.get('summary', '')}"
                                    ),
                                )
                            )
                            yield event

                    elif node == "review":
                        raw = update.get("review") or {}
                        if raw:
                            last_review = raw
                            progress["last_review"] = raw
                            blocked = _review_blocked(raw)
                            self._audit(
                                AuditRecord(
                                    ts=now_iso(),
                                    kind=audit_models.REVIEW,
                                    thread_id=thread_id,
                                    ok=not blocked,
                                    detail=truncate(
                                        f"{raw.get('checked_files', 0)} 个改动文件 → "
                                        f"{raw.get('summary', '')}"
                                    ),
                                )
                            )
                            yield self._review(raw)

                    elif node == "nudge":
                        # 一步没产生任何改动被打回重做。**刻意不发事件**：
                        # 它不是给用户看的结果，而是"这一步还没做完"的内部重试 ——
                        # 前端时间线上会看到这一步继续在动。但审计必须留痕：
                        # 事后要能回答"这次运行里有几步是被打回来的"。
                        self._audit(
                            AuditRecord(
                                ts=now_iso(),
                                kind=audit_models.NUDGE,
                                thread_id=thread_id,
                                detail=truncate(
                                    f"第 {step_idx + 1} 步未产生改动，已要求重做"
                                ),
                            )
                        )

                    elif node == "repair":
                        attempt = int(update.get("retry", 0))
                        limit = self.settings.max_repair_rounds
                        # 摘要按**阻断来源**取：修的是谁就说什么。一味看
                        # last_verification，在"审查阻断"时会带出验证的 ok 摘要。
                        if _review_blocked(last_review):
                            summary = str(last_review.get("summary", ""))
                            issues = self._review(last_review).findings
                        else:
                            summary = str(last_verification.get("summary", ""))
                            issues = self._verification(last_verification).issues
                        self._audit(
                            AuditRecord(
                                ts=now_iso(),
                                kind=audit_models.REPAIR,
                                thread_id=thread_id,
                                detail=truncate(f"第 {attempt}/{limit} 次修复（{summary}）"),
                            )
                        )
                        yield RepairStarted(
                            attempt=attempt,
                            limit=limit,
                            summary=summary,
                            issues=issues,
                        )

                    elif node == "respond":
                        last = _last_message(update)
                        input_tokens, output_tokens = _add_usage(
                            last, input_tokens, output_tokens
                        )
                        progress["input_tokens"] = input_tokens
                        progress["output_tokens"] = output_tokens
                        answer = text_of(last)

        except Exception as exc:  # noqa: BLE001 - 编排层异常也要以事件形式报给前端
            # 尽力记一笔，但**不能让它顶掉真正的错误**：审计本身坏了的时候
            # （比如磁盘满、目录被占），这里再抛一次就把原因盖掉了。
            try:
                self._audit(
                    AuditRecord(
                        ts=now_iso(),
                        kind=audit_models.RUN_ERROR,
                        thread_id=thread_id,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
            except Exception:  # noqa: BLE001, S110 - 已在错误路径上，不再二次抛出
                pass
            # 异常可能发生在任何节点上：此刻还开着的步骤再也不会被 act 收口，
            # 补发一个，免得前端把它显示成「仍在运行」
            for event in _close_step(cancelled=True):
                yield event
            worker_in = (
                int(progress.get("worker_input_tokens", 0))
                if progress.get("worker_tokens_reported")
                else None
            )
            worker_out = (
                int(progress.get("worker_output_tokens", 0))
                if progress.get("worker_tokens_reported")
                else None
            )
            self._forget_thread(thread_id)
            yield RunFailed(
                message=f"{type(exc).__name__}: {exc}",
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None,
                worker_input_tokens=worker_in,
                worker_output_tokens=worker_out,
            )
            return

        if interrupted:
            # 图已挂起：不发 RunFinished，让前端明确知道要 resume 而不是收工
            return

        final = answer or "".join(streamed)
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.RUN_END,
                thread_id=thread_id,
                detail=truncate(final),
                # provider 未回报用量时保持 None（exclude_none 会省略该字段）
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None,
            )
        )
        # 收尾兜底：正常路径下 act 已经收口（空操作），但若某条路径让 act 没能
        # 跑（计划被砍、异常恢复等），这里补上，保证 StepStarted 一定有配对
        for event in _close_step(cancelled=True):
            yield event
        worker_in = (
            int(progress.get("worker_input_tokens", 0))
            if progress.get("worker_tokens_reported")
            else None
        )
        worker_out = (
            int(progress.get("worker_output_tokens", 0))
            if progress.get("worker_tokens_reported")
            else None
        )
        self._forget_thread(thread_id)
        yield RunFinished(
            thread_id=thread_id,
            answer=final,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
            worker_input_tokens=worker_in,
            worker_output_tokens=worker_out,
        )

    # ------------------------------------------------------------------
    # 回滚（用户主动发起，不走审批；审批管的是模型发起的动作）
    # ------------------------------------------------------------------

    def list_snapshots(self, *, limit: int = 20) -> list[SnapshotEntry]:
        return self.snapshots.list(limit=limit)

    def restore(
        self, *, path: str | None = None, snapshot_id: str | None = None
    ) -> RestoreResult:
        """回滚文件并记审计。"""
        store = self.snapshots
        entry: SnapshotEntry | None = None

        # 解析失败同样要记账：用户以为回滚了但没回滚，审计里不能留空白
        def _failed(message: str) -> RestoreResult:
            result = RestoreResult(ok=False, path=path or "", message=message)
            self._audit_rollback(result, snapshot_id or "")
            return result

        if snapshot_id:
            entry = store.find(snapshot_id, path)
            if entry is None:
                return _failed(f"找不到快照 {snapshot_id}")
        elif path:
            entry = store.latest_for(path)
            if entry is None:
                return _failed(f"{path} 没有任何留底，无法回滚。")
        else:
            entries = store.list(limit=1)
            if not entries:
                return _failed("工作区里还没有任何快照，无法回滚。")
            entry = entries[0]

        result = store.restore(entry)
        self._audit_rollback(result, snapshot_id or "")
        return result

    def _audit_rollback(self, result: RestoreResult, requested_id: str) -> None:
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.ROLLBACK,
                workspace=self.workspace,
                path=result.path,
                action=ACTION_RESTORE,
                added=result.added,
                removed=result.removed,
                snapshot_id=result.snapshot_id or requested_id or None,
                ok=result.ok,
                detail=truncate(result.message),
            )
        )

    def diff_snapshot(self, *, path: str | None = None, snapshot_id: str | None = None) -> str:
        """展示某个快照与当前内容的差异（即「你都改了什么」）。"""
        store = self.snapshots
        entry: SnapshotEntry | None = None

        if snapshot_id:
            entry = store.find(snapshot_id, path)
        elif path:
            entry = store.latest_for(path)
        else:
            entries = store.list(limit=1)
            entry = entries[0] if entries else None

        if entry is None:
            return ""

        try:
            original = store.read(entry)
        except Exception:  # noqa: BLE001 - 快照读不出来时给空 diff，由前端提示
            return ""

        target = posixpath.join(store.root, entry.path)
        try:
            current = self._sandbox_fs().read_text(
                target, max_bytes=self.settings.max_file_read_bytes
            )
        except Exception:  # noqa: BLE001 - 文件已被删除时按空内容比较
            current = ""

        return unified_diff(original, current, entry.path)

    def _sandbox_fs(self) -> SandboxFs:
        if self._fs is None:
            self._fs = SandboxFs(self._sandbox, self.workspace)
        return self._fs

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def _audit(self, record: AuditRecord) -> None:
        """写一条审计记录。

        写失败会抛 AuditError 并被上层转成 RunFailed —— 审计有缺口是这个项目
        不能接受的失败模式，宁可让运行显式失败，也不静默丢记录。
        """
        self.audit.write(record)

    def _audit_tool_call(
        self,
        started: ToolCallStarted | None,
        finished: ToolCallFinished,
        thread_id: str,
    ) -> None:
        if finished.decision == DENIED:
            decision = audit_models.DECISION_DENIED
        elif finished.decision == APPROVED:
            decision = audit_models.DECISION_APPROVED
        elif finished.rejected:
            decision = audit_models.DECISION_REJECTED
        else:
            decision = audit_models.DECISION_AUTO
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.TOOL_CALL,
                thread_id=thread_id,
                call_id=finished.call_id,
                tool=finished.name,
                args=sanitize_args(dict(started.args) if started else {}),
                level=finished.level or (started.level if started else "") or "",
                decision=decision,
                ok=finished.ok,
                exit_code=finished.exit_code,
                duration_ms=finished.duration_ms,
            )
        )

    def _audit_subagent(self, artifact: SubAgentArtifact, thread_id: str) -> None:
        """把一次子智能体运行记进审计。

        - 一条 `subagent` 汇总记录（目标、状态、摘要、token）；
        - 子智能体实际执行的每个只读工具一条 `tool_call` 记录。
          没有后者的话，主审计里只能看到一次 delegate，子智能体读了什么完全不可见。
        """
        verdict = (
            True
            if artifact.status == "success"
            else False if artifact.status == "failed" else None
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.SUBAGENT,
                thread_id=thread_id,
                agent_id=artifact.agent_id,
                parent_call_id=artifact.parent_call_id or None,
                task=truncate(artifact.goal),
                ok=verdict,
                detail=truncate(artifact.summary),
                input_tokens=artifact.input_tokens or None,
                output_tokens=artifact.output_tokens or None,
                duration_ms=artifact.duration_ms,
            )
        )
        for call in artifact.tool_calls:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.TOOL_CALL,
                    thread_id=thread_id,
                    call_id=str(call.get("call_id", "")),
                    tool=str(call.get("name", "")),
                    args=sanitize_args(dict(call.get("args") or {})),
                    level="L0 只读",
                    decision=audit_models.DECISION_AUTO,
                    ok=bool(call.get("ok")),
                    duration_ms=call.get("duration_ms"),
                    agent_id=artifact.agent_id,
                    parent_call_id=artifact.parent_call_id or None,
                )
            )

    def _audit_file_change(self, changed: FileChanged, thread_id: str) -> None:
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.FILE_CHANGE,
                thread_id=thread_id,
                path=changed.path,
                action=changed.action,
                added=changed.added,
                removed=changed.removed,
                snapshot_id=changed.snapshot_id,
            )
        )

    # ------------------------------------------------------------------
    # 单条更新 → 事件
    # ------------------------------------------------------------------

    @staticmethod
    def _interrupt_requests(suspension: Any) -> list[ApprovalRequested]:
        """把 approval_gate 挂起时抛出的 payload 翻译成审批事件。"""
        requests: list[ApprovalRequested] = []
        for item in suspension or ():
            payload = getattr(item, "value", None) or {}
            for raw in payload.get("requests", []) or []:
                requests.append(
                    ApprovalRequested(
                        request_id=str(raw.get("call_id", "")),
                        tool=str(raw.get("tool", "")),
                        command=str(raw.get("command", "")),
                        level=str(raw.get("level", "")),
                        reason=str(raw.get("reason", "")),
                    )
                )
        return requests

    def _worker_events(
        self,
        result: dict[str, Any],
        thread_id: str,
        progress: dict[str, Any],
    ) -> Iterator[Event]:
        """一个 worker 结果 → WorkerFinished + 审计（custom/updates 共用）。

        只在结果首次被消费时调用；调用方用 `_worker_key` 去重。
        """
        status = str(result.get("status", ""))
        input_tokens = int(result.get("input_tokens") or 0)
        output_tokens = int(result.get("output_tokens") or 0)
        if input_tokens or output_tokens:
            progress["worker_input_tokens"] = (
                int(progress.get("worker_input_tokens", 0)) + input_tokens
            )
            progress["worker_output_tokens"] = (
                int(progress.get("worker_output_tokens", 0)) + output_tokens
            )
            progress["worker_tokens_reported"] = True

        yield WorkerFinished(
            worker_id=str(result.get("worker_id", "")),
            task_id=str(result.get("task_id", "")),
            role=str(result.get("role", "")),
            status=status,
            summary=str(result.get("summary", "")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int(result.get("duration_ms") or 0),
            attempt=int(result.get("attempt", 1)),
            merged=bool(result.get("merged")),
            merge_error=str(result.get("merge_error", "")),
            tool_calls=len(result.get("tool_calls") or []),
            queue_wait_ms=int(result.get("queue_wait_ms") or 0),
            llm_ms=int(result.get("llm_ms") or 0),
            tool_ms=int(result.get("tool_ms") or 0),
            timed_out=bool(result.get("timed_out")),
            artifact_id=str(result.get("artifact_id", "")),
            depth=int(result.get("depth") or 0),
            priority=int(result.get("priority") or 0),
            scope_violations=[
                str(item) for item in (result.get("scope_violations") or [])
            ],
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.WORKER,
                thread_id=thread_id,
                task_id=str(result.get("task_id", "")),
                role=str(result.get("role", "")),
                worker_id=str(result.get("worker_id", "")),
                attempt=int(result.get("attempt", 1)),
                merged=bool(result.get("merged")),
                ok=(
                    True
                    if status == "success"
                    else False
                    if status == "failed"
                    else None
                ),
                detail=truncate(
                    str(result.get("summary", ""))
                    + (
                        f" | error={result.get('error')}"
                        if result.get("error")
                        else ""
                    )
                    + (
                        " | snapshots="
                        + ",".join(
                            str(item)
                            for item in (result.get("snapshot_ids") or [])
                        )
                        if result.get("snapshot_ids")
                        else ""
                    )
                ),
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None,
                duration_ms=int(result.get("duration_ms") or 0),
            )
        )
        # worker 内部的工具调用逐条落账；否则子智能体改了哪些文件只存在于
        # 摘要里，审计无法追溯。
        for call in result.get("tool_calls") or []:
            call_name = str(call.get("name", ""))
            call_args = dict(call.get("args") or {})
            level = tool_level(call_name, call_args)
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.TOOL_CALL,
                    thread_id=thread_id,
                    call_id=str(call.get("call_id", "")),
                    tool=call_name,
                    args=sanitize_args(call_args),
                    level=level.label,
                    decision=audit_models.DECISION_AUTO,
                    ok=bool(call.get("ok")),
                    duration_ms=call.get("duration_ms"),
                    agent_id=str(result.get("worker_id", "")) or None,
                    task_id=str(result.get("task_id", "")) or None,
                )
            )

    def _tool_started(self, name: str, args: dict[str, Any], call_id: str = "") -> ToolCallStarted:
        # 这里重新判一次等级纯粹是为了给前端着色；真正的放行/拒绝在工具内部，
        # 不存在"UI 判定通过就执行"的路径。
        level = None
        if name == SHELL_TOOL_NAME:
            level = classify(str(args.get("command", ""))).level.label
        return ToolCallStarted(
            call_id=call_id,
            name=name,
            args=args,
            summary=_summarize_args(args),
            level=level,
        )

    def _tool_finished(self, message: Any) -> ToolCallFinished:
        name = str(getattr(message, "name", "") or "")
        call_id = str(getattr(message, "tool_call_id", "") or "")
        content = str(getattr(message, "content", ""))
        preview = _preview(content)
        artifact = parse_artifact(getattr(message, "artifact", None))

        if isinstance(artifact, ShellArtifact):
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=artifact.ok,
                rejected=artifact.rejected,
                decision=artifact.decision,
                exit_code=artifact.exit_code,
                duration_ms=artifact.duration_ms,
                level=artifact.level_label or None,
                preview=preview,
            )

        if isinstance(artifact, FileArtifact):
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=artifact.ok,
                rejected=artifact.rejected,
                decision=artifact.decision,
                level="文件工具",
                preview=preview,
            )

        if isinstance(artifact, CallArtifact):
            # 非 shell / 非文件的工具（git_commit、deps_install、run_tests…）。
            # 被拒时也走这里，level 用工具自身的等级，而不是「文件工具」。
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=artifact.ok,
                rejected=artifact.rejected,
                decision=artifact.decision,
                level=artifact.level_label or None,
                preview=preview,
            )

        if isinstance(artifact, SubAgentArtifact):
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                # 只有 success 才算真正完成；partial 仍会把摘要给模型，
                # 但审计与事件都不该把它记成「通过」。
                ok=artifact.status == "success",
                rejected=False,
                decision=artifact.decision,
                duration_ms=artifact.duration_ms,
                level="探索子智能体",
                preview=preview,
            )

        # 未提供 artifact 的工具：只能从文本粗判成败。新工具都应带 artifact。
        return ToolCallFinished(
            call_id=call_id,
            name=name,
            ok=not content.startswith(_FAILURE_MARKERS),
            preview=preview,
        )

    @staticmethod
    def _subagent_finished(artifact: SubAgentArtifact) -> SubAgentFinished:
        return SubAgentFinished(
            agent_id=artifact.agent_id,
            status=artifact.status,
            summary=artifact.summary,
            relevant_files=list(artifact.relevant_files),
            findings=list(artifact.findings),
            input_tokens=artifact.input_tokens,
            output_tokens=artifact.output_tokens,
            duration_ms=artifact.duration_ms,
        )

    @staticmethod
    def _review(raw: dict[str, Any]) -> ReviewFinished:
        findings = [
            " ".join(
                f"[{f.get('severity', '')}] {f.get('location', '')} {f.get('message', '')}".split()
            )
            for f in (raw.get("findings") or [])
        ]
        return ReviewFinished(
            status=str(raw.get("status", "skipped")),
            blocked=_review_blocked(raw),
            summary=str(raw.get("summary", "")),
            findings=findings,
        )

    @staticmethod
    def _verification(raw: dict[str, Any]) -> Verification:
        issues = [
            " ".join(f"{i.get('location', '')} {i.get('message', '')}".split())
            for i in (raw.get("issues") or [])
        ]
        return Verification(
            status=str(raw.get("status", "skipped")),
            command=str(raw.get("command", "")),
            # 与审计同源：都用三态口径，两边的结论必须一致
            ok=_verification_verdict(raw),
            summary=str(raw.get("summary", "")),
            issues=issues,
            failed_tests=[str(t) for t in (raw.get("failed_tests") or [])],
        )

    @staticmethod
    def _file_changed(message: Any) -> FileChanged | None:
        artifact = parse_artifact(getattr(message, "artifact", None))
        if not isinstance(artifact, FileArtifact):
            return None
        if not artifact.ok or artifact.action not in MUTATING_FILE_ACTIONS:
            return None
        return FileChanged(
            path=artifact.path,
            action=artifact.action,
            added=artifact.added,
            removed=artifact.removed,
            diff=artifact.diff,
            snapshot_id=artifact.snapshot_id,
        )
