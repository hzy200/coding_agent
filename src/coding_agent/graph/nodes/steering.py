"""Phase 3D：在节点边界消费用户干预。

四个入口分别落在能安全改变控制流的位置（见 docs/HARNESS.md §10）：

- `steering_plan`：任务图刚生成、dispatch 之前；
- `steering_dispatch`：每一批 worker 结束、下一批开始之前；
- `steering_step`：direct 模式一步做完、replan 之前；
- `steering_final`：所有进入 respond 的路径之前 —— 单步任务不经过 advance，
  没有它的话，用户在单步运行中按下的 /cancel 永远不会被处理。

这里**只改控制流**：不执行工具、不碰审批。写操作仍然只能由 act/worker 走
原有的审批与路径守卫。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from coding_agent.config import Settings
from coding_agent.graph.plan import EXPLORE, UNKNOWN, VERIFY, align_kinds
from coding_agent.graph.state import AgentState
from coding_agent.steering import SteeringCommand, SteeringController

# /add 的 role 与 direct 模式 plan kind 的映射：review 是只读审查，
# 在单 agent 计划里按 explore 处理（不要求产生改动）。
_ROLE_TO_KIND = {"explore": EXPLORE, "test": VERIFY, "review": EXPLORE}

_PAUSE_MESSAGE = "运行已暂停；/resume 继续，/cancel 取消。"


def _thread_id(config: RunnableConfig | None) -> str:
    return str((((config or {}).get("configurable")) or {}).get("thread_id", ""))


def _take(controller: SteeringController, thread_id: str) -> list[SteeringCommand]:
    """消费指令；遇到 pause 时回填其它命令并挂起。

    pause 用 `interrupt()` 挂起，恢复时**整个节点会从头重跑**。所以先 consume
    再回填：既不会让同一条 pause 反复触发，也不会丢掉同批的 add/replan。
    """
    commands = controller.consume(thread_id)
    if any(command.action == "pause" for command in commands):
        controller.requeue(
            thread_id,
            [command for command in commands if command.action != "pause"],
        )
        controller.mark_paused(thread_id)
        interrupt({"steering": "pause", "message": _PAUSE_MESSAGE})
    controller.clear_paused(thread_id)
    return commands


def _append_task(
    state: AgentState, command: SteeringCommand
) -> tuple[dict[str, Any], str]:
    """追加一个只读任务；返回 (task, id)。"""
    tasks = [dict(task) for task in (state.get("tasks") or [])]
    used = {str(task.get("id", "")) for task in tasks}
    index = 1
    while f"user-{index}" in used:
        index += 1
    task_id = f"user-{index}"
    task = {
        "id": task_id,
        "role": command.role,
        "goal": command.text or "用户追加的只读任务",
        "inputs": [],
        "acceptance": [],
        "depends_on": [],
        "write_scope": [],
    }
    return task, task_id


def _apply_commands(
    commands: list[SteeringCommand],
    state: AgentState,
    settings: Settings,
    *,
    allow_replan: bool,
    allow_add: bool = True,
    final: bool = False,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把命令翻译成 state 更新与事件载荷。

    返回 `(updates, applied)`；`applied` 由 runtime 翻译成 SteeringApplied。
    """
    updates: dict[str, Any] = {}
    applied: list[dict[str, str]] = []
    orchestrated = state.get("execution_mode") == "orchestrated"

    for command in commands:
        if command.action == "cancel":
            status = dict(state.get("task_status") or {})
            for task_id, value in list(status.items()):
                if value == "pending":
                    status[task_id] = "cancelled"
            updates["steering_cancelled"] = True
            if status:
                updates["task_status"] = status
            applied.append(
                {"action": "cancel", "task_id": "", "detail": "已停止派发后续任务"}
            )

        elif command.action == "add":
            if final:
                applied.append(
                    {
                        "action": "add",
                        "task_id": "",
                        "detail": "任务已进入收尾，追加命令未执行",
                    }
                )
                continue
            if not allow_add:
                applied.append(
                    {
                        "action": "add",
                        "task_id": "",
                        "detail": "当前阶段不支持追加任务",
                    }
                )
                continue
            if orchestrated:
                task, task_id = _append_task(state, command)
                updates["tasks"] = [*(state.get("tasks") or []), task]
                status = dict(updates.get("task_status") or state.get("task_status") or {})
                status[task_id] = "pending"
                updates["task_status"] = status
                applied.append(
                    {
                        "action": "add",
                        "task_id": task_id,
                        "detail": f"已追加只读任务：{task['goal']}",
                    }
                )
            else:
                plan = [*(state.get("plan") or []), command.text or "用户追加的只读任务"]
                kinds = align_kinds(
                    plan,
                    [
                        *(state.get("plan_kinds") or []),
                        _ROLE_TO_KIND.get(command.role, UNKNOWN),
                    ],
                )
                updates["plan"] = plan
                updates["plan_kinds"] = kinds
                applied.append(
                    {
                        "action": "add",
                        "task_id": "",
                        "detail": f"已追加计划步骤：{plan[-1]}",
                    }
                )

        elif command.action == "replan":
            if final:
                applied.append(
                    {
                        "action": "replan",
                        "task_id": "",
                        "detail": "任务已进入收尾，重规划未执行",
                    }
                )
                continue
            if not allow_replan:
                applied.append(
                    {
                        "action": "replan",
                        "task_id": "",
                        "detail": "当前阶段不支持重规划",
                    }
                )
                continue
            used = int(state.get("steering_replans", 0))
            if used >= settings.max_replans:
                applied.append(
                    {
                        "action": "replan",
                        "task_id": "",
                        "detail": f"重规划次数已达上限 {settings.max_replans}",
                    }
                )
                continue
            updates["steering_replan"] = True
            updates["steering_replans"] = used + 1
            applied.append(
                {"action": "replan", "task_id": "", "detail": "剩余工作将重新规划"}
            )

    return updates, applied


def make_steering_plan_node(
    controller: SteeringController, settings: Settings
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """任务图生成后、dispatch 前。这里刚规划完，忽略 replan 请求。"""

    def steering_plan(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        commands = _take(controller, _thread_id(config))
        updates, applied = _apply_commands(
            commands, state, settings, allow_replan=False
        )
        updates["steering_applied"] = applied
        return updates

    return steering_plan


def make_steering_dispatch_node(
    controller: SteeringController, settings: Settings
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """每批 worker 结束、下一批开始前。取消会阻止后续派发。"""

    def steering_dispatch(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        commands = _take(controller, _thread_id(config))
        updates, applied = _apply_commands(
            commands, state, settings, allow_replan=True
        )
        updates["steering_applied"] = applied
        return updates

    return steering_dispatch


def make_steering_step_node(
    controller: SteeringController, settings: Settings
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """direct 模式一步做完、replan 前。"""

    def steering_step(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        commands = _take(controller, _thread_id(config))
        updates, applied = _apply_commands(
            commands, state, settings, allow_replan=True
        )
        updates["steering_applied"] = applied
        return updates

    return steering_step


def make_steering_final_node(
    controller: SteeringController, settings: Settings
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """所有 respond 之前的最后一道消费点。"""

    def steering_final(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        commands = _take(controller, _thread_id(config))
        updates, applied = _apply_commands(
            commands,
            state,
            settings,
            allow_replan=False,
            allow_add=False,
            final=True,
        )
        updates["steering_applied"] = applied
        return updates

    return steering_final
