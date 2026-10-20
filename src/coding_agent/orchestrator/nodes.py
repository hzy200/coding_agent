"""编排节点的兼容导出层。

真正的实现已按职责拆分：

- `survey.py`  → 仓库画像
- `plan.py`    → 任务图生成/重规划
- `scheduler.py` → 事件驱动调度
- `fanin.py`   → 汇总与 artifact
- `merge.py`   → 补丁校验/快照/合并
- `routing.py` → 入口条件边

这里只保留稳定导入路径，以及两个测试/旧调用方仍在使用的轻量适配器。
生产子图直接使用 `scheduler`，不再走按批 `dispatch`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from coding_agent.config import Settings
from coding_agent.messages import last_human_text
from coding_agent.orchestrator.fanin import make_synthesize_node
from coding_agent.orchestrator.graph import validate_proposals
from coding_agent.orchestrator.merge import (
    _normalize_patch_path,
    _patch_paths,
    _snapshot_paths,
)
from coding_agent.orchestrator.plan import (
    _IMPLEMENT_ROLE_NOTES,
    _IMPLEMENT_RULES,
    _READ_ONLY_ROLE_NOTES,
    _READ_ONLY_RULES,
    PLAN_TASKS_PROMPT,
    make_plan_tasks_node,
)
from coding_agent.orchestrator.routing import route_after_select, route_entry
from coding_agent.orchestrator.scheduler import make_scheduler_node
from coding_agent.orchestrator.survey import make_survey_node
from coding_agent.orchestrator.workers import WorkerRunner
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.steering import SteeringController

__all__ = [
    "_IMPLEMENT_ROLE_NOTES",
    "_IMPLEMENT_RULES",
    "_READ_ONLY_ROLE_NOTES",
    "_READ_ONLY_RULES",
    "PLAN_TASKS_PROMPT",
    "_normalize_patch_path",
    "_patch_paths",
    "_snapshot_paths",
    "make_dispatch_node",
    "make_extend_tasks_node",
    "make_plan_tasks_node",
    "make_route_after_dispatch",
    "make_survey_node",
    "make_synthesize_node",
    "route_after_select",
    "route_entry",
]


def make_dispatch_node(
    *,
    worker_runner: WorkerRunner,
    settings: Settings,
    sandbox: WslSandbox | None = None,
    root: str = "",
    steering: SteeringController | None = None,
) -> Callable[[dict[str, Any], RunnableConfig], Awaitable[dict[str, Any]]]:
    """兼容适配器：旧调用方传 `messages`，scheduler 读 `request`。

    生产路径已统一到 scheduler；保留这个薄封装只为现有测试与集成调用。
    """
    scheduler = make_scheduler_node(
        worker_runner=worker_runner,
        settings=settings,
        sandbox=sandbox,
        root=root,
        steering=steering,
    )

    async def dispatch(
        state: dict[str, Any], config: RunnableConfig
    ) -> dict[str, Any]:
        if not state.get("request") and state.get("messages"):
            state = {
                **state,
                "request": last_human_text(state.get("messages") or []),
            }
        return await scheduler(state, config)

    return dispatch


def make_extend_tasks_node(
    settings: Settings,
) -> Callable[[dict[str, Any], RunnableConfig], dict[str, Any]]:
    """兼容：单独消费 worker 提议的只读任务（scheduler 已内置该逻辑）。"""

    def extend_tasks(
        state: dict[str, Any], config: RunnableConfig
    ) -> dict[str, Any]:
        tasks = [dict(task) for task in (state.get("tasks") or [])]
        results = list(state.get("worker_results") or [])
        seen = int(state.get("task_extensions_seen", 0))
        fresh = results[seen:]
        proposals: list[dict[str, Any]] = []
        for result in fresh:
            if result.get("status") not in {"success", "partial"}:
                continue
            proposals.extend((result.get("proposed_tasks") or [])[:2])
        new_tasks, errors = validate_proposals(
            proposals,
            existing=tasks,
            max_total=settings.orchestrator_max_tasks,
        )
        status = dict(state.get("task_status") or {})
        for task in new_tasks:
            status[task["id"]] = "pending"
        return {
            "tasks": tasks + new_tasks,
            "task_status": status,
            "task_extensions_seen": len(results),
            "task_extensions_applied": new_tasks,
            "task_extension_errors": errors,
        }

    return extend_tasks


def make_route_after_dispatch(
    settings: Settings,
) -> Callable[[dict[str, Any]], str]:
    """兼容：旧条件边语义（生产子图由 scheduler 的 continue/done 驱动）。"""

    def route_after_dispatch(state: dict[str, Any]) -> str:
        if state.get("scheduler_continue"):
            return "dispatch"
        pending = any(
            value == "pending"
            for value in (state.get("task_status") or {}).values()
        )
        return "dispatch" if pending else "synthesize"

    return route_after_dispatch
