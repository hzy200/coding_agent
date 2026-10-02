"""AgentRuntime —— 唯一编排入口。

所有前端（CLI / TUI / Web）都只跟这一层打交道：
它持有图、沙箱与安全策略，把 LangGraph 的原始流翻译成语义化的领域事件。
前端拿不到工具，也就无法绕过命令分级审批。

    async for event in AgentRuntime(settings).run("列出文件", thread_id="x"):
        match event.type: ...
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.audit import models as audit_models
from coding_agent.audit.logger import now_iso, sanitize_args, truncate
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
from coding_agent.graph.build import build_graph
from coding_agent.memory.checkpointer import CheckpointStore
from coding_agent.messages import text_of
from coding_agent.sandbox.pathguard import normalize_root
from coding_agent.sandbox.policy import classify
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import FileArtifact, ShellArtifact, parse_artifact
from coding_agent.tools.shell import SHELL_TOOL_NAME

# 只有这几个 action 才真正改动了文件，read 不发 FileChanged
MUTATING_FILE_ACTIONS = frozenset({"create", "overwrite", "edit"})

# 只有这两个节点的模型输出是给用户看的文本；planner 的结构化调用不流式渲染
STREAMING_NODES = frozenset({"act", "respond"})

_PREVIEW_CHARS = 240
_FAILURE_MARKERS = ("工具执行异常：", "错误：")
_SUMMARY_KEYS = ("command", "path", "pattern", "query")


def _last_message(update: dict[str, Any]) -> Any:
    messages = update.get("messages") or []
    return messages[-1] if messages else None


def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _summarize_args(args: dict[str, Any]) -> str:
    for key in _SUMMARY_KEYS:
        if args.get(key):
            return str(args[key])
    return ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])


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
    ) -> None:
        self.settings = settings or get_settings()
        self.allow_write = allow_write
        self._sandbox = WslSandbox(self.settings)
        self._workspace = normalize_root(workspace) if workspace else None
        self._graph: Any = None

        # 显式传入的 checkpointer 由调用方负责生命周期；否则按配置自行管理
        self._external_checkpointer = checkpointer
        self._store = (
            None
            if checkpointer is not None
            else CheckpointStore(self.settings.resolved_checkpoint_path)
        )
        self.audit = audit or AuditLogger(
            self.settings.resolved_audit_dir, enabled=self.settings.audit_enabled
        )

    @property
    def workspace(self) -> str:
        """沙箱内的工作区绝对路径，按需解析并缓存。"""
        if self._workspace is None:
            self._workspace = resolve_workspace(self.settings, self._sandbox)
        return self._workspace

    @property
    def audit_path(self) -> Path:
        return self.audit.path

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

    @property
    def effective_settings(self) -> Settings:
        """把解析后的工作区写回配置。

        工具的路径守卫是从 settings 里读工作区的，若这里不写回，
        `--workspace` 覆盖只会影响图状态里的 cwd，工具仍按旧根目录判边界，
        结果是所有命令都被判「工作目录非法」。
        """
        return self.settings.model_copy(update={"wsl_workspace": self.workspace})

    async def _ensure_graph(self) -> Any:
        """编译后的图，首次使用时才构建。

        构造 AgentRuntime 不应触发建图 —— 否则只想读 workspace 或渲染 UI 的
        前端也会被迫要求 API Key、并做一次无谓的沙箱探测。
        """
        if self._graph is None:
            checkpointer = (
                self._external_checkpointer
                if self._external_checkpointer is not None
                else await self._store.aopen()
            )
            self._graph = build_graph(
                self.effective_settings,
                checkpointer=checkpointer,
                allow_write=self.allow_write,
            )
        return self._graph

    # ------------------------------------------------------------------
    # 事件流
    # ------------------------------------------------------------------

    async def run(self, prompt: str, *, thread_id: str) -> AsyncIterator[Event]:
        settings = self.settings
        config = {
            "configurable": {"thread_id": thread_id},
            # planner + 每步 (act/tools 对 + advance) + respond 的宽松上界
            "recursion_limit": settings.max_plan_steps * (2 * settings.max_tool_rounds + 2) + 10,
        }
        inputs = {
            "messages": [HumanMessage(content=prompt)],
            "cwd": self.workspace,
            "tool_rounds": 0,
            "budget_exhausted": False,
        }

        plan: list[str] = []
        step_idx = 0
        answer = ""
        streamed: list[str] = []
        pending: dict[str, ToolCallStarted] = {}

        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.RUN_START,
                thread_id=thread_id,
                workspace=self.workspace,
                detail=truncate(prompt),
            )
        )

        try:
            graph = await self._ensure_graph()
            async for mode, data in graph.astream(
                inputs, config, stream_mode=["messages", "updates"]
            ):
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

                for node, update in data.items():
                    if not isinstance(update, dict):
                        continue

                    if node == "planner":
                        plan = [s for s in (update.get("plan") or []) if s]
                        step_idx = 0
                        if plan:
                            self._audit(
                                AuditRecord(
                                    ts=now_iso(),
                                    kind=audit_models.PLAN,
                                    thread_id=thread_id,
                                    steps=plan,
                                )
                            )
                            yield PlanCreated(steps=plan)
                        yield StepStarted(
                            index=0,
                            total=max(len(plan), 1),
                            text=plan[0] if plan else prompt,
                        )

                    elif node == "advance":
                        step_idx = int(update.get("step_idx", step_idx + 1))
                        yield StepStarted(
                            index=step_idx,
                            total=len(plan),
                            text=plan[step_idx] if step_idx < len(plan) else "",
                        )

                    elif node == "act":
                        last = _last_message(update)
                        calls = getattr(last, "tool_calls", None) or []
                        for call in calls:
                            started = self._tool_started(
                                str(call.get("name", "")),
                                dict(call.get("args") or {}),
                                str(call.get("id", "")),
                            )
                            pending[started.call_id] = started
                            yield started
                        if not calls:
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

                            changed = self._file_changed(message)
                            if changed is not None:
                                self._audit_file_change(changed, thread_id)
                                yield changed

                    elif node == "respond":
                        answer = text_of(_last_message(update))

        except Exception as exc:  # noqa: BLE001 - 编排层异常也要以事件形式报给前端
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.RUN_ERROR,
                    thread_id=thread_id,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            yield RunFailed(message=f"{type(exc).__name__}: {exc}")
            return

        final = answer or "".join(streamed)
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.RUN_END,
                thread_id=thread_id,
                detail=truncate(final),
            )
        )
        yield RunFinished(thread_id=thread_id, answer=final)

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
        if finished.rejected:
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
                level="文件工具",
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
