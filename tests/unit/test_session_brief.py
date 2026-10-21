"""会话约束摘要：只保留人/助手文本、有界、可关闭。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent.session_brief import build_session_brief


def test_only_human_and_assistant_text_is_kept() -> None:
    messages = [
        HumanMessage(content="不要改测试文件"),
        AIMessage(content="", tool_calls=[{"name": "file_read", "args": {}, "id": "1"}]),
        ToolMessage(content="源码内容", tool_call_id="1"),
        AIMessage(content="明白，只改源码。"),
    ]

    brief = build_session_brief(messages, limit=1_000)

    assert "[用户] 不要改测试文件" in brief
    assert "[助手] 明白，只改源码。" in brief
    assert "源码内容" not in brief


def test_the_summary_keeps_the_most_recent_items_and_caps_length() -> None:
    messages = [
        HumanMessage(content=f"第{i}条约束" + "x" * 100)
        for i in range(30)
    ]

    brief = build_session_brief(messages, limit=800)

    assert len(brief) <= 820
    assert "第29条约束" in brief
    assert "第0条约束" not in brief


def test_zero_limit_disables_the_brief() -> None:
    assert build_session_brief([HumanMessage(content="约束")], limit=0) == ""
