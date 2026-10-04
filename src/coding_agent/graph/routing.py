"""条件边。"""

from __future__ import annotations

from collections.abc import Callable

from coding_agent.graph.state import AgentState

APPROVE = "approval_gate"
TOOLS = "tools"
VERIFY = "verify"
REPAIR = "repair"
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

    有工具调用时先进 approval_gate 而不是直接进 tools —— 审批是工具执行的
    前置关卡，任何调用都绕不过去。一步做完了则先过 verify。
    """
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return APPROVE
    return VERIFY


def make_route_after_verify(max_repair_rounds: int) -> Callable[[AgentState], str]:
    """验证之后去哪。

    失败时的分支就是「失败驱动的修复循环」：
    还有预算 → repair（带着结构化错误重来）；预算耗尽 → respond（如实上报）。

    重试上限是硬性的。没有上限的自动修复会变成一个烧 token 的无限循环，
    而且用户永远拿不到「我试过了但没修好」这个结论。
    """

    def route_after_verify(state: AgentState) -> str:
        verification = state.get("verification") or {}
        if verification.get("status") == "failed":
            if state.get("retry", 0) < max_repair_rounds:
                return REPAIR
            return RESPOND
        # 工具轮次耗尽、且这一步一个改动都没产生：多半是卡住了。
        # 不静默跳到下一步，停下来让收尾如实说明，而不是假装这步已完成。
        if state.get("budget_exhausted") and not state.get("dirty"):
            return RESPOND
        return ADVANCE if has_more_steps(state) else RESPOND

    return route_after_verify
