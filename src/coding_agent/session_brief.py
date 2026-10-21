"""把多轮会话压缩成给编排子图/worker 的约束摘要。

独立 orchestrator 子图刻意不复制父图的 `messages`（见
`orchestrator/state.py`）——checkpoint 里塞整段历史既昂贵又会让子图与主图
产生两套事实。但完全不传历史又会丢掉用户在前几轮说过的持久约束
（「不要改测试」「沿用上次的接口」）。

这里取一个折中：宿主从 checkpoint 里抽最近若干轮的人/助手文本，压成
**有界、确定性**的摘要，作为 `session_brief` 显式传入子图。它只读已有
状态，不额外调用模型。
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from coding_agent.messages import text_of

# 单条消息的上限。约束通常很短，长消息多是粘贴的代码/日志，截断即可。
_MAX_ITEM_CHARS = 600
# 最多保留最近多少条「人 + 助手」消息（按消息条数，不是轮数）。
_MAX_ITEMS = 16


def build_session_brief(
    messages: Sequence[BaseMessage],
    *,
    limit: int,
) -> str:
    """从历史消息生成会话约束摘要；`limit <= 0` 时关闭。"""
    if limit <= 0:
        return ""

    lines: list[str] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            role = "用户"
        elif isinstance(message, AIMessage) and not getattr(
            message, "tool_calls", None
        ):
            role = "助手"
        else:
            # 工具调用与工具结果属于过程噪声，不是约束。
            continue
        text = " ".join(text_of(message).split())
        if not text:
            continue
        lines.append(f"[{role}] {text[:_MAX_ITEM_CHARS]}")

    lines = lines[-_MAX_ITEMS:]
    if not lines:
        return ""

    body = "\n".join(lines)
    if len(body) > limit:
        body = "…（更早的轮次已省略）\n" + body[-limit:]
    return body
