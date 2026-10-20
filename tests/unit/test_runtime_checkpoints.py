"""checkpoint 轮转与会话 fork/rewind。

用一张极小图代替真实 Agent 图，避免单测拉起 LLM/WSL。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from coding_agent.config import Settings
from coding_agent.memory.checkpointer import CheckpointStore
from coding_agent.runtime import AgentRuntime


class _State(TypedDict):
    messages: Annotated[list, add_messages]


def _node_a(state: _State) -> dict:
    return {"messages": [HumanMessage(content="a")]}


def _node_b(state: _State) -> dict:
    return {"messages": [HumanMessage(content="b")]}


def _tiny_graph(checkpointer) -> object:  # noqa: ANN001
    graph = StateGraph(_State)
    graph.add_node("a", _node_a)
    graph.add_node("b", _node_b)
    graph.set_entry_point("a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    return graph.compile(checkpointer=checkpointer)


def test_checkpoint_prune_keeps_latest(tmp_path) -> None:
    async def _main() -> None:
        store = CheckpointStore(str(tmp_path / "cp.sqlite"))
        app = _tiny_graph(await store.aopen())
        config = {"configurable": {"thread_id": "t"}}
        for index in range(5):
            await app.ainvoke(
                {"messages": [HumanMessage(content=f"start-{index}")]}, config
            )
        removed = await store.aprune("t", 4)
        rows = await (
            await store._connection.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM checkpoints"
            )
        ).fetchone()
        assert removed >= 1
        assert rows[0] == 4
        # 最新 checkpoint 仍然可读
        latest = await app.checkpointer.aget_tuple(config)
        assert latest is not None
        await store.aclose()

    asyncio.run(_main())


def test_fork_and_rewind(monkeypatch, tmp_path) -> None:
    async def _main() -> None:
        runtime = AgentRuntime(
            Settings(_env_file=None, checkpoint_path=str(tmp_path / "rt.sqlite"))
        )
        app = _tiny_graph(MemorySaver())

        async def _fake_graph() -> object:
            return app

        async def _fake_checkpointer() -> object:
            return app.checkpointer

        monkeypatch.setattr(runtime, "_ensure_graph", _fake_graph)
        monkeypatch.setattr(runtime, "_checkpointer", _fake_checkpointer)
        source_config = {"configurable": {"thread_id": "source"}}
        await app.ainvoke(
            {"messages": [HumanMessage(content="origin")]}, source_config
        )
        source_state = await app.aget_state(source_config)
        source_messages = list(source_state.values["messages"])

        listed = await runtime.checkpoints("source", limit=5)
        assert listed
        assert listed[0]["checkpoint_id"]
        assert listed[0]["preview"]

        forked = await runtime.fork("source")
        forked_state = await app.aget_state(
            {"configurable": {"thread_id": forked}}
        )
        assert forked.startswith("source-fork-")
        assert [m.content for m in forked_state.values["messages"]] == [
            m.content for m in source_messages
        ]

        new_checkpoint = await runtime.rewind("source", steps=1)
        assert new_checkpoint
        rewound = await app.aget_state(source_config)
        assert len(rewound.values["messages"]) < len(source_messages)
        await runtime.aclose()

    asyncio.run(_main())
