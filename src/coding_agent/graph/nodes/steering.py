"""Phase 3D：在节点边界消费用户干预。

四个入口分别落在能安全改变控制流的位置（见 docs/HARNESS.md §10）：

- `steering_plan`：任务图刚生成、dispatch 之前；
- `steering_dispatch`：每一批 worker 结束、下一批开始之前；
- `steering_step`：direct 模式一步做完、replan 之前；
- `steering_cancel`：direct 模式每轮 act/tools 之间 —— 只消费 cancel，
  让长步骤里的取消不必等整步跑完；add/replan/pause 仍留给语义边界；
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


def _take(
    controller: SteeringController,
    thread_id: str,
    *,
    allow_pause: bool = True,
) -> list[SteeringCommand]:
    """消费指令；遇到 pause 时回填其它命令并挂起。

    pause 用 `interrupt()` 挂起，恢复时**整个节点会从头重跑**。所以先 consume
    再回填：既不会让同一条 pause 反复触发，也不会丢掉同批的 add/replan。

    `allow_pause=False` 用于「取消已经成立」的收尾路径：此时再挂起只会让用户
    取消完又停在半路，pause 直接消费掉、不再 interrupt。
    """
    commands = controller.consume(thread_id)
    if allow_pause and any(
        command.action == "pause" for command in commands
    ):
        controller.requeue(
            thread_id,
            [command for command in commands if command.action != "pause"],
        )
        controller.mark_paused(thread_id)
        interrupt({"steering": "pause", "message": _PAUSE_MESSAGE})
    else:
        # 取消优先：指令已消费，但不再挂起；pause 不应出现在后续载荷里。
        commands = [command for command in commands if command.action != "pause"]
    controller.clear_paused(thread_id)
    return commands


def _append_task(
    tasks: list[dict[str, Any]],
    status: dict[str, str],
    command: SteeringCommand,
) -> tuple[dict[str, Any], str]:
    """把只读任务追加进**本批正在累积的** tasks/status，返回 (task, id)。

    必须基于累积后的列表分配 id：同一批到达的多条 /add 若都从原始 state
    取 id，会全部拿到 `user-1`，后写覆盖先写。
    """
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
    tasks.append(task)
    status[task_id] = "pending"
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

    **同批指令必须基于累积结果处理**：add 追加到同一份 tasks/plan，
    否则两条 /add 会互相覆盖（都从原始 state 重建）。**cancel 是终态**：
    一旦本批出现 cancel（或 state 已取消），其余指令一律不执行 —— 否则
    "先取消、再追加" 会让新任务在取消之后仍被派发。
    """
    updates: dict[str, Any] = {}
    applied: list[dict[str, str]] = []
    orchestrated = state.get("execution_mode") == "orchestrated"
    has_cancel = any(command.action == "cancel" for command in commands)
    already_cancelled = bool(state.get("steering_cancelled"))

    # 取消一旦成立，本批不允许再产生任何可执行更新。already_cancelled 时也
    # 不走正常路径：路由已经不会再派发，再报"已追加"只会误导用户。
    if has_cancel or already_cancelled:
        status = dict(state.get("task_status") or {})
        if has_cancel:
            for task_id, value in list(status.items()):
                if value == "pending":
                    status[task_id] = "cancelled"
            updates["steering_cancelled"] = True
            # 同批若还带了 replan，也不该再触发一次重规划。
            updates["steering_replan"] = False
            applied.append(
                {"action": "cancel", "task_id": "", "detail": "已停止派发后续任务"}
            )
        if status:
            updates["task_status"] = status
        for command in commands:
            if command.action == "cancel":
                continue
            applied.append(
                {
                    "action": command.action,
                    "task_id": "",
                    "detail": "任务已取消，指令未执行",
                }
            )
        return updates, applied

    tasks = [dict(task) for task in (state.get("tasks") or [])]
    status = dict(state.get("task_status") or {})
    plan = [str(step) for step in (state.get("plan") or [])]
    # 先把 kinds 对齐到当前 plan，再逐条追加，避免长度不齐时新 kind 落到
    # 填充位上。
    plan_kinds = align_kinds(plan, state.get("plan_kinds"))
    added_task = False
    added_step = False
    replan_selected = False
    used_replans = int(state.get("steering_replans", 0))

    for command in commands:
        if command.action == "add":
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
                task, task_id = _append_task(tasks, status, command)
                added_task = True
                applied.append(
                    {
                        "action": "add",
                        "task_id": task_id,
                        "detail": f"已追加只读任务：{task['goal']}",
                    }
                )
            else:
                goal = command.text or "用户追加的只读任务"
                plan.append(goal)
                plan_kinds.append(_ROLE_TO_KIND.get(command.role, UNKNOWN))
                added_step = True
                applied.append(
                    {
                        "action": "add",
                        "task_id": "",
                        "detail": f"已追加计划步骤：{goal}",
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
            if replan_selected:
                # 同批多条 /replan 只需要执行一次，不能重复消耗额度。
                applied.append(
                    {
                        "action": "replan",
                        "task_id": "",
                        "detail": "已包含在本次重规划中",
                    }
                )
                continue
            if used_replans >= settings.max_replans:
                applied.append(
                    {
                        "action": "replan",
                        "task_id": "",
                        "detail": f"重规划次数已达上限 {settings.max_replans}",
                    }
                )
                continue
            replan_selected = True
            updates["steering_replan"] = True
            updates["steering_replans"] = used_replans + 1
            applied.append(
                {"action": "replan", "task_id": "", "detail": "剩余工作将重新规划"}
            )

    if orchestrated and added_task:
        updates["tasks"] = tasks
        updates["task_status"] = status
    elif not orchestrated and added_step:
        updates["plan"] = plan
        updates["plan_kinds"] = plan_kinds

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


def make_steering_cancel_node(
    controller: SteeringController, settings: Settings
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """act/tools 之间的轻量取消检查点。

    direct 模式一个 plan step 内部可能循环很多轮 `act → tools`，而
    `steering_step` 只在整步结束后才消费指令。这里只处理 cancel：一旦看到
    取消就立即停止本步（不再执行后续工具、也不再调用模型）；add/replan 留给
    下一个语义边界，pause 直接丢弃（取消优先，避免取消后又在收尾前挂起）。

    没有 cancel 时只做一次 `peek`，不产生 state 更新，因此不改变原有
    add/replan 的生效时点。
    """

    def steering_cancel(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        thread_id = _thread_id(config)
        queued = controller.peek(thread_id)
        if not any(command.action == "cancel" for command in queued):
            return {}

        # 先 peek 再 consume：没有 cancel 的高频路径不做文件锁/写盘。
        commands = controller.consume(thread_id)
        keep = [
            command
            for command in commands
            if command.action not in {"cancel", "pause"}
        ]
        controller.requeue(thread_id, keep)
        cancels = [
            command for command in commands if command.action == "cancel"
        ]
        updates, applied = _apply_commands(
            cancels,
            state,
            settings,
            allow_replan=False,
            allow_add=False,
        )
        updates["steering_applied"] = applied
        return updates

    return steering_cancel


def make_steering_final_node(
    controller: SteeringController, settings: Settings
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """所有 respond 之前的最后一道消费点。"""

    def steering_final(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        # 已经取消的收尾不再接受 pause：用户取消后不该又停在半路。
        commands = _take(
            controller,
            _thread_id(config),
            allow_pause=not bool(state.get("steering_cancelled")),
        )
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
