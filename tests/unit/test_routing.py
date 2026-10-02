from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent.graph.routing import ADVANCE, RESPOND, TOOLS, has_more_steps, route_after_act


def _state(messages, plan=None, step_idx=0):
    return {"messages": messages, "plan": plan or [], "step_idx": step_idx}


def test_plain_answer_with_single_step_goes_to_respond() -> None:
    state = _state([HumanMessage(content="hi"), AIMessage(content="done")], ["only"], 0)
    assert not has_more_steps(state)
    assert route_after_act(state) == RESPOND


def test_plain_answer_with_pending_steps_goes_to_advance() -> None:
    state = _state([HumanMessage(content="hi"), AIMessage(content="step1 done")], ["a", "b"], 0)
    assert has_more_steps(state)
    assert route_after_act(state) == ADVANCE


def test_last_step_of_plan_goes_to_respond() -> None:
    state = _state([AIMessage(content="step2 done")], ["a", "b"], 1)
    assert not has_more_steps(state)
    assert route_after_act(state) == RESPOND


def test_tool_calls_win_over_step_progress() -> None:
    """只要 act 产出了 tool_calls，就一定先执行工具，不能被步进逻辑抢走。"""
    call = AIMessage(content="", tool_calls=[{"name": "shell_exec", "args": {}, "id": "1"}])
    state = _state([call], ["a", "b"], 0)
    assert route_after_act(state) == TOOLS


def test_empty_plan_goes_to_respond() -> None:
    state = _state([AIMessage(content="done")], [], 0)
    assert route_after_act(state) == RESPOND


def test_tool_message_does_not_look_like_tool_call() -> None:
    state = _state([ToolMessage(content="out", tool_call_id="1")], ["a"], 0)
    assert route_after_act(state) == RESPOND
