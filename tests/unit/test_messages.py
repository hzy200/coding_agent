from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from coding_agent.messages import (
    last_human_text,
    recover_dangling_tool_calls,
    text_of,
)


def test_text_of_string_content() -> None:
    assert text_of(HumanMessage(content="hi")) == "hi"


def test_text_of_content_blocks() -> None:
    message = AIMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert text_of(message) == "ab"


def test_text_of_list_of_strings() -> None:
    assert text_of(AIMessage(content=["x", "y"])) == "xy"


def test_text_of_ignores_non_text_blocks() -> None:
    message = AIMessage(content=[{"type": "image_url", "image_url": "..."}, "z"])
    assert text_of(message) == "z"


def test_text_of_missing_content() -> None:
    assert text_of(object()) == ""


# ---------------- last_human_text ----------------

def test_returns_most_recent_human_message() -> None:
    """回归：持久化会话里消息会跨轮次累积，取第一条会拿到上一轮的请求。"""
    messages = [
        HumanMessage(content="第一轮的问题"),
        AIMessage(content="第一轮的回答"),
        HumanMessage(content="第二轮的问题"),
    ]
    assert last_human_text(messages) == "第二轮的问题"


def test_single_turn_still_works() -> None:
    assert last_human_text([HumanMessage(content="唯一的问题")]) == "唯一的问题"


def test_ignores_system_and_tool_messages() -> None:
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="问题"),
        AIMessage(content=""),
        ToolMessage(content="结果", tool_call_id="1"),
    ]
    assert last_human_text(messages) == "问题"


def test_empty_when_no_human_message() -> None:
    assert last_human_text([SystemMessage(content="s")]) == ""
    assert last_human_text([]) == ""


# ---------------- 崩溃后的悬空 tool_calls ----------------

def _dangling(ids=("c1", "c2")):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "file_write", "args": {"path": "a.py"}, "id": call_id}
            for call_id in ids
        ],
    )


def test_recover_adds_a_placeholder_for_each_unanswered_call() -> None:
    messages = [HumanMessage(content="改"), _dangling()]

    recovered = recover_dangling_tool_calls(messages)

    assert [item.tool_call_id for item in recovered] == ["c1", "c2"]
    assert all(item.artifact["interrupted"] for item in recovered)
    assert all("结果未知" in item.content for item in recovered)


def test_recover_only_fills_the_missing_calls() -> None:
    messages = [
        HumanMessage(content="改"),
        _dangling(),
        ToolMessage(content="已完成", tool_call_id="c1"),
    ]

    recovered = recover_dangling_tool_calls(messages)

    assert [item.tool_call_id for item in recovered] == ["c2"]


def test_recover_does_not_guess_when_the_tail_is_not_a_tool_batch() -> None:
    messages = [
        HumanMessage(content="改"),
        _dangling(),
        HumanMessage(content="算了，换个问题"),
    ]

    assert recover_dangling_tool_calls(messages) == []


def test_recover_is_a_noop_for_a_complete_history() -> None:
    messages = [
        HumanMessage(content="改"),
        _dangling(("c1",)),
        ToolMessage(content="已完成", tool_call_id="c1"),
        AIMessage(content="做完了"),
    ]

    assert recover_dangling_tool_calls(messages) == []
