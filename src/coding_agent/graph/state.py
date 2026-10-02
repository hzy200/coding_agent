"""图状态定义。

字段在整个四个月中逐步启用，当前阶段只用到 messages / cwd / tool_rounds。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    # 对话与工具调用轨迹，add_messages 负责追加与去重
    messages: Annotated[list[AnyMessage], add_messages]
    # 沙箱内的工作目录
    cwd: str
    # 已发生的模型轮次，用于限制工具循环
    tool_rounds: int
    # 当前步骤是否因工具预算耗尽而被 act 强制收尾
    budget_exhausted: bool
    # --- 以下字段在后续里程碑启用 ---
    plan: list[str]              # W2  planner
    step_idx: int                # W2  planner
    retry: int                   # W10 repair
    approvals: list[dict[str, Any]]  # W5 审批记录
    snapshot_id: str | None      # W7 回滚快照
