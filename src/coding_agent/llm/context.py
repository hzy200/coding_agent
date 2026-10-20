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

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    ToolMessage,
)

# 硬裁剪档位：先降到 SOFT，仍超预算再降到 HARD
HARD_TOOL_CHARS = 300


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """字符/估算 token 双预算；都为 0 或 keep_recent<=0 表示不裁剪。"""

    max_chars: int = 60_000
    keep_recent: int = 12
    tool_chars: int = 1_500
    # 0 = 只按字符预算；>0 时按保守估算的 token 数再兜一层。
    max_tokens: int = 0

    @property
    def enabled(self) -> bool:
        return (
            (self.max_chars > 0 or self.max_tokens > 0)
            and self.keep_recent > 0
            and self.tool_chars > 0
        )


@dataclass(slots=True)
class TrimReport:
    trimmed: int = 0
    before_chars: int = 0
    after_chars: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
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


def estimate_tokens(text: str) -> int:
    """保守 token 估算：CJK 约 1 字/token，其余约 4 字符/token。

    不引 tokenizer：这里要的是预算护栏，不是计费精确值；真正的 usage
    仍以 provider 回报为准。
    """
    if not text:
        return 0
    cjk = sum(
        1
        for char in text
        if "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
    )
    # 非 CJK 部分向上取整：1–3 个字符也算 1 token；纯中文不应凭空多出 1。
    return cjk + (len(text) - cjk + 3) // 4


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def message_tokens(message: AnyMessage) -> int:
    return estimate_tokens(_content_text(message.content))


def total_tokens(messages: list[AnyMessage]) -> int:
    return sum(message_tokens(m) for m in messages)


def _over_budget(messages: list[AnyMessage], budget: ContextBudget) -> bool:
    if budget.max_chars > 0 and total_chars(messages) > budget.max_chars:
        return True
    return budget.max_tokens > 0 and total_tokens(messages) > budget.max_tokens


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


def _cap_single(message: AnyMessage, budget: ContextBudget) -> tuple[AnyMessage, bool]:
    """单条消息的绝对上限（只按字符预算时生效）。"""
    if budget.max_chars <= 0 or message_chars(message) <= budget.max_chars:
        return message, False
    return _shorten(message, budget.max_chars)


def _is_trimmable(message: AnyMessage) -> bool:
    """工具结果是大头；助手的长篇总结次之。

    带 tool_calls 的助手消息不动 —— 它的 content 通常很短，而且结构敏感。
    """
    if isinstance(message, ToolMessage):
        return True
    return isinstance(message, AIMessage) and not getattr(message, "tool_calls", None)


_CONTEXT_ERROR_HINTS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
)


def is_context_length_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(hint in text for hint in _CONTEXT_ERROR_HINTS)


def tighter_budget(budget: ContextBudget) -> ContextBudget:
    """context-length 400 后的降档预算。"""
    return ContextBudget(
        max_chars=max(1_000, budget.max_chars // 2)
        if budget.max_chars > 0
        else 0,
        max_tokens=max(1_000, budget.max_tokens // 2)
        if budget.max_tokens > 0
        else 0,
        keep_recent=max(2, budget.keep_recent // 2),
        tool_chars=max(100, budget.tool_chars // 2),
    )


def summarize_messages(
    messages: list[AnyMessage], *, max_chars: int = 1_500
) -> str:
    """从消息历史生成确定性压缩摘要（不调模型）。

    重点保留：用户目标、最近结论、最近工具动作。它不能替代完整历史，
    但能在裁剪掉旧工具结果后，让模型仍知道“之前做过什么/为什么”。
    """
    users: list[str] = []
    answers: list[str] = []
    actions: list[str] = []
    for message in messages:
        text = _content_text(message.content).strip()
        if isinstance(message, HumanMessage) and text:
            users.append(" ".join(text.split())[:300])
        elif (
            isinstance(message, AIMessage)
            and not getattr(message, "tool_calls", None)
            and text
        ):
            answers.append(" ".join(text.split())[:300])
        for call in getattr(message, "tool_calls", None) or []:
            name = str(call.get("name", ""))
            args = call.get("args") or {}
            target = (
                args.get("path")
                or args.get("command")
                or args.get("query")
                or ""
            )
            actions.append(f"{name}({str(target)[:120]})")

    parts: list[str] = []
    if users:
        parts.append("用户目标：" + " / ".join(users[-3:]))
    if answers:
        parts.append("最近结论：" + " / ".join(answers[-3:]))
    if actions:
        parts.append("最近工具动作：" + "；".join(actions[-12:]))
    summary = "\n".join(parts)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "…（摘要已截断）"
    return summary


def trim_messages(
    messages: list[AnyMessage], budget: ContextBudget
) -> tuple[list[AnyMessage], TrimReport]:
    """返回裁剪后的消息列表与报告。原列表不会被就地修改。

    两阶段：
    1. 老的 trimmable 消息降到 `tool_chars`；最近窗口只做单条上限；
    2. 如果总字符/估算 token 仍超预算，**最近窗口也参与降档**，
       从老到新依次降到 HARD/120/40/占位说明。消息结构始终保留。
    """
    report = TrimReport(
        before_chars=total_chars(messages),
        before_tokens=total_tokens(messages),
    )
    if not budget.enabled:
        report.after_chars = report.before_chars
        report.after_tokens = report.before_tokens
        return list(messages), report

    cutoff = max(0, len(messages) - budget.keep_recent)
    trimmed: list[AnyMessage] = []

    for index, message in enumerate(messages):
        if not _is_trimmable(message):
            trimmed.append(message)
            continue
        limit = budget.tool_chars if index < cutoff else budget.max_chars
        if limit <= 0:
            trimmed.append(message)
            continue
        new_message, changed = _shorten(message, limit)
        report.trimmed += int(changed)
        trimmed.append(new_message)

    if _over_budget(trimmed, budget):
        report.escalated = True
        order = [*range(cutoff), *range(cutoff, len(trimmed))]
        for limit in (HARD_TOOL_CHARS, 120, 40, 0):
            for index in order:
                message = trimmed[index]
                if not _is_trimmable(message):
                    continue
                new_message, changed = _shorten(message, limit)
                if changed:
                    report.trimmed += 1
                    trimmed[index] = new_message
            if not _over_budget(trimmed, budget):
                break

    report.after_chars = total_chars(trimmed)
    report.after_tokens = total_tokens(trimmed)
    return trimmed, report
