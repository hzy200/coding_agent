"""`AgentRuntime.history` 只读 checkpoint，不该要求 API Key / 沙箱。

它服务于 Web `/api/thread` 与 TUI `/switch` —— 这两处只想回放对话，
不该因为环境里没有 Key 或 WSL 就用不了。这里用一个被污染成必然报错的
`build_graph` 来证明「历史恢复根本没有建图」。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from coding_agent.config import Settings
from coding_agent.graph.state import AgentState
from coding_agent.runtime import AgentRuntime

THREAD = "t-hist"


def _populated_saver() -> MemorySaver:
    saver = MemorySaver()

    def noop(state: AgentState) -> dict:  # noqa: ANN001
        return {}

    graph = StateGraph(AgentState)
    graph.add_node("noop", noop)
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    app = graph.compile(checkpointer=saver)

    app.invoke(
        {
            "messages": [
                HumanMessage(content="把计算器加个除法"),
                ToolMessage(content="工具输出", tool_call_id="c1"),
                AIMessage(content="已经加好了"),
            ]
        },
        {"configurable": {"thread_id": THREAD}},
    )
    return saver


def _history(saver: MemorySaver) -> list:
    runtime = AgentRuntime(
        Settings(_env_file=None), workspace="/ws", checkpointer=saver
    )
    return asyncio.run(runtime.history(THREAD))


def test_history_returns_user_and_assistant_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coding_agent.runtime.build_graph",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("history 不应建图")),
    )
    history = _history(_populated_saver())
    assert [(h.role, h.text) for h in history] == [
        ("user", "把计算器加个除法"),
        ("assistant", "已经加好了"),
    ]


def test_history_skips_tool_messages() -> None:
    history = _history(_populated_saver())
    assert all(h.text != "工具输出" for h in history)


def test_history_of_unknown_thread_is_empty() -> None:
    runtime = AgentRuntime(Settings(_env_file=None), workspace="/ws", checkpointer=MemorySaver())
    assert asyncio.run(runtime.history("no-such-thread")) == []


def test_history_works_without_api_key() -> None:
    """空 Key 也不该影响只读历史：设置里没有 Key 仍然能拿到对话。"""
    settings = Settings(_env_file=None, deepseek_api_key="")
    assert settings.deepseek_api_key == ""
    runtime = AgentRuntime(settings, workspace="/ws", checkpointer=_populated_saver())
    assert len(asyncio.run(runtime.history(THREAD))) == 2
