"""planner 节点：把用户请求拆成有序子任务。

结构化输出走 pydantic 校验 + 重试；即使模型反复给不出合法结果，也降级为
「整个请求当成一步」，保证图不会因为规划失败而中断。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from coding_agent.graph.state import AgentState
from coding_agent.llm.prompts import (
    PLANNER_PERMISSION_READONLY,
    PLANNER_PERMISSION_WRITE,
    PLANNER_PROMPT,
)
from coding_agent.messages import last_human_text


class TaskPlan(BaseModel):
    """规划结果。"""

    steps: list[str] = Field(
        default_factory=list,
        description="有序子任务列表，每项一句话，描述做什么以及如何算完成",
    )


def make_planner_node(
    llm: BaseChatModel,
    *,
    max_steps: int,
    allow_write: bool,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    structured = llm.with_structured_output(TaskPlan).with_retry(stop_after_attempt=3)
    prompt = PLANNER_PROMPT.format(
        max_steps=max_steps,
        permission_note=PLANNER_PERMISSION_WRITE if allow_write else PLANNER_PERMISSION_READONLY,
    )

    def plan(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        request = last_human_text(state["messages"])
        try:
            result = structured.invoke(
                [SystemMessage(content=prompt), HumanMessage(content=request)],
                config,
            )
            steps = [s.strip() for s in (result.steps or []) if s.strip()][:max_steps]
        except Exception:  # noqa: BLE001 - 规划失败不能拖垮整张图
            steps = []

        if not steps:
            steps = [request or "完成用户请求"]

        return {
            "plan": steps,
            "step_idx": 0,
            "tool_rounds": 0,
            "budget_exhausted": False,
            "dirty": False,
            "verification": {},
            "retry": 0,
        }

    return plan
