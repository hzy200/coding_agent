"""上下文裁剪。

长会话的变化主要来自**工具结果**：一次 `file_read` 可能就是几千字符，多步任务
下来轻松堆到几十万字符。

**唯一的不变量：只截断内容，绝不丢弃消息。**

丢弃消息会破坏 `tool_calls` ↔ `tool_call_id` 的配对，下一轮 OpenAI 兼容接口
直接报错 —— 这是 W2 就踩过并专门立过不变量的坑。截断内容不动结构，
配对永远是完整的。
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

# 硬裁剪档位：先降到 SOFT，仍超预算再降到 HARD
HARD_TOOL_CHARS = 300


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """0 或 keep_recent<=0 表示不裁剪。"""

    max_chars: int = 60_000
    keep_recent: int = 12
    tool_chars: int = 1_500

    @property
    def enabled(self) -> bool:
        return self.max_chars > 0 and self.keep_recent > 0 and self.tool_chars > 0


@dataclass(slots=True)
class TrimReport:
    trimmed: int = 0
    before_chars: int = 0
    after_chars: int = 0
    escalated: bool = False

    @property
    def changed(self) -> bool:
        return self.trimmed > 0


def message_chars(message: AnyMessage) -> int:
    content = message.content
    if isinstance(content, str):
        return len(content)
    return len(str(content))


def total_chars(messages: list[AnyMessage]) -> int:
    return sum(message_chars(m) for m in messages)


def _shorten(message: AnyMessage, limit: int) -> tuple[AnyMessage, bool]:
    """把超长的内容换成「开头 + 截断说明」。消息对象本身保留。"""
    content = message.content
    if not isinstance(content, str) or len(content) <= limit:
        return message, False

    shortened = (
        f"{content[:limit]}\n"
        f"…（此处省略 {len(content) - limit} 字符，内容已裁剪以控制上下文长度）"
    )
    return message.model_copy(update={"content": shortened}), True


def _is_trimmable(message: AnyMessage) -> bool:
    """工具结果是大头；助手的长篇总结次之。

    带 tool_calls 的助手消息不动 —— 它的 content 通常很短，而且结构敏感。
    """
    if isinstance(message, ToolMessage):
        return True
    return isinstance(message, AIMessage) and not getattr(message, "tool_calls", None)


def trim_messages(
    messages: list[AnyMessage], budget: ContextBudget
) -> tuple[list[AnyMessage], TrimReport]:
    """返回裁剪后的消息列表与报告。原列表不会被就地修改。"""
    report = TrimReport(before_chars=total_chars(messages))
    if not budget.enabled or len(messages) <= budget.keep_recent:
        report.after_chars = report.before_chars
        return list(messages), report

    cutoff = len(messages) - budget.keep_recent
    trimmed: list[AnyMessage] = []

    for index, message in enumerate(messages):
        if index >= cutoff or not _is_trimmable(message):
            trimmed.append(message)
            continue
        new_message, changed = _shorten(message, budget.tool_chars)
        report.trimmed += int(changed)
        trimmed.append(new_message)

    # 降档仍然超预算时，对老消息再狠一点
    if total_chars(trimmed) > budget.max_chars:
        harsher: list[AnyMessage] = []
        for index, message in enumerate(trimmed):
            if index >= cutoff or not _is_trimmable(message):
                harsher.append(message)
                continue
            new_message, changed = _shorten(message, HARD_TOOL_CHARS)
            report.trimmed += int(changed)
            harsher.append(new_message)
        trimmed = harsher
        report.escalated = True

    report.after_chars = total_chars(trimmed)
    return trimmed, report
