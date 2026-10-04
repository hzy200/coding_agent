"""审批关卡：决策逻辑 + 图级中断/恢复。

图级测试用真实的 StateGraph 跑 —— `interrupt()` 只能在图运行上下文里工作，
直接调节点函数验证不了挂起与恢复。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from coding_agent.graph.nodes.approve import make_approval_gate_node, tool_level
from coding_agent.graph.nodes.tools import make_tools_node
from coding_agent.graph.routing import APPROVE, TOOLS
from coding_agent.graph.state import AgentState
from coding_agent.sandbox.policy import (
    APPROVAL_APPROVE,
    APPROVAL_ASK,
    APPROVAL_DENY,
    ASK,
    AUTO,
    DENY,
    CommandLevel,
    SessionPolicy,
)
from coding_agent.tools.artifacts import ShellArtifact, pack

CONFIG = {"configurable": {"thread_id": "t1"}}


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------- SessionPolicy ----------------

@pytest.mark.parametrize(
    ("allow_write", "mode", "level", "expected"),
    [
        (False, APPROVAL_ASK, CommandLevel.READ, AUTO),
        (False, APPROVAL_ASK, CommandLevel.LOW_WRITE, DENY),
        (True, APPROVAL_ASK, CommandLevel.LOW_WRITE, AUTO),
        (False, APPROVAL_ASK, CommandLevel.MUTATE, ASK),
        (False, APPROVAL_ASK, CommandLevel.DANGER, ASK),
        (True, APPROVAL_ASK, CommandLevel.DANGER, ASK),
        (False, APPROVAL_APPROVE, CommandLevel.MUTATE, AUTO),
        (False, APPROVAL_APPROVE, CommandLevel.DANGER, AUTO),
        (False, APPROVAL_DENY, CommandLevel.MUTATE, DENY),
        (True, APPROVAL_DENY, CommandLevel.DANGER, DENY),
    ],
)
def test_session_policy(allow_write, mode, level, expected) -> None:
    assert SessionPolicy(allow_write=allow_write, approval_mode=mode).decide(level) == expected


def test_low_write_never_prompts() -> None:
    """L1 由 --write 一次性授权，不该逐条打断用户。"""
    policy = SessionPolicy(allow_write=True, approval_mode=APPROVAL_ASK)
    assert policy.decide(CommandLevel.LOW_WRITE) == AUTO


# ---------------- tool_level ----------------

def test_shell_level_comes_from_command() -> None:
    assert tool_level("shell_exec", {"command": "ls"}) is CommandLevel.READ
    assert tool_level("shell_exec", {"command": "git commit -m x"}) is CommandLevel.MUTATE
    assert tool_level("shell_exec", {"command": "rm -rf /"}) is CommandLevel.DANGER


def test_file_tools_are_governed_by_write_flag() -> None:
    """文件工具是受控路径（有 diff、有备份），不逐条询问。"""
    assert tool_level("file_read", {"path": "a"}) is CommandLevel.READ
    assert tool_level("file_write", {"path": "a"}) is CommandLevel.LOW_WRITE
    assert tool_level("file_edit", {"path": "a"}) is CommandLevel.LOW_WRITE


def test_unregistered_tool_fails_closed() -> None:
    assert tool_level("some_new_tool", {}) is CommandLevel.DANGER


# ---------------- 图级：中断与恢复 ----------------

class _RecordingTool:
    def __init__(self, name: str = "shell_exec") -> None:
        self.name = name
        self.invocations: list[dict] = []

    def invoke(self, args, config=None):  # noqa: ANN001
        self.invocations.append(dict(args))
        return pack("done", ShellArtifact(command=str(args.get("command", "")), ok=True))


def _act_stub(state: AgentState) -> dict:
    call = {
        "name": "shell_exec",
        "args": {"command": "pip install requests", "reason": "装依赖"},
        "id": "c1",
    }
    return {"messages": [AIMessage(content="", tool_calls=[call])]}


def _gate_graph(policy: SessionPolicy, tool: _RecordingTool):
    graph = StateGraph(AgentState)
    graph.add_node("act", _act_stub)
    graph.add_node(APPROVE, make_approval_gate_node(policy))
    graph.add_node(TOOLS, make_tools_node([tool]))
    graph.add_edge(START, "act")
    graph.add_edge("act", APPROVE)
    graph.add_edge(APPROVE, TOOLS)
    graph.add_edge(TOOLS, END)
    return graph.compile(checkpointer=MemorySaver())


def _initial() -> dict:
    return {"messages": [HumanMessage(content="装个包")], "approvals": {}}


def test_l2_command_suspends_the_graph() -> None:
    async def scenario() -> None:
        tool = _RecordingTool()
        app = _gate_graph(SessionPolicy(approval_mode=APPROVAL_ASK), tool)

        result = await app.ainvoke(_initial(), CONFIG)

        assert "__interrupt__" in result
        request = result["__interrupt__"][0].value["requests"][0]
        assert request["call_id"] == "c1"
        assert request["command"] == "pip install requests"
        assert request["level"] == "L2 变更性"
        # 挂起期间绝不能执行
        assert tool.invocations == []

    _run(scenario())


def test_approving_resumes_and_executes() -> None:
    async def scenario() -> None:
        tool = _RecordingTool()
        app = _gate_graph(SessionPolicy(approval_mode=APPROVAL_ASK), tool)

        await app.ainvoke(_initial(), CONFIG)
        await app.ainvoke(Command(resume={"c1": True}), CONFIG)

        assert tool.invocations == [{"command": "pip install requests", "reason": "装依赖"}]

    _run(scenario())


def test_denying_resumes_without_executing() -> None:
    async def scenario() -> None:
        tool = _RecordingTool()
        app = _gate_graph(SessionPolicy(approval_mode=APPROVAL_ASK), tool)

        await app.ainvoke(_initial(), CONFIG)
        result = await app.ainvoke(Command(resume={"c1": False}), CONFIG)

        assert tool.invocations == []
        message = result["messages"][-1]
        assert "用户拒绝了这条命令" in message.content
        assert message.artifact["decision"] == "denied"

    _run(scenario())


def test_missing_answer_in_resume_denies() -> None:
    async def scenario() -> None:
        tool = _RecordingTool()
        app = _gate_graph(SessionPolicy(approval_mode=APPROVAL_ASK), tool)

        await app.ainvoke(_initial(), CONFIG)
        await app.ainvoke(Command(resume={}), CONFIG)  # 没给 c1 的答复

        assert tool.invocations == []

    _run(scenario())


def test_garbage_resume_payload_denies() -> None:
    """前端给了无法理解的答复时 fail closed。"""

    async def scenario() -> None:
        tool = _RecordingTool()
        app = _gate_graph(SessionPolicy(approval_mode=APPROVAL_ASK), tool)

        await app.ainvoke(_initial(), CONFIG)
        await app.ainvoke(Command(resume="definitely-not-a-dict"), CONFIG)

        assert tool.invocations == []

    _run(scenario())


def test_read_only_command_does_not_suspend() -> None:
    async def scenario() -> None:
        tool = _RecordingTool()
        app = _gate_graph(SessionPolicy(approval_mode=APPROVAL_ASK), tool)

        def read_stub(state: AgentState) -> dict:
            call = {"name": "shell_exec", "args": {"command": "ls -la"}, "id": "c9"}
            return {"messages": [AIMessage(content="", tool_calls=[call])]}

        graph = StateGraph(AgentState)
        graph.add_node("act", read_stub)
        graph.add_node(APPROVE, make_approval_gate_node(SessionPolicy()))
        graph.add_node(TOOLS, make_tools_node([tool]))
        graph.add_edge(START, "act")
        graph.add_edge("act", APPROVE)
        graph.add_edge(APPROVE, TOOLS)
        graph.add_edge(TOOLS, END)
        app = graph.compile(checkpointer=MemorySaver())

        result = await app.ainvoke(_initial(), CONFIG)

        assert "__interrupt__" not in result
        assert tool.invocations == [{"command": "ls -la"}]

    _run(scenario())


def test_approve_mode_skips_the_prompt() -> None:
    async def scenario() -> None:
        tool = _RecordingTool()
        app = _gate_graph(SessionPolicy(approval_mode=APPROVAL_APPROVE), tool)

        result = await app.ainvoke(_initial(), CONFIG)

        assert "__interrupt__" not in result
        assert tool.invocations == [{"command": "pip install requests", "reason": "装依赖"}]

    _run(scenario())


def test_deny_mode_blocks_without_prompting() -> None:
    async def scenario() -> None:
        tool = _RecordingTool()
        app = _gate_graph(SessionPolicy(approval_mode=APPROVAL_DENY), tool)

        result = await app.ainvoke(_initial(), CONFIG)

        assert "__interrupt__" not in result
        assert tool.invocations == []
        assert "被会话策略拒绝" in result["messages"][-1].content

    _run(scenario())


def test_approvals_do_not_leak_into_next_round() -> None:
    """审批结果用后即清，第二轮不能靠上一轮的批准蒙混过关。"""

    async def scenario() -> None:
        tool = _RecordingTool()
        app = _gate_graph(SessionPolicy(approval_mode=APPROVAL_ASK), tool)

        await app.ainvoke(_initial(), CONFIG)
        await app.ainvoke(Command(resume={"c1": True}), CONFIG)
        assert len(tool.invocations) == 1

        # 再跑一轮，仍应挂起而不是直接执行
        result = await app.ainvoke(_initial(), CONFIG)
        assert "__interrupt__" in result
        assert len(tool.invocations) == 1

    _run(scenario())


@pytest.mark.parametrize(
    "tool_calls",
    [
        [{"name": "shell_exec", "args": {"command": "pip install requests"}, "id": ""}],  # 空 id
        [  # 两个调用共用同一个 id
            {"name": "shell_exec", "args": {"command": "pip install a"}, "id": "dup"},
            {"name": "shell_exec", "args": {"command": "pip install b"}, "id": "dup"},
        ],
    ],
)
def test_empty_or_duplicate_call_id_fails_closed(tool_calls: list[dict]) -> None:
    """id 为空或重复时审批结果无法配对，必须整批拒绝且不进入交互。"""

    async def scenario() -> None:
        tool = _RecordingTool()

        def act_stub(state: AgentState) -> dict:
            return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}

        graph = StateGraph(AgentState)
        graph.add_node("act", act_stub)
        graph.add_node(APPROVE, make_approval_gate_node(SessionPolicy(approval_mode=APPROVAL_ASK)))
        graph.add_node(TOOLS, make_tools_node([tool]))
        graph.add_edge(START, "act")
        graph.add_edge("act", APPROVE)
        graph.add_edge(APPROVE, TOOLS)
        graph.add_edge(TOOLS, END)
        app = graph.compile(checkpointer=MemorySaver())

        result = await app.ainvoke(_initial(), CONFIG)

        assert "__interrupt__" not in result  # 不该挂起等一个配不上号的答复
        assert tool.invocations == []
        assert result["messages"][-1].artifact["decision"] == "denied"

    _run(scenario())
