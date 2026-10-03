from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent.graph.routing import (
    ADVANCE,
    APPROVE,
    REPAIR,
    RESPOND,
    VERIFY,
    has_more_steps,
    make_route_after_verify,
    route_after_act,
)

MAX_REPAIRS = 3
route_after_verify = make_route_after_verify(MAX_REPAIRS)


def _state(messages, plan=None, step_idx=0):
    return {"messages": messages, "plan": plan or [], "step_idx": step_idx}


# ---------------- act 之后 ----------------

def test_step_done_goes_to_verify() -> None:
    """一步做完先验证，再决定步进还是收尾。"""
    state = _state([HumanMessage(content="hi"), AIMessage(content="done")], ["only"], 0)
    assert route_after_act(state) == VERIFY


def test_tool_calls_go_through_approval_gate_first() -> None:
    """只要 act 产出了 tool_calls，就必须先过审批关卡，不能直达 tools。"""
    call = AIMessage(content="", tool_calls=[{"name": "shell_exec", "args": {}, "id": "1"}])
    state = _state([call], ["a", "b"], 0)
    assert route_after_act(state) == APPROVE


def test_tool_message_does_not_look_like_tool_call() -> None:
    state = _state([ToolMessage(content="out", tool_call_id="1")], ["a"], 0)
    assert route_after_act(state) == VERIFY


# ---------------- verify 之后 ----------------

def test_verify_then_advance_when_steps_remain() -> None:
    state = _state([AIMessage(content="step1 done")], ["a", "b"], 0)
    assert has_more_steps(state)
    assert route_after_verify(state) == ADVANCE


def test_verify_then_respond_on_last_step() -> None:
    state = _state([AIMessage(content="step2 done")], ["a", "b"], 1)
    assert not has_more_steps(state)
    assert route_after_verify(state) == RESPOND


def test_verify_then_respond_when_plan_empty() -> None:
    assert route_after_verify(_state([AIMessage(content="done")], [], 0)) == RESPOND


# ---------------- 失败驱动的修复循环 ----------------

def test_failed_verification_enters_repair_when_budget_remains() -> None:
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "failed", "summary": "1 failed"}
    state["retry"] = 0
    assert route_after_verify(state) == REPAIR


def test_failed_verification_reports_when_budget_exhausted() -> None:
    """上限是硬性的：没有它，自动修复就是个烧 token 的无限循环，
    而且用户永远拿不到「我试过了但没修好」这个结论。"""
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "failed"}
    state["retry"] = MAX_REPAIRS
    assert route_after_verify(state) == RESPOND


def test_repair_budget_counts_up_to_the_limit() -> None:
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "failed"}
    for attempt in range(MAX_REPAIRS):
        state["retry"] = attempt
        assert route_after_verify(state) == REPAIR, attempt
    state["retry"] = MAX_REPAIRS
    assert route_after_verify(state) == RESPOND


def test_repair_budget_is_per_step_not_global() -> None:
    """还有子任务时，预算耗尽也只是结束本步，不是结束整个任务。"""
    state = _state([AIMessage(content="done")], ["a", "b"], 0)
    state["verification"] = {"status": "failed"}
    state["retry"] = MAX_REPAIRS
    assert route_after_verify(state) == RESPOND  # 本步放弃，交给收尾报告


def test_passing_verification_ignores_retry_counter() -> None:
    state = _state([AIMessage(content="done")], ["a", "b"], 0)
    state["verification"] = {"status": "ok"}
    state["retry"] = MAX_REPAIRS
    assert route_after_verify(state) == ADVANCE


def test_zero_budget_disables_the_loop() -> None:
    strict = make_route_after_verify(0)
    state = _state([AIMessage(content="done")], ["a"], 0)
    state["verification"] = {"status": "failed"}
    state["retry"] = 0
    assert strict(state) == RESPOND
