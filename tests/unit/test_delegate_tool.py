"""delegate_explore 的登记、拦截与预算。

关键点是「工具本身永远不被 invoke」：真正执行的是宿主 runner。这样它才能
沿用 tools 节点的审批/审计唯一咽喉，而不是长出一条绕过的执行路径。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from coding_agent.config import Settings
from coding_agent.graph.build import build_graph
from coding_agent.graph.nodes.approve import tool_level
from coding_agent.graph.nodes.tools import make_tools_node
from coding_agent.sandbox.policy import CommandLevel
from coding_agent.sandbox.wsl_exec import ExecResult, WslSandbox
from coding_agent.tools.artifacts import SubAgentArtifact
from coding_agent.tools.delegate import DELEGATE_EXPLORE, build_delegate_tool


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.invoked = False

    def invoke(self, args: dict[str, Any], config: Any = None) -> str:
        self.invoked = True
        return "ok"


def _state(
    *,
    delegate_calls: int = 0,
    delegation_allowed: bool = True,
    delegation_budget: int = 6,
    plan_kinds: list[str] | None = None,
) -> dict[str, Any]:
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": DELEGATE_EXPLORE,
                "args": {"goal": "找到入口", "paths": ["."], "reason": "不熟"},
                "id": "d1",
            }
        ],
    )
    return {
        "messages": [HumanMessage(content="看一下这个项目"), call],
        "cwd": "/home/u/ws",
        "approvals": {"d1": "auto"},
        "delegate_calls": delegate_calls,
        "plan": ["探索"],
        "plan_kinds": plan_kinds or ["explore"],
        "delegation_allowed": delegation_allowed,
        "delegation_budget": delegation_budget,
    }


def _run(node, state: dict[str, Any], config: Any = None):  # noqa: ANN001
    return asyncio.run(node(state, config))


def test_delegate_is_registered_as_read_only() -> None:
    assert tool_level(DELEGATE_EXPLORE, {}) == CommandLevel.READ


def test_delegate_tool_schema_and_host_only_invocation() -> None:
    tool = build_delegate_tool()
    assert tool.name == DELEGATE_EXPLORE
    assert {"goal", "paths", "reason"} <= set(tool.args)

    with pytest.raises(Exception, match="宿主"):
        tool.invoke({"goal": "g", "paths": [], "reason": "r"})


def test_tools_node_intercepts_delegate_and_never_invokes_it() -> None:
    calls: list[dict[str, Any]] = []

    async def runner(**kwargs: Any) -> SubAgentArtifact:
        calls.append(kwargs)
        return SubAgentArtifact(
            agent_id=f"explore:{kwargs['call_id']}",
            parent_call_id=kwargs["call_id"],
            goal=kwargs["args"]["goal"],
            status="success",
            summary="入口在 main.py",
            relevant_files=["main.py"],
        )

    delegate_tool = build_delegate_tool()
    node = make_tools_node(
        [delegate_tool, _FakeTool("file_read")],
        delegate_runner=runner,
        max_delegate_calls=2,
    )
    out = _run(node, _state())

    assert out["delegate_calls"] == 1
    assert calls and calls[0]["parent_request"] == "看一下这个项目"
    message = out["messages"][0]
    assert message.artifact["kind"] == "subagent"
    assert message.artifact["summary"] == "入口在 main.py"
    assert "入口在 main.py" in message.content


def test_delegate_budget_stops_further_calls() -> None:
    async def runner(**_kwargs: Any) -> SubAgentArtifact:
        raise AssertionError("预算耗尽后不应再调用 runner")

    node = make_tools_node(
        [build_delegate_tool()],
        delegate_runner=runner,
        max_delegate_calls=2,
    )
    out = _run(node, _state(delegate_calls=2))

    assert out["delegate_calls"] == 2
    assert "上限" in out["messages"][0].content


def test_delegate_is_rejected_when_the_cost_policy_disallows_it() -> None:
    async def runner(**_kwargs: Any) -> SubAgentArtifact:
        raise AssertionError("策略关闭时不应调用 runner")

    node = make_tools_node([build_delegate_tool()], delegate_runner=runner)
    out = _run(node, _state(delegation_allowed=False))

    assert "未启用探索委派" in out["messages"][0].content
    assert out["delegate_calls"] == 0


def test_delegate_is_rejected_outside_an_explore_step() -> None:
    async def runner(**_kwargs: Any) -> SubAgentArtifact:
        raise AssertionError("实现/验证步不应调用 runner")

    node = make_tools_node([build_delegate_tool()], delegate_runner=runner)
    out = _run(node, _state(plan_kinds=["implement"]))

    assert "只有探索步" in out["messages"][0].content
    assert out["delegate_calls"] == 0


def test_delegate_budget_comes_from_the_cost_policy() -> None:
    async def runner(**_kwargs: Any) -> SubAgentArtifact:
        raise AssertionError("预算耗尽后不应调用 runner")

    node = make_tools_node(
        [build_delegate_tool()], delegate_runner=runner, max_delegate_calls=6
    )
    out = _run(node, _state(delegation_budget=1, delegate_calls=1))

    assert "最多委派 1 次" in out["messages"][0].content
    assert out["delegate_calls"] == 1


def test_delegate_without_runner_reports_disabled() -> None:
    node = make_tools_node([build_delegate_tool()], delegate_runner=None)
    out = _run(node, _state())

    assert "未启用" in out["messages"][0].content
    assert out["delegate_calls"] == 0


def _multi_state(count: int, *, delegate_calls: int = 0) -> dict[str, Any]:
    calls = [
        {
            "name": DELEGATE_EXPLORE,
            "args": {"goal": f"目标{index}", "paths": [], "reason": "并发"},
            "id": f"d{index}",
        }
        for index in range(count)
    ]
    return {
        "messages": [
            HumanMessage(content="并发探索"),
            AIMessage(content="", tool_calls=calls),
        ],
        "cwd": "/home/u/ws",
        "approvals": {f"d{index}": "auto" for index in range(count)},
        "delegate_calls": delegate_calls,
        "plan": ["探索"],
        "plan_kinds": ["explore"],
        "delegation_allowed": True,
        "delegation_budget": 6,
    }


def _subagent(call_id: str) -> SubAgentArtifact:
    return SubAgentArtifact(
        agent_id=f"explore:{call_id}",
        parent_call_id=call_id,
        goal=call_id,
        status="success",
        summary=f"结论 {call_id}",
    )


def test_multiple_delegate_calls_run_concurrently() -> None:
    """三个子智能体必须同时到达屏障；串行执行会在 wait_for 上超时。"""
    barrier = asyncio.Barrier(3)

    async def runner(**kwargs: Any) -> SubAgentArtifact:
        await asyncio.wait_for(barrier.wait(), timeout=1)
        return _subagent(kwargs["call_id"])

    node = make_tools_node(
        [build_delegate_tool()],
        delegate_runner=runner,
        max_delegate_calls=3,
        max_concurrent_agents=3,
    )
    out = _run(node, _multi_state(3))

    assert out["delegate_calls"] == 3
    assert [message.tool_call_id for message in out["messages"]] == ["d0", "d1", "d2"]
    assert all("工具执行异常" not in message.content for message in out["messages"])
    assert all(
        message.artifact and message.artifact["kind"] == "subagent"
        for message in out["messages"]
    )


def test_concurrent_batch_respects_the_task_budget() -> None:
    seen: list[str] = []

    async def runner(**kwargs: Any) -> SubAgentArtifact:
        seen.append(kwargs["call_id"])
        return _subagent(kwargs["call_id"])

    node = make_tools_node(
        [build_delegate_tool()],
        delegate_runner=runner,
        max_delegate_calls=2,
        max_concurrent_agents=3,
    )
    out = _run(node, _multi_state(3))

    assert out["delegate_calls"] == 2
    assert seen == ["d0", "d1"]
    artifacts = [
        message for message in out["messages"]
        if message.artifact and message.artifact.get("kind") == "subagent"
    ]
    rejected = [message for message in out["messages"] if "上限" in message.content]
    assert len(artifacts) == 2
    assert len(rejected) == 1
    assert rejected[0].tool_call_id == "d2"


def test_build_graph_with_explore_enabled_keeps_the_topology(monkeypatch) -> None:
    """打开开关只多一个工具，不新增图节点/边（recursion_limit 因此不变）。"""
    probe = ExecResult(
        command="command -v rg", exit_code=0, stdout="/usr/bin/rg", stderr="", duration_ms=0
    )
    monkeypatch.setattr(
        WslSandbox,
        "_exec",
        lambda self, script, *, wall, command="", **kwargs: probe,
    )
    settings = Settings(
        _env_file=None, deepseek_api_key="x", explore_enabled=True, wsl_workspace="/tmp/ws"
    )
    graph = build_graph(settings, checkpointer=MemorySaver())

    nodes = set(graph.get_graph().nodes)
    assert {"planner", "act", "approval_gate", "tools", "verify", "review"} <= nodes
    # 没有新增图节点：子智能体在 tools 节点内部执行
    assert "delegate_explore" not in nodes


def test_async_tools_node_still_runs_normal_tools() -> None:
    tool = _FakeTool("file_read")
    node = make_tools_node([tool])
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "file_read", "args": {"path": "a.py"}, "id": "c1"}],
            )
        ],
        "approvals": {"c1": "auto"},
    }
    out = _run(node, state)

    assert tool.invoked is True
    assert out["messages"][0].tool_call_id == "c1"
