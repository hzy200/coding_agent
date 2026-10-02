from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from coding_agent.messages import last_human_text, text_of


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
