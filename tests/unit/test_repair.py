"""失败驱动的修复循环。

图级测试用真实的 StateGraph 跑一遍：只有把 act → verify → repair → act 这条环
真的连起来跑，才能证明它会收敛、且到上限就停。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from coding_agent.graph.build import estimate_recursion_limit
from coding_agent.graph.nodes import advance
from coding_agent.graph.nodes.repair import repair
from coding_agent.graph.routing import (
    ADVANCE,
    APPROVE,
    REPAIR,
    REPLAN,
    RESPOND,
    REVIEW,
    TOOLS,
    VERIFY,
    make_route_after_review,
    make_route_after_verify,
    route_after_act,
)
from coding_agent.graph.state import AgentState
from coding_agent.llm.prompts import format_verification_feedback

FAILED = {
    "status": "failed",
    "command": "python3 -m unittest discover",
    "summary": "FAILED (failures=1)",
    "issues": [{"location": "calc.py:2", "message": "assertEqual"}],
}
PASSED = {"status": "ok", "command": "python3 -m unittest discover", "summary": "OK"}


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------- repair 节点 ----------------

def test_repair_increments_attempt() -> None:
    assert repair({"retry": 0})["retry"] == 1
    assert repair({"retry": 2})["retry"] == 3


def test_repair_gives_a_fresh_tool_budget() -> None:
    """上一步的轮次可能已经用光，不给新预算模型还没改就被 act 掐掉了。"""
    out = repair({"retry": 0, "tool_rounds": 12, "budget_exhausted": True})
    assert out["tool_rounds"] == 0
    assert out["budget_exhausted"] is False


def test_repair_keeps_the_step_dirty() -> None:
    """修完必须重新验证，否则「改坏了但没人发现」。"""
    assert repair({"retry": 0, "dirty": False})["dirty"] is True


def test_repair_missing_counter_starts_from_zero() -> None:
    assert repair({})["retry"] == 1


# ---------------- 回灌文本 ----------------

def test_feedback_includes_command_summary_and_locations() -> None:
    text = format_verification_feedback(FAILED, attempt=1, limit=3)
    assert "python3 -m unittest discover" in text
    assert "FAILED (failures=1)" in text
    assert "calc.py:2" in text
    assert "第 1/3 次" in text


def test_feedback_warns_on_last_attempt() -> None:
    text = format_verification_feedback(FAILED, attempt=3, limit=3)
    assert "最后一次修复机会" in text
    assert "最后一次" not in format_verification_feedback(FAILED, attempt=1, limit=3)


def test_feedback_falls_back_to_raw_output() -> None:
    """没有结构化 issue 时退回原始输出末尾，而不是什么都不给。"""
    raw = {"status": "failed", "command": "make", "output_tail": "make: *** No rule"}
    text = format_verification_feedback(raw, attempt=1, limit=2)
    assert "No rule" in text


def test_feedback_handles_completely_empty_verification() -> None:
    text = format_verification_feedback({}, attempt=1, limit=1)
    assert "第 1/1 次" in text  # 不能崩，也不能给空串


# ---------------- 图级：循环收敛与上限 ----------------

class _FlakyVerifier:
    """前 N 次失败，之后通过。"""

    def __init__(self, fail_times: int) -> None:
        self.remaining = fail_times
        self.calls = 0

    def __call__(self, state: AgentState, config) -> dict:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return {"verification": dict(FAILED)}
        return {"verification": dict(PASSED)}


class _RecordingAct:
    """记录每次被调用时看到的 retry 计数与验证状态。"""

    def __init__(self) -> None:
        self.seen: list[tuple[int, str]] = []

    def __call__(self, state: AgentState, config) -> dict:
        verification = state.get("verification") or {}
        self.seen.append((state.get("retry", 0), verification.get("status", "")))
        return {"messages": [AIMessage(content=f"attempt {len(self.seen)}")]}


def _review_pass(state: AgentState, config) -> dict:
    """审查通过的空实现。

    迷你图要跟真实拓扑一致（`VERIFY → REVIEW → ADVANCE`）：少接这一跳，
    `route_after_verify` 返回的 REVIEW 就不在条件边的映射里，图会直接报错 ——
    而这正是本题要覆盖的"两道质量关串联"的结构。
    """
    return {"review": {}}


def _loop_graph(verifier, act, limit: int):
    graph = StateGraph(AgentState)
    graph.add_node("act", act)
    graph.add_node(VERIFY, verifier)
    graph.add_node(REVIEW, _review_pass)
    graph.add_node(REPAIR, repair)
    # 修不动了就进 replan。真节点会问模型「换个做法行不行」，这里只关心
    # 修复循环的收敛与上限，所以用最简单的等价行为（放弃）代替。
    graph.add_node(
        REPLAN,
        lambda state: {"plan": (state.get("plan") or [])[: state.get("step_idx", 0)]},
    )
    graph.add_node("respond", lambda state: {"messages": [AIMessage(content="done")]})

    graph.add_edge(START, "act")
    graph.add_conditional_edges("act", route_after_act, {VERIFY: VERIFY})
    graph.add_conditional_edges(
        VERIFY,
        make_route_after_verify(limit),
        {REPAIR: REPAIR, REPLAN: REPLAN, REVIEW: REVIEW, RESPOND: "respond"},
    )
    graph.add_conditional_edges(
        REVIEW,
        make_route_after_review(limit),
        {REPAIR: REPAIR, REPLAN: REPLAN, RESPOND: "respond"},
    )
    graph.add_edge(REPAIR, "act")
    graph.add_edge(REPLAN, "respond")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=MemorySaver())


def _initial() -> dict:
    return {
        "messages": [HumanMessage(content="修好它")],
        "dirty": True,
        "retry": 0,
        "verification": {},
    }


CONFIG = {"configurable": {"thread_id": "t"}}


def test_loop_converges_after_one_repair() -> None:
    async def scenario() -> None:
        verifier = _FlakyVerifier(fail_times=1)
        act = _RecordingAct()
        app = _loop_graph(verifier, act, limit=3)

        result = await app.ainvoke(_initial(), CONFIG)

        assert result["retry"] == 1
        assert result["verification"]["status"] == "ok"
        assert verifier.calls == 2  # 失败一次 + 修复后通过
        assert len(act.seen) == 2

    _run(scenario())


def test_act_sees_the_failure_on_the_second_attempt() -> None:
    """第一次 act 时没有验证结果，修复那次必须看到 failed。"""

    async def scenario() -> None:
        verifier = _FlakyVerifier(fail_times=1)
        act = _RecordingAct()
        app = _loop_graph(verifier, act, limit=3)

        await app.ainvoke(_initial(), CONFIG)

        assert act.seen[0] == (0, "")
        assert act.seen[1] == (1, "failed")

    _run(scenario())


def test_loop_stops_at_the_limit() -> None:
    """一直修不好就必须停 —— 没有上限的自动修复是烧 token 的无底洞。"""

    async def scenario() -> None:
        verifier = _FlakyVerifier(fail_times=99)
        act = _RecordingAct()
        app = _loop_graph(verifier, act, limit=3)

        result = await app.ainvoke(_initial(), CONFIG)

        assert result["retry"] == 3
        assert result["verification"]["status"] == "failed"
        assert len(act.seen) == 4  # 初次 + 3 次修复
        assert verifier.calls == 4

    _run(scenario())


def test_zero_limit_skips_repair_entirely() -> None:
    async def scenario() -> None:
        verifier = _FlakyVerifier(fail_times=99)
        act = _RecordingAct()
        app = _loop_graph(verifier, act, limit=0)

        result = await app.ainvoke(_initial(), CONFIG)

        assert result["retry"] == 0
        assert verifier.calls == 1
        assert len(act.seen) == 1

    _run(scenario())


def test_loop_does_not_run_when_verification_passes() -> None:
    async def scenario() -> None:
        verifier = _FlakyVerifier(fail_times=0)
        act = _RecordingAct()
        app = _loop_graph(verifier, act, limit=3)

        result = await app.ainvoke(_initial(), CONFIG)

        assert result["retry"] == 0
        assert verifier.calls == 1
        assert len(act.seen) == 1

    _run(scenario())


def test_each_retry_gets_a_fresh_tool_budget() -> None:
    """否则第二次 act 会因为上一轮轮次用光而直接放弃。"""

    async def scenario() -> None:
        verifier = _FlakyVerifier(fail_times=2)
        app = _loop_graph(verifier, _RecordingAct(), limit=3)

        result = await app.ainvoke(_initial(), CONFIG)

        assert result["verification"]["status"] == "ok"
        assert result["tool_rounds"] == 0  # repair 每轮都清零
        assert result["budget_exhausted"] is False

    _run(scenario())


# ---------------- 图级：最坏路径不能撞 recursion_limit ----------------
#
# 守的是一个真实缺陷：repair 会把 tool_rounds 清零，于是**每一轮修复都重新吃满
# 一个完整周期**，而 estimate_recursion_limit 早期只按单周期推导。结果是「反复失败
# 且每轮都把工具预算用光」的任务会以 GraphRecursionError 收场，拿不到 respond
# 「我试过了但没修好」的收尾 —— 恰好打掉修复循环的结论价值。


class _BudgetBurningAct:
    """每轮都产出一个工具调用，直到本步的轮次预算耗尽 —— 最坏路径的形态。"""

    def __init__(self, max_rounds: int) -> None:
        self.max_rounds = max_rounds
        self.calls = 0

    def __call__(self, state: AgentState, config) -> dict:
        self.calls += 1
        rounds = state.get("tool_rounds", 0)
        if rounds >= self.max_rounds:
            return {
                "messages": [AIMessage(content="预算用尽")],
                "budget_exhausted": True,
            }
        return {
            "messages": [
                AIMessage(
                    content="继续",
                    tool_calls=[{"name": "noop", "args": {}, "id": f"c{self.calls}"}],
                )
            ],
            "tool_rounds": rounds + 1,
        }


def _burning_graph(act, verifier, repair_limit: int):
    """与 build.py 同形的拓扑，只把模型输出换成「每轮都调工具」。"""

    def _gate(state: AgentState) -> dict:
        return {"approvals": {}}

    def _tools(state: AgentState) -> dict:
        last = state["messages"][-1]
        return {
            "messages": [
                ToolMessage(content="ok", tool_call_id=c["id"], name=c["name"])
                for c in last.tool_calls
            ]
        }

    def _respond(state: AgentState) -> dict:
        return {"messages": [AIMessage(content="done")]}

    def _replan(state: AgentState) -> dict:
        """简化版 replan：修不动了就砍掉剩余步骤，直接收尾。

        真节点会先问模型「换个做法行不行」；这里只关心修复循环的收敛与上限，
        所以用最简单的等价行为（放弃）代替。
        """
        return {"plan": (state.get("plan") or [])[: state.get("step_idx", 0)]}

    graph = StateGraph(AgentState)
    graph.add_node("act", act)
    graph.add_node(APPROVE, _gate)
    graph.add_node(TOOLS, _tools)
    graph.add_node(VERIFY, verifier)
    graph.add_node(REVIEW, _review_pass)
    graph.add_node(REPAIR, repair)
    graph.add_node(REPLAN, _replan)
    graph.add_node(ADVANCE, advance)
    graph.add_node(RESPOND, _respond)

    graph.add_edge(START, "act")
    graph.add_conditional_edges("act", route_after_act, {APPROVE: APPROVE, VERIFY: VERIFY})
    graph.add_edge(APPROVE, TOOLS)
    graph.add_edge(TOOLS, "act")
    graph.add_conditional_edges(
        VERIFY,
        make_route_after_verify(repair_limit),
        {REPAIR: REPAIR, REPLAN: REPLAN, REVIEW: REVIEW, RESPOND: RESPOND},
    )
    graph.add_conditional_edges(
        REVIEW,
        make_route_after_review(repair_limit),
        {REPAIR: REPAIR, REPLAN: REPLAN, ADVANCE: ADVANCE, RESPOND: RESPOND},
    )
    graph.add_edge(REPAIR, "act")
    graph.add_edge(REPLAN, RESPOND)
    graph.add_edge(ADVANCE, "act")
    graph.add_edge(RESPOND, END)
    return graph.compile(checkpointer=MemorySaver())


def _one_step() -> dict:
    return {
        "messages": [HumanMessage(content="修好它")],
        "plan": ["完成这一步"],
        "step_idx": 0,
        "tool_rounds": 0,
        "budget_exhausted": False,
        "dirty": True,
        "retry": 0,
        "verification": {},
        "approvals": {},
    }


def test_maxed_out_repair_loop_fits_the_estimated_limit() -> None:
    """每轮修复都烧光工具预算，仍要走到 respond 而不是抛 GraphRecursionError。"""

    async def scenario() -> None:
        rounds, repairs = 3, 2
        act = _BudgetBurningAct(rounds)
        verifier = _FlakyVerifier(fail_times=99)
        app = _burning_graph(act, verifier, repair_limit=repairs)
        limit = estimate_recursion_limit(1, rounds, repairs, 0)

        result = await app.ainvoke(_one_step(), {**CONFIG, "recursion_limit": limit})

        assert result["retry"] == repairs
        assert result["verification"]["status"] == "failed"
        # (修复次数+1) 个周期，每个周期 act 跑满 (轮次+1) 次
        assert act.calls == (repairs + 1) * (rounds + 1)
        assert verifier.calls == repairs + 1

    _run(scenario())


def test_repair_blind_limit_would_be_too_small() -> None:
    """漏算修复循环的旧公式确实不够用 —— 这就是当初的缺陷，留着防回退。"""

    async def scenario() -> None:
        rounds, repairs = 3, 2
        per_cycle = (rounds + 1) + 2 * rounds + 1
        repair_blind = per_cycle + 1 + 10  # 单周期 + advance + planner/respond 余量

        act = _BudgetBurningAct(rounds)
        app = _burning_graph(act, _FlakyVerifier(fail_times=99), repair_limit=repairs)

        with pytest.raises(GraphRecursionError):
            await app.ainvoke(_one_step(), {**CONFIG, "recursion_limit": repair_blind})

    _run(scenario())
