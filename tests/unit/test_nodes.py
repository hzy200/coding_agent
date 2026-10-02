from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from coding_agent.graph.nodes.act import BUDGET_EXHAUSTED_TEMPLATE, make_act_node
from coding_agent.graph.nodes.advance import advance
from coding_agent.graph.nodes.tools import make_tools_node
from coding_agent.llm.prompts import compose_system_prompt


class _EchoLLM:
    """记录收到的消息，便于断言 act 往提示里塞了什么。"""

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.seen: list = []

    def invoke(self, messages, config=None):  # noqa: ANN001
        self.seen = messages
        return AIMessage(content=self.reply)


# ---------------- advance ----------------

def test_advance_increments_step_and_resets_budget() -> None:
    out = advance({"messages": [], "plan": ["a", "b"], "step_idx": 0, "tool_rounds": 7})
    assert out == {"step_idx": 1, "tool_rounds": 0, "budget_exhausted": False}


# ---------------- act ----------------

def test_act_injects_plan_and_marks_current_step() -> None:
    llm = _EchoLLM()
    node = make_act_node(llm, max_tool_rounds=5)
    node(
        {
            "messages": [HumanMessage(content="do it")],
            "plan": ["first", "second"],
            "step_idx": 1,
            "tool_rounds": 0,
        },
        None,
    )

    assert isinstance(llm.seen[0], SystemMessage)
    system = llm.seen[0].content
    assert "first" in system and "（已完成）" in system
    assert "second" in system and "← 现在只做这一步" in system
    # 步骤上下文只进系统提示，不写回消息历史
    assert llm.seen[1:] == [HumanMessage(content="do it")]


def test_act_counts_rounds() -> None:
    node = make_act_node(_EchoLLM(), max_tool_rounds=5)
    out = node({"messages": [HumanMessage(content="x")], "tool_rounds": 2}, None)
    assert out["tool_rounds"] == 3


def test_act_stops_without_tool_calls_when_budget_exhausted() -> None:
    """这是路由能无条件信任 tool_calls 的前提：超预算的 act 不产出 tool_calls。"""
    llm = _EchoLLM()
    node = make_act_node(llm, max_tool_rounds=3)
    out = node({"messages": [HumanMessage(content="x")], "tool_rounds": 3}, None)

    assert llm.seen == []  # 根本没调用模型
    message = out["messages"][0]
    assert not getattr(message, "tool_calls", None)
    assert message.content == BUDGET_EXHAUSTED_TEMPLATE.format(limit=3)
    assert "tool_rounds" not in out


# ---------------- tools ----------------

class _FakeTool:
    def __init__(self, name: str, result: str = "done") -> None:
        self.name = name
        self.result = result
        self.seen: dict = {}

    def invoke(self, args, config=None):  # noqa: ANN001
        self.seen = args
        return self.result


class _BoomTool:
    name = "boom"

    def invoke(self, args, config=None):  # noqa: ANN001
        raise RuntimeError("kaboom")


def _tool_call(name: str, args: dict, call_id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def test_tools_injects_state_cwd_when_model_omits_it() -> None:
    tool = _FakeTool("shell_exec")
    node = make_tools_node([tool])
    state = {"messages": [_tool_call("shell_exec", {"command": "ls", "reason": "r"})],
             "cwd": "/home/u/ws"}
    node(state, None)
    assert tool.seen["cwd"] == "/home/u/ws"


def test_tools_keeps_explicit_cwd() -> None:
    tool = _FakeTool("shell_exec")
    node = make_tools_node([tool])
    state = {
        "messages": [_tool_call("shell_exec", {"command": "ls", "reason": "r", "cwd": "/tmp"})],
        "cwd": "/home/u/ws",
    }
    node(state, None)
    assert tool.seen["cwd"] == "/tmp"


def test_tools_does_not_inject_cwd_for_other_tools() -> None:
    tool = _FakeTool("file_read")
    node = make_tools_node([tool])
    node({"messages": [_tool_call("file_read", {"path": "a.py"})], "cwd": "/home/u/ws"}, None)
    assert tool.seen == {"path": "a.py"}


def test_unknown_tool_is_reported_to_model() -> None:
    node = make_tools_node([_FakeTool("shell_exec")])
    out = node({"messages": [_tool_call("nope", {})]}, None)
    assert "不存在名为 nope 的工具" in out["messages"][0].content


def test_tool_exception_is_captured_not_raised() -> None:
    node = make_tools_node([_BoomTool()])
    out = node({"messages": [_tool_call("boom", {})]}, None)
    assert "工具执行异常：RuntimeError: kaboom" in out["messages"][0].content


def test_tool_results_are_paired_with_call_ids() -> None:
    node = make_tools_node([_FakeTool("shell_exec")])
    out = node({"messages": [_tool_call("shell_exec", {}, call_id="abc123")]}, None)
    assert isinstance(out["messages"][0], ToolMessage)
    assert out["messages"][0].tool_call_id == "abc123"


# ---------------- compose_system_prompt ----------------

@pytest.mark.parametrize("allow_write", [True, False])
def test_compose_without_plan_is_just_base_plus_permission(allow_write: bool) -> None:
    out = compose_system_prompt(plan=None, allow_write=allow_write)
    assert "当前任务计划" not in out
    assert "会话权限" in out


def test_compose_states_readonly_permission() -> None:
    assert "L0 只读" in compose_system_prompt(allow_write=False)
    assert "L1 低风险写" in compose_system_prompt(allow_write=True)
