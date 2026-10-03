"""失败驱动的修复循环。

图级测试用真实的 StateGraph 跑一遍：只有把 act → verify → repair → act 这条环
真的连起来跑，才能证明它会收敛、且到上限就停。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from coding_agent.graph.nodes.repair import repair
from coding_agent.graph.routing import (
    REPAIR,
    RESPOND,
    VERIFY,
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


def _loop_graph(verifier, act, limit: int):
    graph = StateGraph(AgentState)
    graph.add_node("act", act)
    graph.add_node(VERIFY, verifier)
    graph.add_node(REPAIR, repair)
    graph.add_node("respond", lambda state: {"messages": [AIMessage(content="done")]})

    graph.add_edge(START, "act")
    graph.add_conditional_edges("act", route_after_act, {VERIFY: VERIFY})
    graph.add_conditional_edges(
        VERIFY,
        make_route_after_verify(limit),
        {REPAIR: REPAIR, RESPOND: "respond"},
    )
    graph.add_edge(REPAIR, "act")
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
