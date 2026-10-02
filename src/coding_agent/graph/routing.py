"""条件边。"""

from __future__ import annotations

from coding_agent.graph.state import AgentState

TOOLS = "tools"
ADVANCE = "advance"
RESPOND = "respond"


def has_more_steps(state: AgentState) -> bool:
    plan = state.get("plan") or []
    return state.get("step_idx", 0) + 1 < len(plan)


def route_after_act(state: AgentState) -> str:
    """act 之后去哪。

    这里可以直接信任「有 tool_calls 就一定有预算」，因为 act 在超预算时
    不会产出 tool_calls（见 nodes/act.py 的不变量说明），所以不会出现
    待应答的 tool_calls 被丢在历史里的情况。
    """
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return TOOLS
    if has_more_steps(state):
        return ADVANCE
    return RESPOND
