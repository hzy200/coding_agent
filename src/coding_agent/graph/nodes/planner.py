"""planner 节点：把用户请求拆成有序子任务。

结构化输出走 pydantic 校验 + 重试；即使模型反复给不出合法结果，也降级为
「整个请求当成一步」，保证图不会因为规划失败而中断。

**降级必须可见。** 早先的实现把降级伪装成一次正常规划，于是多步工作流悄悄
退化成单步，而 `PlanCreated` 看上去完全正常 —— 只有翻审计才发现每次的计划
都是请求原文（见 llm/deepseek.py 里关于 json_mode 的注释）。
因此降级时写 `plan_degraded=True`，由事件层与前端明确告知。

与 act / respond 一致，这里是 **async 节点**（`ainvoke`），理由同上：
同步调用在异步图里会被丢进线程池，取消信号传不进去。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import with_json_output
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
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    structured = with_json_output(llm, TaskPlan).with_retry(stop_after_attempt=3)
    prompt = PLANNER_PROMPT.format(
        max_steps=max_steps,
        permission_note=PLANNER_PERMISSION_WRITE if allow_write else PLANNER_PERMISSION_READONLY,
    )

    async def plan(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        request = last_human_text(state["messages"])
        degraded = False
        try:
            result = await structured.ainvoke(
                [SystemMessage(content=prompt), HumanMessage(content=request)],
                config,
            )
            steps = [s.strip() for s in (result.steps or []) if s.strip()][:max_steps]
        except Exception:  # noqa: BLE001 - 规划失败不能拖垮整张图
            steps = []
            degraded = True

        if not steps:
            steps = [request or "完成用户请求"]
            # 模型给了合法 JSON 但 steps 为空，同样是「没规划出来」，
            # 与抛异常走的是同一条降级路径，也要如实标记。
            degraded = True

        return {
            "plan": steps,
            "plan_degraded": degraded,
            "step_idx": 0,
            # 两个重规划额度都是整个任务的，开局归零
            "replan_count": 0,
            "tweak_count": 0,
            "tool_rounds": 0,
            "budget_exhausted": False,
            "dirty": False,
            "verification": {},
            "review": {},
            # 水位线是**整轮**的：新的一轮开始，之前审过的留底都不算数了。
            # 只有这里归零，advance / repair 都不许碰它（详见 graph/state.py）。
            "review_watermark": "",
            "retry": 0,
        }

    return plan
