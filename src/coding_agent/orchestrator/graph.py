"""任务图校验与 ready 队列。

校验失败**不抛异常**：编排器降级为单个 explore 任务，并让事件/审计把
`degraded` 显示出来。模型给出的图不可信，不能让它拖垮整次运行。
"""

from __future__ import annotations

import posixpath
from typing import Any

from coding_agent.orchestrator.contracts import TaskSpec

ALLOWED_ROLES = frozenset({"explore", "test", "review", "implement"})


def default_task(request: str) -> dict[str, Any]:
    return {
        "id": "explore-1",
        "role": "explore",
        "goal": request or "理解当前工作区",
        "inputs": [],
        "acceptance": [],
        "depends_on": [],
    }


def _has_cycle(tasks: list[dict[str, Any]]) -> bool:
    by_id = {task["id"]: task for task in tasks}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dep in by_id[node].get("depends_on") or []:
            if dep in by_id and visit(dep):
                return True
        visiting.remove(node)
        done.add(node)
        return False

    return any(visit(task_id) for task_id in by_id)


def _normalize_scope(scope: str) -> str:
    text = str(scope).strip().replace("\\", "/")
    if not text or text.startswith("/") or ".." in text.split("/"):
        return ""
    normalized = posixpath.normpath(text)
    return "" if normalized in {"", "."} else normalized


def _scope_contains(path: str, scope: str) -> bool:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    return normalized == scope or normalized.startswith(scope.rstrip("/") + "/")


def _scopes_overlap(left: str, right: str) -> bool:
    return _scope_contains(left, right) or _scope_contains(right, left)


def validate_tasks(
    raw: list[TaskSpec | dict[str, Any]] | None,
    *,
    request: str,
    max_tasks: int,
    allow_write: bool = False,
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """把模型输出归一化为任务列表。

    返回 `(tasks, degraded, errors)`。任何结构问题都退化为单 explore 任务，
    不做部分采纳 —— 半张图比明确的降级更难排查。
    """
    tasks: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, TaskSpec):
            tasks.append(item.model_dump())
        elif isinstance(item, dict):
            tasks.append(dict(item))
    errors: list[str] = []
    if not tasks:
        errors.append("任务图为空")
    # **先检查原始长度再截断**：先截断的话这个分支永远不可能触发，
    # 超限的任务图会被静默丢弃一部分。
    if len(tasks) > max_tasks:
        errors.append(f"任务数 {len(tasks)} 超过上限 {max_tasks}")
    tasks = tasks[: max(max_tasks, 1)]

    ids: list[str] = []
    for task in tasks:
        task_id = str(task.get("id", "")).strip()
        goal = str(task.get("goal", "")).strip()
        role = str(task.get("role", "explore"))
        if not task_id:
            errors.append("任务 id 为空")
        if task_id in ids:
            errors.append(f"任务 id 重复：{task_id}")
        ids.append(task_id)
        if not goal:
            errors.append(f"任务 {task_id or '?'} 没有 goal")
        if role not in ALLOWED_ROLES:
            errors.append(f"任务 {task_id or '?'} 的角色不允许：{role}")
        if role == "implement" and not allow_write:
            errors.append(f"任务 {task_id or '?'} 是 implement，但当前未启用写编排")
        task["id"] = task_id
        task["goal"] = goal
        task["role"] = role
        task["inputs"] = [str(v) for v in (task.get("inputs") or [])]
        task["acceptance"] = [str(v) for v in (task.get("acceptance") or [])]
        task["depends_on"] = [str(v) for v in (task.get("depends_on") or [])]
        mode = str(task.get("depends_mode", "full")).strip().lower()
        if mode not in {"full", "any"}:
            errors.append(
                f"任务 {task_id or '?'} 的 depends_mode 非法：{mode}"
            )
            mode = "full"
        if role == "implement" and mode != "full":
            errors.append(
                f"任务 {task_id or '?'} 是 implement，depends_mode 必须是 full"
            )
            mode = "full"
        task["depends_mode"] = mode
        raw_scope = [str(v) for v in (task.get("write_scope") or [])]
        scope = [_normalize_scope(item) for item in raw_scope]
        scope = [item for item in scope if item]
        task["write_scope"] = scope
        if role == "implement":
            if not scope:
                errors.append(f"任务 {task_id or '?'} 是 implement，但没有 write_scope")
        elif scope:
            errors.append(f"任务 {task_id or '?'} 是只读角色，却声明了 write_scope")

    known = set(ids)
    for task in tasks:
        deps = task.get("depends_on") or []
        for dep in deps:
            if dep == task["id"]:
                errors.append(f"任务 {task['id']} 依赖自己")
            elif dep not in known:
                errors.append(f"任务 {task['id']} 依赖不存在的 {dep}")

    # implement 任务必须互相独立：它们的 worktree 都基于同一个 HEAD，
    # 若一个依赖另一个，后者的 worktree 看不到前者的改动。
    by_id = {task["id"]: task for task in tasks}
    for task in tasks:
        if task["role"] != "implement":
            continue
        for dep in task.get("depends_on") or []:
            if by_id.get(dep, {}).get("role") == "implement":
                errors.append(
                    f"任务 {task['id']} 依赖另一个 implement {dep}；"
                    "写任务必须独立，不能互相依赖"
                )

    # 并发写范围必须两两不重叠，否则合并会冲突。
    implement = [task for task in tasks if task["role"] == "implement"]
    for index, left in enumerate(implement):
        for right in implement[index + 1 :]:
            for left_scope in left.get("write_scope") or []:
                for right_scope in right.get("write_scope") or []:
                    if _scopes_overlap(left_scope, right_scope):
                        errors.append(
                            f"写范围重叠：{left['id']} {left_scope} 与 "
                            f"{right['id']} {right_scope}"
                        )

    if not errors and _has_cycle(tasks):
        errors.append("任务图存在环")

    if errors:
        return [default_task(request)], True, errors
    return tasks, False, []


_PROPOSAL_ROLES = frozenset({"explore", "test", "review"})


def _goal_key(text: Any) -> str:
    return " ".join(str(text).strip().lower().split())


def validate_proposals(
    raw: list[dict[str, Any]] | None,
    *,
    existing: list[dict[str, Any]],
    max_total: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把 worker 提议的只读任务归一化为可调度任务。

    worker 只拿到自己的任务，不知道整张图，所以这里必须重新做 host 侧边界：
    只读角色、非空 goal、去重、依赖存在、总量上限、无环。任何不合规的提议
    丢弃并记录原因，**不让 worker 的自由文本直接影响写路径**。
    """
    ids = {str(task.get("id", "")) for task in existing}
    goals = {_goal_key(task.get("goal", "")) for task in existing}
    errors: list[str] = []
    accepted: list[dict[str, Any]] = []
    max_new = max(int(max_total) - len(existing), 0)

    for item in raw or []:
        if len(accepted) >= max_new:
            errors.append(
                f"追加任务达到总上限 {max_total}，其余提议已忽略"
            )
            break
        role = str(item.get("role", "explore")).strip().lower()
        goal = str(item.get("goal", "")).strip()
        if role not in _PROPOSAL_ROLES:
            errors.append(f"拒绝非只读提议（role={role}）：{goal}")
            continue
        if not goal:
            errors.append("拒绝空 goal 的任务提议")
            continue
        key = _goal_key(goal)
        if key in goals:
            errors.append(f"重复任务提议已忽略：{goal}")
            continue
        if item.get("write_scope"):
            errors.append(f"只读提议不允许 write_scope：{goal}")
            continue
        deps = [
            str(dep).strip()
            for dep in (item.get("depends_on") or [])
            if str(dep).strip()
        ]
        unknown = [dep for dep in deps if dep not in ids]
        if unknown:
            errors.append(
                f"任务提议依赖不存在的 id {unknown}，已忽略：{goal}"
            )
            continue

        index = 1
        while f"ext-{index}" in ids:
            index += 1
        task_id = f"ext-{index}"
        accepted.append(
            {
                "id": task_id,
                "role": role,
                "goal": goal,
                "inputs": [str(v) for v in (item.get("inputs") or [])],
                "acceptance": [
                    str(v) for v in (item.get("acceptance") or [])
                ],
                "depends_on": deps,
                "depends_mode": "full",
                "write_scope": [],
            }
        )
        ids.add(task_id)
        goals.add(key)

    if accepted and _has_cycle(existing + accepted):
        return [], errors + ["追加任务引入了依赖环，全部丢弃"]
    return accepted, errors


def _dependency_met(task: dict[str, Any], dep: str, status: dict[str, str]) -> bool:
    """单个依赖是否已满足。

    `depends_mode=any` 允许消费 partial（例如 review 只需要 implement 的产出，
    不需要它通过完整验证）；failed/blocked/cancelled 任何模式都不满足。
    """
    value = status.get(dep)
    if value == "success":
        return True
    return value == "partial" and task.get("depends_mode") == "any"


def ready_tasks(
    tasks: list[dict[str, Any]], status: dict[str, str]
) -> list[dict[str, Any]]:
    """依赖已满足的 pending 任务。"""
    ready = []
    for task in tasks:
        if status.get(task["id"], "pending") != "pending":
            continue
        deps = task.get("depends_on") or []
        if all(_dependency_met(task, dep, status) for dep in deps):
            ready.append(task)
    return ready


def mark_blocked(
    tasks: list[dict[str, Any]], status: dict[str, str]
) -> dict[str, str]:
    """把依赖 failed/blocked 的 pending 任务标成 blocked（原地返回新 dict）。"""
    updated = dict(status)
    changed = True
    while changed:
        changed = False
        for task in tasks:
            task_id = task["id"]
            if updated.get(task_id, "pending") != "pending":
                continue
            deps = task.get("depends_on") or []
            # failed/blocked/cancelled 任何模式都阻塞；partial 只阻塞
            # depends_mode=full 的任务，review/test 可用 any 继续消费产出。
            mode = str(task.get("depends_mode") or "full")
            if any(
                updated.get(dep) in {"failed", "blocked", "cancelled"}
                or (updated.get(dep) == "partial" and mode != "any")
                for dep in deps
            ):
                updated[task_id] = "blocked"
                changed = True
    return updated
