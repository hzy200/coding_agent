"""编排入口与子图内部条件边。"""

from __future__ import annotations

from typing import Any


def route_entry(state: dict[str, Any]) -> str:
    return "survey" if state.get("execution_mode") == "orchestrated" else "planner"


def route_after_select(state: dict[str, Any]) -> str:
    return (
        "plan_tasks"
        if state.get("workflow")
        in {"parallel_readonly", "orchestrated_implement"}
        else "planner"
    )
