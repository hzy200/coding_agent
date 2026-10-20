"""任务图生成与重规划。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from coding_agent.config import Settings
from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import with_json_output
from coding_agent.orchestrator.contracts import TaskGraph
from coding_agent.orchestrator.graph import validate_tasks
from coding_agent.orchestrator.state import request_of


def _default_write_task(
    request: str, summary: dict[str, Any]
) -> dict[str, Any] | None:
    """写模式下的保守兜底：单个 implement，覆盖源码顶层目录。"""
    dirs = [
        str(item).strip().strip("/")
        for item in (summary.get("top_dirs") or [])
        if str(item).strip()
        and str(item).strip() not in {"tests", "test", ".agent"}
    ]
    files = [
        str(item).strip().strip("/")
        for item in (summary.get("top_files") or [])
        if str(item).strip() and not str(item).startswith(".")
    ]
    scope = [f"{directory}/" for directory in dirs] if dirs else files
    if not scope:
        return None
    return {
        "id": "implement-1",
        "role": "implement",
        "goal": request or "完成用户请求",
        "inputs": [],
        "acceptance": [],
        "depends_on": [],
        "write_scope": scope,
    }


PLAN_TASKS_PROMPT = """\
你是编码智能体的编排器。把任务拆成 1–{max_tasks} 个子任务，交给并发 worker：

{role_notes}

{write_rules}

硬性要求：
- 每个任务必须有一个清晰、可独立完成的目标；
- 只有确实需要前序结果时才写 depends_on；
- `review` 通常应独立于 `test`（除非要专门审查测试结果），否则测试失败会把审查一起阻塞；
- 不要把一条连贯的功能线拆成多个重复的 explore；
- review/test 若只需要依赖任务的产出（即使它最终是 partial），把 depends_mode
  设为 "any"；其余任务保持 "full"（默认）。
- 只输出 JSON：{{"tasks": [{{"id": "t1", "role": "explore", "goal": "...",
  "inputs": [], "acceptance": [], "depends_on": [], "depends_mode": "full",
  "write_scope": []}}]}}
"""

_READ_ONLY_ROLE_NOTES = """\
- `explore`：定位/理解代码。给出 goal、inputs（优先看的文件/目录）、acceptance。
- `test`：运行项目现有测试并总结失败项。无需 inputs。
- `review`：对 inputs 中的文件做只读代码审查，返回风险/遗漏。不要修改文件。
- `test`/`review` 依赖实现任务时可用 depends_mode="any"：实现任务的产出
  （文件/摘要）已经可用即可，不要求它通过完整验证。"""

_IMPLEMENT_ROLE_NOTES = """\
- `implement`：修改 `write_scope` 内的文件完成任务。必须给出 write_scope
  （文件或目录，例如 "app/reporting.py" 或 "app/handlers/"）。
- `explore` / `test` / `review`：只读辅助任务，不能修改文件。
- `test`/`review` 依赖 implement 时可用 depends_mode="any" 消费 partial 产出。"""

_READ_ONLY_RULES = "- 只允许 explore / test / review，**不要生成实现/修改任务**。"

_IMPLEMENT_RULES = """\
- 允许 explore / test / review / implement。
- 每个 implement 必须写 write_scope；不同 implement 的 write_scope **不能重叠**。
- 如果用户请求点名了具体文件（例如「在 app/alpha.py 实现…」），write_scope
  必须**精确到这些文件**，不要用整个目录。
- implement 任务之间**不能有 depends_on**（它们基于同一个 HEAD 并发修改）。
- implement 只改源码，不要改测试文件（除非任务明确要求）。"""


def _completed_view(
    tasks: list[dict[str, Any]],
    status: dict[str, str],
    results: list[dict[str, Any]],
) -> str:
    """给重规划模型看的「已完成，不要重复」清单。"""
    latest: dict[str, dict[str, Any]] = {}
    for result in results:
        task_id = str(result.get("task_id", ""))
        previous = latest.get(task_id)
        if previous is None or int(result.get("attempt", 1)) >= int(
            previous.get("attempt", 1)
        ):
            latest[task_id] = result
    lines: list[str] = []
    for task in tasks:
        task_id = str(task.get("id", ""))
        item = latest.get(task_id) or {}
        summary = str(item.get("summary", "")).strip()[:160]
        merged = "，已合并" if item.get("merged") else ""
        lines.append(
            f"- {task_id} ({task.get('role', '')}/{status.get(task_id, '')})"
            f"{merged}: {summary}"
        )
    return "\n".join(lines) or "（无）"


def make_plan_tasks_node(
    llm: BaseChatModel, settings: Settings, *, allow_write: bool = False
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    structured = with_json_output(llm, TaskGraph).with_retry(stop_after_attempt=2)

    async def plan_tasks(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        request = request_of(state)
        summary = state.get("repo_summary") or {}
        replan_mode = bool(state.get("steering_replan"))
        existing_tasks = [dict(task) for task in (state.get("tasks") or [])]
        existing_status = dict(state.get("task_status") or {})
        results = state.get("worker_results") or []
        completed_tasks = [
            task
            for task in existing_tasks
            if existing_status.get(task["id"]) in {"success", "partial"}
        ]
        merged_any = any(
            result.get("role") == "implement" and result.get("merged")
            for result in results
        )
        write_mode = bool(
            allow_write
            and settings.orchestrator_write_enabled
            and state.get("workflow") == "orchestrated_implement"
            and not merged_any
        )
        body = (
            f"用户请求：{request}\n\n"
            f"仓库画像：{summary}\n\n"
            f"工作流：{state.get('workflow', '')}\n\n"
        )
        if replan_mode:
            body += (
                "用户要求重新规划剩余工作。以下任务已经完成，不要重复：\n"
                f"{_completed_view(completed_tasks, existing_status, results)}\n\n"
            )
            if merged_any:
                body += "注意：已有实现补丁合并；新增任务只能使用 explore/test/review。\n\n"
        body += "请生成任务图。"

        tasks: list[dict[str, Any]] = []
        degraded = False
        errors: list[str] = []
        prefetched = [
            dict(task) for task in (state.get("prefetched_tasks") or [])
        ]
        if prefetched and not replan_mode:
            # select_workflow 已经把任务图一并返回；不要再付一次 LLM 往返。
            tasks, degraded, errors = validate_tasks(
                prefetched,
                request=request,
                max_tasks=settings.orchestrator_max_tasks,
                allow_write=write_mode,
                allow_overlap=settings.orchestrator_allow_overlap,
            )
            degraded = degraded or bool(state.get("prefetched_degraded"))
        else:
            try:
                graph = await structured.ainvoke(
                    [
                        SystemMessage(
                            content=PLAN_TASKS_PROMPT.format(
                                max_tasks=settings.orchestrator_max_tasks,
                                role_notes=(
                                    _IMPLEMENT_ROLE_NOTES
                                    if write_mode
                                    else _READ_ONLY_ROLE_NOTES
                                ),
                                write_rules=(
                                    (
                                        _IMPLEMENT_RULES
                                        + (
                                            "\n- 允许不同 implement 修改同一文件；"
                                            "宿主会用 3-way 合并，冲突时整批回滚。"
                                            if settings.orchestrator_allow_overlap
                                            else ""
                                        )
                                    )
                                    if write_mode
                                    else _READ_ONLY_RULES
                                ),
                            )
                        ),
                        HumanMessage(content=body),
                    ],
                    config,
                )
                tasks, degraded, errors = validate_tasks(
                    graph.tasks,
                    request=request,
                    max_tasks=settings.orchestrator_max_tasks,
                    allow_write=write_mode,
                    allow_overlap=settings.orchestrator_allow_overlap,
                )
                degraded = degraded or bool(graph.degraded)
            except Exception:  # noqa: BLE001 - 规划失败降级
                tasks, degraded, errors = validate_tasks(
                    [],
                    request=request,
                    max_tasks=settings.orchestrator_max_tasks,
                    allow_write=write_mode,
                    allow_overlap=settings.orchestrator_allow_overlap,
                )

        if degraded and write_mode:
            fallback = _default_write_task(request, summary)
            if fallback is not None:
                tasks, fallback_degraded, fallback_errors = validate_tasks(
                    [fallback],
                    request=request,
                    max_tasks=settings.orchestrator_max_tasks,
                    allow_write=True,
                )
                if not fallback_degraded:
                    degraded = True
                else:
                    errors = errors + fallback_errors
            else:
                errors.append(
                    "写编排降级：工作区没有可用的顶层目录或文件作为 write_scope"
                )

        if (
            not degraded
            and not write_mode
            and len(tasks) == 1
            and settings.orchestrator_add_explore_tests
        ):
            used = {task["id"] for task in tasks}
            extra_id = "explore-tests"
            while extra_id in used:
                extra_id += "-2"
            tasks.append(
                {
                    "id": extra_id,
                    "role": "explore",
                    "goal": "定位与用户请求相关的测试文件，并总结现有断言/期望行为",
                    "inputs": [],
                    "acceptance": ["列出相关测试文件与关键断言"],
                    "depends_on": [],
                }
            )

        revised_reason = ""
        if replan_mode:
            prefix = f"r{int(state.get('steering_replans', 1))}-"
            mapping = {
                str(task["id"]): f"{prefix}{task['id']}" for task in tasks
            }
            for task in tasks:
                old_id = str(task["id"])
                task["id"] = mapping[old_id]
                task["depends_on"] = [
                    mapping.get(str(dep), str(dep))
                    for dep in task.get("depends_on") or []
                ]
            cap = max(settings.orchestrator_max_tasks - len(completed_tasks), 1)
            tasks = completed_tasks + tasks[:cap]
            revised_reason = (
                f"用户要求重规划：保留 {len(completed_tasks)} 个已完成任务，"
                f"新增 {len(tasks) - len(completed_tasks)} 个"
            )

        # repo map 随任务下发：worker 不必再从零 grep 目录结构。
        repo_map = str(summary.get("repo_map") or "").strip()
        if repo_map:
            for task in tasks:
                task["repo_map"] = repo_map

        keep_ids = {task["id"] for task in completed_tasks}
        status = {
            task["id"]: (
                existing_status.get(task["id"], "pending")
                if task["id"] in keep_ids
                else "pending"
            )
            for task in tasks
        }
        attempts = dict(state.get("task_attempts") or {})
        return {
            "tasks": tasks,
            "task_status": status,
            "task_attempts": attempts,
            "prefetched_tasks": [],
            "prefetched_degraded": False,
            "orchestrator_degraded": degraded,
            "orchestrator_round": 0,
            "orchestrator_summary": "；".join(errors),
            "steering_replan": False,
            "workflow_revised_reason": revised_reason,
        }

    return plan_tasks
