"""条件边。"""

from __future__ import annotations

from collections.abc import Callable

from coding_agent.graph.state import AgentState

ACT = "act"
APPROVE = "approval_gate"
TOOLS = "tools"
VERIFY = "verify"
REVIEW = "review"
NUDGE = "nudge"
REPAIR = "repair"
ADVANCE = "advance"
REPLAN = "replan"
RESPOND = "respond"


def has_more_steps(state: AgentState) -> bool:
    plan = state.get("plan") or []
    return state.get("step_idx", 0) + 1 < len(plan)


def has_current_step(state: AgentState) -> bool:
    """当前 step_idx 是否还指向一个真实存在的步骤。

    与 `has_more_steps` 的差别在**求值时机**：后者用于 advance 之前（"后面还有吗"），
    本函数用于 advance 之后（"现在这一步还在吗"）。replan 可以把剩余步骤全部取消
    —— 工作已经被做完了 —— 这时就该直接去收尾，而不是让 act 去做一个不存在的步骤。
    """
    plan = state.get("plan") or []
    return state.get("step_idx", 0) < len(plan)


def route_after_replan(state: AgentState) -> str:
    """重规划之后去哪。"""
    return ACT if has_current_step(state) else RESPOND


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

        还有预算 → repair（带着结构化错误重来）
        预算耗尽 → replan（换一种做法再试；它也改不动才收尾）

    重试上限是硬性的。没有上限的自动修复会变成一个烧 token 的无限循环，
    而且用户永远拿不到「我试过了但没修好」这个结论。replan 同样有硬性上限，
    所以「换做法 → 又失败」也不会无限循环下去。

    验证**通过之后**进 review 而不是直接 advance：测试通过 ≠ 代码正确，
    第二道关在 `nodes/review.py`。

    **这一跳只看验证，不看审查**（反过来 review 那一跳也只看审查）。
    两边都看会出一个很隐蔽的错：`repair` 刻意不清 `review`（act 要靠它知道该改
    什么），于是"验证失败 → repair → 再验证通过"这条路上，上一轮的审查阻断还
    留在 state 里 —— 把它算进来的话，验证刚通过就被直接打回 repair，
    **审查再也不会重跑**，等于上一轮修复有没有解决问题没人确认。
    """

    def route_after_verify(state: AgentState) -> str:
        if (state.get("verification") or {}).get("status") == "failed":
            if state.get("retry", 0) < max_repair_rounds:
                return REPAIR
            # 修不动了，但还没到放弃的时候：先让 replan 看看能不能换个做法。
            # 它若也没辙，会把剩余步骤砍掉，再由 route_after_replan 送去收尾。
            return REPLAN
        # 工具轮次耗尽、且这一步一个改动都没产生：多半是卡住了。
        # 不静默跳到下一步，停下来让收尾如实说明，而不是假装这步已完成。
        if state.get("budget_exhausted") and not state.get("dirty"):
            return RESPOND
        return REVIEW

    return route_after_verify


def make_route_after_review(
    max_repair_rounds: int, *, nudge_empty_steps: bool = True
) -> Callable[[AgentState], str]:
    """审查之后去哪。

    审查的阻断与验证的失败走**同一条出口**（repair，额度耗尽则 replan）：
    对这一步来说它们的含义相同 —— 东西做出来了，但不合格。

    `nudge_empty_steps` 关掉时行为与本轮之前完全一致（给现有基线留一条可比的路）。
    """

    def route_after_review(state: AgentState) -> str:
        if (state.get("review") or {}).get("status") == "blocked":
            if state.get("retry", 0) < max_repair_rounds:
                return REPAIR
            return REPLAN
        # 这一步**一个改动都没产生** —— 而 verify 与 review 都只在 `dirty` 时才跑，
        # 所以两者都短路通过，「只读了文件」和「做完了」在图上长得一样。
        # 给它一次重做的机会（见 nodes/nudge.py），上限一次。
        if nudge_empty_steps and not state.get("dirty") and not state.get("empty_step_nudged"):
            return NUDGE
        return ADVANCE if has_more_steps(state) else RESPOND

    return route_after_review
