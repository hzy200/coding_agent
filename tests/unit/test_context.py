"""上下文裁剪。

最重要的是那条不变量：**只截断内容，绝不丢弃消息**。
丢消息会破坏 tool_calls ↔ tool_call_id 配对，下一轮 API 直接报错 ——
这是 W2 踩过并专门立过不变量的坑。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from coding_agent.llm.context import (
    HARD_TOOL_CHARS,
    ContextBudget,
    total_chars,
    trim_messages,
)

BUDGET = ContextBudget(max_chars=2_000, keep_recent=4, tool_chars=100)


def _tool_call(call_id: str, content: str = "") -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[{"name": "shell_exec", "args": {}, "id": call_id}],
    )


def _tool_result(call_id: str, size: int) -> ToolMessage:
    return ToolMessage(content="x" * size, tool_call_id=call_id, name="shell_exec")


def _history(pairs: int, size: int = 5_000) -> list:
    messages: list = [HumanMessage(content="开始")]
    for i in range(pairs):
        messages.append(_tool_call(f"call-{i}"))
        messages.append(_tool_result(f"call-{i}", size))
    return messages


# ---------------- 不变量 ----------------

def test_never_drops_messages() -> None:
    messages = _history(6)
    trimmed, _ = trim_messages(messages, BUDGET)
    assert len(trimmed) == len(messages)


def test_preserves_message_types_and_order() -> None:
    messages = _history(6)
    trimmed, _ = trim_messages(messages, BUDGET)
    assert [type(m).__name__ for m in trimmed] == [type(m).__name__ for m in messages]


def test_preserves_tool_call_pairing() -> None:
    """裁剪后每个 tool_call 仍然有对应的 tool_call_id 消息。"""
    messages = _history(6)
    trimmed, _ = trim_messages(messages, BUDGET)

    pending: list[str] = []
    for message in trimmed:
        for call in getattr(message, "tool_calls", None) or []:
            pending.append(call["id"])
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
    assert pending == []


def test_does_not_mutate_the_original_list() -> None:
    messages = _history(4)
    original = [m.content for m in messages]
    trim_messages(messages, BUDGET)
    assert [m.content for m in messages] == original


# ---------------- 何时不裁 ----------------

def test_disabled_budget_is_a_noop() -> None:
    messages = _history(6)
    trimmed, report = trim_messages(messages, ContextBudget(max_chars=0))
    assert trimmed == messages
    assert not report.changed


def test_short_history_is_untouched() -> None:
    messages = _history(1)
    trimmed, report = trim_messages(messages, BUDGET)
    assert [m.content for m in trimmed] == [m.content for m in messages]
    assert not report.changed


def test_small_messages_are_untouched() -> None:
    messages = _history(6, size=10)
    roomy = ContextBudget(max_chars=10_000_000, keep_recent=1, tool_chars=100)
    trimmed, _ = trim_messages(messages, roomy)
    assert [m.content for m in trimmed] == [m.content for m in messages]


# ---------------- 裁剪行为 ----------------

def test_recent_window_is_never_trimmed() -> None:
    messages = _history(6)
    trimmed, _ = trim_messages(messages, BUDGET)
    recent = BUDGET.keep_recent
    for original, result in zip(messages[-recent:], trimmed[-recent:], strict=True):
        assert original.content == result.content


def test_old_tool_results_are_shortened() -> None:
    messages = _history(6)
    trimmed, report = trim_messages(messages, BUDGET)
    assert report.changed
    assert report.trimmed > 0
    assert report.after_chars < report.before_chars
    assert "已裁剪" in trimmed[2].content


def test_truncation_keeps_the_head_of_the_content() -> None:
    """保留开头：定位信息通常在前几行。"""
    message = ToolMessage(content="HEADER\n" + "y" * 5_000, tool_call_id="c", name="t")
    messages = [HumanMessage(content="x")] * 5 + [message]
    tight = ContextBudget(max_chars=100, keep_recent=1, tool_chars=50)
    trimmed, _ = trim_messages(messages, tight)
    assert trimmed[-1].content.startswith("HEADER")


def test_assistant_summary_is_trimmed_but_tool_calls_are_not() -> None:
    long_summary = AIMessage(content="z" * 5_000)
    with_calls = _tool_call("c1", content="short")
    messages = [HumanMessage(content="x")] * 5 + [long_summary, with_calls]
    tight = ContextBudget(max_chars=100, keep_recent=1, tool_chars=50)
    trimmed, _ = trim_messages(messages, tight)

    assert "已裁剪" in trimmed[-2].content
    # 带 tool_calls 的消息内容短且结构敏感，不该动
    assert trimmed[-1].content == "short"
    assert trimmed[-1].tool_calls == with_calls.tool_calls


def test_escalates_when_soft_limit_is_not_enough() -> None:
    """降档到硬上限才对 —— 否则裁剪完仍然超预算，等于没裁。"""
    messages = _history(20, size=10_000)
    tight = ContextBudget(max_chars=1_000, keep_recent=2, tool_chars=5_000)
    trimmed, report = trim_messages(messages, tight)

    assert report.escalated
    old_tools = [m for m in trimmed[2:-2] if isinstance(m, ToolMessage)]
    assert old_tools
    assert all(len(m.content) <= HARD_TOOL_CHARS + 100 for m in old_tools)


def test_report_is_accurate() -> None:
    messages = _history(6)
    trimmed, report = trim_messages(messages, BUDGET)
    assert report.before_chars == total_chars(messages)
    assert report.after_chars == total_chars(trimmed)
    assert report.after_chars < report.before_chars


def test_system_message_at_the_front_is_untouched() -> None:
    messages = [SystemMessage(content="s" * 5_000)] + _history(6)
    trimmed, _ = trim_messages(messages, BUDGET)
    assert trimmed[0].content == "s" * 5_000


def test_non_string_content_does_not_crash() -> None:
    message = AIMessage(content=[{"type": "text", "text": "x" * 5_000}])
    messages = [HumanMessage(content="q")] * 5 + [message]
    tight = ContextBudget(max_chars=100, keep_recent=1, tool_chars=50)
    trimmed, _ = trim_messages(messages, tight)
    assert len(trimmed) == len(messages)
