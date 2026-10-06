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
    # planner 没解析出计划、退化成「整条请求当一步」时为 True。
    # **每次 planner 都要写**（正常时也写 False）：只写 True 的话，
    # 上一轮的降级标记会串到这一轮，把一次正常规划误报成降级。
    plan_degraded: bool
    # 「修复用尽后换做法」的重规划次数（上限 max_replans）。
    # 由 planner 清零、replan 的**失败入口**递增；advance 不动它 ——
    # 它是整个任务的额度，不是每步的。
    # 与 tweak_count **必须分开计数**，理由见 graph/nodes/replan.py 顶部。
    replan_count: int
    # 「一步做完后微调剩余步骤」的次数（上限同为 max_replans）。
    # 独立于 replan_count：微调是锦上添花，换做法是救命稻草，
    # 共用额度会让无害的微调把「失败后换做法」的机会耗光。
    tweak_count: int

    # 当前步骤的用量与结果 —— 全部由 planner / advance / repair 按步重置
    tool_rounds: int
    budget_exhausted: bool
    # 本步是否改过文件/依赖：没改动就不必跑验证
    dirty: bool
    # 本步是否已经因「一个改动都没产生」被要求重做过一次（上限一次，防死循环）。
    # 由 planner / advance / repair 复位，nudge 置位。
    empty_step_nudged: bool
    # 最近一次验证结果（VerifyResult 的 model_dump）
    verification: dict[str, Any]
    # 最近一次代码审查结果（ReviewResult 的 model_dump）。
    # 生命周期与 verification 一致：planner / advance 清空，**repair 刻意不清** ——
    # repair 之后的 act 要靠它知道"审查到底不满意什么"，清了就只能瞎改。
    review: dict[str, Any]
    # 已审过的最大 snapshot_id。
    #
    # **只由 planner 归零，跨步骤保留**（advance / repair 都不许碰）。原因很隐蔽：
    # 留底记的是**写前内容**，所以任何被改过的文件都会永远与自己的留底不同 ——
    # 水位线一旦被逐步清零，每一步都会把前面所有步骤的改动重审一遍，同一个
    # 早就处理过的问题反复告警，直到把修复预算耗光。
    review_watermark: str
    # 当前步骤已修复次数
    retry: int

    # call_id -> auto | approved | denied，由 approval_gate 写入、tools 消费后清空
    approvals: dict[str, str]
