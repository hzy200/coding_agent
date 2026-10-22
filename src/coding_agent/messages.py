"""消息处理辅助。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)


def text_of(message: object) -> str:
    """取出消息的纯文本内容。

    模型可能返回 str，也可能返回 content blocks 列表，这里统一成字符串。
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def last_human_text(messages: Sequence[BaseMessage]) -> str:
    """最近一条用户输入。

    必须是**最后**一条而不是第一条：启用持久化 checkpoint 后，同一 thread_id
    的消息列表会跨轮次累积，取第一条会拿到上一轮、甚至很久以前的请求。
    """
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return text_of(message)
    return ""


def unanswered_tool_calls(
    messages: Sequence[BaseMessage],
) -> list[dict]:
    """返回尾部 tool_calls 批次里仍未被 ToolMessage 应答的调用。

    只在**尾部的 tool_calls 批次**上工作：如果它后面已经出现非 ToolMessage，
    说明不是一次中断留下的尾巴，不做猜测。
    """
    if not messages:
        return []

    start = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage) and getattr(
            message, "tool_calls", None
        ):
            start = index
            break
    if start < 0:
        return []

    tail = messages[start + 1 :]
    if any(not isinstance(message, ToolMessage) for message in tail):
        return []

    answered = {
        str(getattr(message, "tool_call_id", ""))
        for message in tail
        if isinstance(message, ToolMessage)
    }
    return [
        call
        for call in messages[start].tool_calls or []
        if str(call.get("id", "")) and str(call.get("id", "")) not in answered
    ]


def recover_dangling_tool_calls(
    messages: Sequence[BaseMessage],
) -> list[ToolMessage]:
    """为崩溃前没来得及返回的 tool_calls 补占位结果。

    崩溃可能发生在 `act` 已提交（checkpoint 里有带 tool_calls 的 AIMessage）、
    `tools` 尚未提交之间。此时同一 thread 重新运行会把新消息追加在悬空
    tool_calls 后面，OpenAI 兼容接口会因为「assistant tool_calls 没有对应
    tool 消息」直接报错。

    返回的占位消息由调用方插到新用户消息之前，从而恢复合法的消息顺序。
    """
    recovered: list[ToolMessage] = []
    for call in unanswered_tool_calls(messages):
        call_id = str(call.get("id", ""))
        recovered.append(
            ToolMessage(
                content=(
                    "上次运行在工具执行完成前中断；该调用的结果未知。"
                    "不要假设它已成功：只读操作可以重新执行，写操作请先"
                    "读取文件当前内容确认状态。"
                ),
                tool_call_id=call_id,
                name=str(call.get("name", "")),
                artifact={
                    "ok": False,
                    "interrupted": True,
                    "recovered": True,
                    "tool": str(call.get("name", "")),
                },
            )
        )
    return recovered
