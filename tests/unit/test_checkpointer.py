"""checkpoint 持久化：会话必须能跨进程恢复。

用一个不需要 LLM 的计数器图验证 —— 只要第二个 store 打开同一个 sqlite 文件
还能读到上一个 store 留下的状态，跨进程恢复就是成立的。
"""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from coding_agent.memory.checkpointer import CheckpointStore


class CounterState(TypedDict, total=False):
    n: int
    log: Annotated[list[str], operator.add]


def _bump(state: CounterState) -> dict:
    return {"n": state.get("n", 0) + 1, "log": ["bump"]}


async def _graph_for(store: CheckpointStore):
    graph = StateGraph(CounterState)
    graph.add_node("bump", _bump)
    graph.add_edge(START, "bump")
    graph.add_edge("bump", END)
    return graph.compile(checkpointer=await store.aopen())


CONFIG = {"configurable": {"thread_id": "t1"}}


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------- 模式选择 ----------------

def test_empty_path_is_memory_backed() -> None:
    assert not CheckpointStore("").persistent


def test_explicit_memory_sentinel_is_not_persistent() -> None:
    assert not CheckpointStore(":memory:").persistent


def test_path_is_persistent(tmp_path) -> None:
    assert CheckpointStore(str(tmp_path / "c.sqlite")).persistent


# ---------------- 真实落盘 ----------------

def test_sqlite_file_and_schema_created(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "nested" / "checkpoints.sqlite"
        store = CheckpointStore(str(path))
        await store.aopen()  # setup() 会建表
        assert path.exists()
        await store.aclose()

    _run(scenario())


def test_state_accumulates_within_one_store(tmp_path) -> None:
    async def scenario() -> None:
        store = CheckpointStore(str(tmp_path / "c.sqlite"))
        graph = await _graph_for(store)
        try:
            await graph.ainvoke({}, CONFIG)
            await graph.ainvoke({}, CONFIG)
            state = await graph.aget_state(CONFIG)
            assert state.values["n"] == 2
        finally:
            await store.aclose()

    _run(scenario())


def test_state_survives_store_reopen(tmp_path) -> None:
    """这就是「跨进程恢复」的最小证明：关掉连接再重新打开，状态还在。"""

    async def scenario() -> None:
        path = tmp_path / "c.sqlite"

        first = CheckpointStore(str(path))
        graph = await _graph_for(first)
        await graph.ainvoke({}, CONFIG)
        await graph.ainvoke({}, CONFIG)
        assert (await graph.aget_state(CONFIG)).values["n"] == 2
        await first.aclose()

        second = CheckpointStore(str(path))
        reopened = await _graph_for(second)
        try:
            state = await reopened.aget_state(CONFIG)
            assert state.values["n"] == 2
            assert state.values["log"] == ["bump", "bump"]
            await reopened.ainvoke({}, CONFIG)
            assert (await reopened.aget_state(CONFIG)).values["n"] == 3
        finally:
            await second.aclose()

    _run(scenario())


def test_threads_are_isolated(tmp_path) -> None:
    async def scenario() -> None:
        store = CheckpointStore(str(tmp_path / "c.sqlite"))
        graph = await _graph_for(store)
        try:
            await graph.ainvoke({}, {"configurable": {"thread_id": "a"}})
            await graph.ainvoke({}, {"configurable": {"thread_id": "a"}})
            await graph.ainvoke({}, {"configurable": {"thread_id": "b"}})

            a = await graph.aget_state({"configurable": {"thread_id": "a"}})
            b = await graph.aget_state({"configurable": {"thread_id": "b"}})
            assert a.values["n"] == 2
            assert b.values["n"] == 1
        finally:
            await store.aclose()

    _run(scenario())


def test_memory_store_does_not_create_files(tmp_path) -> None:
    async def scenario() -> None:
        store = CheckpointStore(":memory:")
        graph = await _graph_for(store)
        await graph.ainvoke({}, CONFIG)
        assert (await graph.aget_state(CONFIG)).values["n"] == 1
        await store.aclose()

    _run(scenario())


def test_close_is_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        store = CheckpointStore(str(tmp_path / "c.sqlite"))
        await store.aopen()
        await store.aclose()
        await store.aclose()
        assert store.saver is None

    _run(scenario())


def test_aopen_is_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        store = CheckpointStore(str(tmp_path / "c.sqlite"))
        first = await store.aopen()
        second = await store.aopen()
        assert first is second
        await store.aclose()

    _run(scenario())
