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
        "empty_step_nudged": False,
        "verification": {},
        # 两道质量关的结果都按步清空：上一步的失败不该影响下一步的判断。
        # **review_watermark 不在这里清** —— 它跨步骤保留、只在整轮开始时归零，
        # 否则每一步都会重审前面所有步骤的改动（见 graph/state.py 的说明）。
        "review": {},
        # 修复预算按步计算，逐步重置
        "retry": 0,
    }
