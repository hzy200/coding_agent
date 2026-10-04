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
from typing import Any

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


def _content_text_len(content: Any) -> int:
    """content 里的**真实文本**长度。

    `str` 直接算；content blocks 列表只累加 `type == "text"` 的文本块
    —— 用 `len(str(list))` 会连结构符号一起算，虚高得离谱。
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                total += len(str(block.get("text", "")))
        return total
    return len(str(content))


def message_chars(message: AnyMessage) -> int:
    return _content_text_len(message.content)


def total_chars(messages: list[AnyMessage]) -> int:
    return sum(message_chars(m) for m in messages)


def _omission_note(omitted: int) -> str:
    return f"\n…（此处省略 {omitted} 字符，内容已裁剪以控制上下文长度）"


def _truncate_blocks(blocks: list[Any], limit: int) -> list[Any]:
    """按顺序保留前 `limit` 个字符的文本，其余省略；非文本块原样保留。"""
    out: list[Any] = []
    remaining = limit
    omitted = 0
    truncated_at: int | None = None

    def keep_text(text: str, is_block: bool, block: Any) -> None:
        nonlocal remaining, omitted, truncated_at
        if remaining <= 0:
            omitted += len(text)
            return
        if len(text) <= remaining:
            out.append(block if is_block else text)
            remaining -= len(text)
            return
        kept = text[:remaining]
        if is_block:
            new_block = dict(block)
            new_block["text"] = kept
            out.append(new_block)
        else:
            out.append(kept)
        omitted += len(text) - remaining
        truncated_at = len(out) - 1
        remaining = 0

    for block in blocks:
        if isinstance(block, str):
            keep_text(block, False, block)
        elif isinstance(block, dict) and block.get("type") == "text":
            keep_text(str(block.get("text", "")), True, block)
        else:
            out.append(block)  # 图片等非文本块原样保留

    if omitted:
        note = _omission_note(omitted)
        if truncated_at is None:
            out.append(note)  # 恰好卡在块边界、没有可附着的文本块
        elif isinstance(out[truncated_at], str):
            out[truncated_at] += note
        else:
            patched = dict(out[truncated_at])
            patched["text"] = str(patched.get("text", "")) + note
            out[truncated_at] = patched
    return out


def _shorten(message: AnyMessage, limit: int) -> tuple[AnyMessage, bool]:
    """把超长的内容换成「开头 + 截断说明」。消息对象本身保留。"""
    content = message.content
    if isinstance(content, str):
        if len(content) <= limit:
            return message, False
        return message.model_copy(
            update={"content": content[:limit] + _omission_note(len(content) - limit)}
        ), True
    if isinstance(content, list):
        if _content_text_len(content) <= limit:
            return message, False
        return message.model_copy(update={"content": _truncate_blocks(content, limit)}), True
    return message, False


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
