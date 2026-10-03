"""advance 节点：把指针推进到下一个子任务，并重置该步的工具轮次预算。"""

from __future__ import annotations

from typing import Any

from coding_agent.graph.state import AgentState


def advance(state: AgentState) -> dict[str, Any]:
    return {
        "step_idx": state.get("step_idx", 0) + 1,
        "tool_rounds": 0,
        "budget_exhausted": False,
        # 每步的「改过东西没」独立计算，否则一步改过之后每步都会跑验证
        "dirty": False,
        "verification": {},
        # 修复预算按步计算，逐步重置
        "retry": 0,
    }
