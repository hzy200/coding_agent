"""消息处理辅助。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage


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
