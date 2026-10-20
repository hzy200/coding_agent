"""动态工作流选择（Phase 3A）。

规则优先，规则不确定时才问模型；两者都失败时退回 `direct`。模型只能在
预置模板里选，不能自由发明工作流。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import with_json_output
from coding_agent.orchestrator.contracts import TaskSpec, WorkflowDecision
from coding_agent.orchestrator.state import request_of


class WorkflowPlan(BaseModel):
    """工作流选择 + 任务图一次返回，避免模糊请求付两次 LLM 往返。"""

    workflow: Literal[
        "direct", "parallel_readonly", "orchestrated_implement"
    ] = "direct"
    reason: str = ""
    tasks: list[TaskSpec] = Field(default_factory=list)
    degraded: bool = False

# 明确的「多交付物/多区域」信号：命中时不必再问模型。
_MULTI_HINTS = (
    "同时",
    "分别",
    "各自",
    "多个",
    "两个",
    "两件",
    "三个",
    "两条",
    "一起",
    "多条",
    "另外",
    "各加",
    "各修",
    "都要",
)

WORKFLOW_PROMPT = """\
你是编码智能体的编排器。根据用户请求和仓库画像，在三种工作流中选择：

- `direct`：单 agent 直接执行。适合单点修改、单一功能线、小范围任务。
- `parallel_readonly`：先生成 2–4 个只读子任务（explore/test/review），并发收集
  事实，再把汇总交给主 agent 实现。适合跨多个模块、需要同时定位多处、或有多个
  独立交付物的任务。
- `orchestrated_implement`：生成多个**互相独立**的 implement 任务，各自在 git
  worktree 中并发修改，最后由宿主合并补丁。仅在写编排已开启、工作区是干净的
  git 仓库、且任务能拆成**写范围不重叠**的独立块时选择。

判断要保守：如果一条功能线是连贯的、改几个文件就能完成，选 `direct`；
只有确实能拆成互不依赖的区域时才选并行模板。写任务之间不能有依赖，
write_scope 不能重叠。

只输出 JSON：
{"workflow": "direct|parallel_readonly|orchestrated_implement", "reason": "一句话"}
"""


def rule_workflow(
    request: str,
    summary: dict[str, Any],
    settings: Settings,
    *,
    write_available: bool = False,
) -> WorkflowDecision | None:
    """返回确定性的选择；None 表示规则不确定，交给模型。"""
    file_count = int(summary.get("file_count") or 0)
    if not file_count or file_count < settings.orchestrator_min_files:
        return WorkflowDecision(
            workflow="direct",
            reason=f"仓库只有 {file_count or '未知'} 个文件，直接执行更便宜",
        )
    if any(hint in request for hint in _MULTI_HINTS):
        if write_available:
            return WorkflowDecision(
                workflow="orchestrated_implement",
                reason="请求里有多个独立交付物，且写编排可用",
            )
        return WorkflowDecision(
            workflow="parallel_readonly",
            reason="请求里出现多个独立交付物/区域",
        )
    return None


def make_select_workflow_node(
    llm: BaseChatModel, settings: Settings, *, allow_write: bool = False
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    structured = with_json_output(llm, WorkflowPlan).with_retry(
        stop_after_attempt=2
    )

    async def select_workflow(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        request = request_of(state)
        summary = state.get("repo_summary") or {}
        write_available = bool(
            settings.orchestrator_write_enabled
            and allow_write
            and summary.get("git")
            and summary.get("git_root")
            and summary.get("git_clean")
        )
        decision = rule_workflow(
            request, summary, settings, write_available=write_available
        )
        prefetched: list[dict[str, Any]] = []
        prefetched_degraded = False

        if decision is None:
            # 规则不确定时一次调用同时拿到 workflow 和任务图，省掉
            # select → plan 的第二次 LLM 往返。
            from coding_agent.orchestrator.plan import (
                _IMPLEMENT_ROLE_NOTES,
                _IMPLEMENT_RULES,
                _READ_ONLY_ROLE_NOTES,
                _READ_ONLY_RULES,
                PLAN_TASKS_PROMPT,
            )

            task_prompt = PLAN_TASKS_PROMPT.format(
                max_tasks=settings.orchestrator_max_tasks,
                role_notes=(
                    _IMPLEMENT_ROLE_NOTES
                    if write_available
                    else _READ_ONLY_ROLE_NOTES
                ),
                write_rules=(
                    _IMPLEMENT_RULES
                    if write_available
                    else _READ_ONLY_RULES
                ),
            )
            body = (
                f"用户请求：{request}\n\n"
                f"仓库画像：{summary}\n\n"
                "请先选择 workflow，再给出任务图。"
                "输出 JSON："
                '{"workflow": "direct|parallel_readonly|orchestrated_implement",'
                ' "reason": "一句话", "tasks": [...]}'
            )
            try:
                plan = await structured.ainvoke(
                    [
                        SystemMessage(
                            content=WORKFLOW_PROMPT + "\n\n" + task_prompt
                        ),
                        HumanMessage(content=body),
                    ],
                    config,
                )
            except Exception:  # noqa: BLE001 - 选择失败不能拖垮图
                plan = None

            if isinstance(plan, WorkflowPlan):
                decision = WorkflowDecision(
                    workflow=plan.workflow, reason=plan.reason
                )
                prefetched = [task.model_dump() for task in plan.tasks]
                prefetched_degraded = bool(plan.degraded)
            elif isinstance(plan, WorkflowDecision):
                decision = plan

        if decision is None:
            decision = WorkflowDecision(
                workflow="direct", reason="工作流选择失败，回退 direct"
            )

        # 模型可能无视条件选择写模板；这里做硬性兜底。
        if decision.workflow == "orchestrated_implement" and not write_available:
            decision = WorkflowDecision(
                workflow="parallel_readonly",
                reason="写编排不可用，回退为只读并行",
            )
            prefetched = []

        return {
            "workflow": decision.workflow,
            "workflow_reason": decision.reason,
            "prefetched_tasks": prefetched,
            "prefetched_degraded": prefetched_degraded,
        }

    return select_workflow
