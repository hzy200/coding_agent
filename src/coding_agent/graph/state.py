"""图状态定义。

只放**必须跨节点传递**的东西。字段的生命周期（谁写入、谁重置）见
`docs/ARCHITECTURE.md` 第 5 节 —— 重置漏了会让状态跨步骤串味。
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
    # 跨会话的项目事实，每次 run 从记忆文件刷新后注入系统提示
    memories: list[str]

    # 计划与步进
    plan: list[str]
    step_idx: int

    # 当前步骤的用量与结果 —— 全部由 planner / advance / repair 按步重置
    tool_rounds: int
    budget_exhausted: bool
    # 本步是否改过文件/依赖：没改动就不必跑验证
    dirty: bool
    # 最近一次验证结果（VerifyResult 的 model_dump）
    verification: dict[str, Any]
    # 当前步骤已修复次数
    retry: int

    # call_id -> auto | approved | denied，由 approval_gate 写入、tools 消费后清空
    approvals: dict[str, str]
