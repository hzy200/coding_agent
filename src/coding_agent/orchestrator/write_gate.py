"""派发写 worker 前的一次性计划审批。

并发区**不能**弹审批：多个 implement worker 同时 interrupt 会让恢复语义
纠缠不清。这里在 scheduler 启动前把整批 `write_scope` 一次性展示给用户，
批准/拒绝后再进入并发执行。

默认关闭；`AGENT_ORCHESTRATOR_WRITE_PLAN_APPROVAL=true` 时启用。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from coding_agent.config import Settings
from coding_agent.graph.state import AgentState

WRITE_GATE = "write_gate"


def make_write_gate_node(
    settings: Settings,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    def write_gate(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        if not settings.orchestrator_write_plan_approval:
            return {}
        if state.get("workflow") != "orchestrated_implement":
            return {}
        status = dict(state.get("task_status") or {})
        implement = [
            task
            for task in (state.get("tasks") or [])
            if task.get("role") == "implement"
            and status.get(str(task.get("id")), "pending") == "pending"
        ]
        if not implement:
            return {}

        pending = [
            {
                "call_id": f"plan:{task.get('id', '')}",
                "tool": "implement_worker",
                "command": "write_scope: "
                + ", ".join(str(s) for s in (task.get("write_scope") or [])),
                "reason": str(task.get("goal", "")),
                "level": "L1 低风险写",
                "level_int": 1,
            }
            for task in implement
        ]
        answers = interrupt({"requests": pending})
        if not isinstance(answers, dict):
            answers = {}

        # fail closed：缺失/无法解析的答复按拒绝处理。
        for task in implement:
            task_id = str(task.get("id", ""))
            if not answers.get(f"plan:{task_id}"):
                status[task_id] = "cancelled"
        return {"task_status": status}

    return write_gate
