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
    # 本轮用户请求的纯文本副本。独立子图不复制整段 messages，只读这一项；
    # direct 路径仍以 messages 为准（此字段不参与规划）。
    request: str
    # 从 checkpoint 抽取的上一轮及更早对话摘要。独立子图拿不到完整 messages，
    # 用它携带「不要改测试」「沿用上次接口」这类持久约束；0 字符时为空。
    session_brief: str
    # 上一轮没有正常收尾时给模型的恢复提示（工作区可能有未验证改动）。
    recovery_notice: str
    # running / finished。respond 收尾时置 finished；异常/崩溃会把它留在
    # running，下次同一 thread 的新 run 据此触发恢复提示。
    run_status: str
    # 沙箱内的工作目录
    cwd: str
    # 跨会话的项目事实，每次 run 从记忆文件刷新后注入系统提示
    memories: list[str]
    # 本次运行中已被成功修改的文件。历史里对这些文件的读取结果可能已过期，
    # act 用它生成「请重新读取」提示；planner 在 run 开始时清空。
    stale_paths: list[str]
    # 分层项目指令（AGENTS.md / CLAUDE.md）合并后的文本，run 开始时加载。
    project_instructions: str

    # 计划与步进
    plan: list[str]
    # 与 plan 逐项对齐的步骤类型：explore / implement / verify / unknown。
    # planner 初始化，replan 同步改写剩余部分；advance 不碰。
    plan_kinds: list[str]
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
    # 本次节点内因瞬时错误触发的重试次数（runtime 累加后写进 run_end 审计）。
    # 不是页面上的进度，但事后要能回答「这一轮是不是一直在抖动」。
    transient_retries: int
    # 本步是否改过文件/依赖：没改动就不必跑验证
    dirty: bool
    # 整个任务是否产生过任何改动。与 `dirty` 的区别：后者每步重置，
    # 前者只由 planner 归零。用于收尾时防止「一次都没写却声称已完成」。
    run_dirty: bool
    # 编排层合并过补丁、但主 agent 还没跑过一次自动验证。
    #
    # 合并发生在主 planner 之前，而 planner 会按步重置 `dirty`；若只看 `dirty`，
    # 「主 agent 只读/只总结」就会让合并进来的改动完全绕过 verify。这个标志
    # 跨步骤保留，直到 verify 真正执行（或在 verify 被禁用时显式清除）。
    pending_verification: bool
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
    # 连续出现**同一失败签名**的次数。verify 写入；repair 与 replan 都刻意不清
    # （清了就检测不到原地打转）；只有 planner / advance（换了步骤）归零。
    same_failure_count: int
    # 上一次失败签名。replan 会清空 `verification`（旧结论针对旧做法），
    # 但停滞检测需要跨做法比较，所以单独留一份。
    last_failure_signature: str
    # 整个运行最后一次真实执行过的验证结果。replan 会清空 `verification`，
    # 但收尾时必须知道「最近一次验证到底过没过」，否则会声称成功。
    last_verification: dict[str, Any]

    # 本次运行的仓库画像与委派策略（runtime 在 run() 时注入）。
    # repo_profile: {"file_count": int, "test_file_count": int}
    repo_profile: dict[str, int]
    # 小仓库或非 explore 步直接不允许委派；预算按仓库规模分级。
    delegation_allowed: bool
    delegation_budget: int

    # 整个任务已委派给子智能体的次数（上限 max_delegate_calls）。
    # 由 planner 归零、tools 递增；advance / repair 不动 —— 它是任务级预算，
    # 不是每步的。与 tool_rounds 相互独立：一次委派只占主 agent 一轮工具预算。
    delegate_calls: int

    # ---------- Orchestrator（Phase 3A）----------
    # runtime 注入：direct | orchestrated
    execution_mode: str
    # select_workflow 写入：direct | parallel_readonly
    workflow: str
    workflow_reason: str
    # survey 写入：文件数、测试文件数、顶层目录、测试命令
    repo_summary: dict[str, Any]
    # select_workflow 在规则不确定时一次返回的预取任务；plan_tasks 直接消费。
    prefetched_tasks: list[dict[str, Any]]
    prefetched_degraded: bool
    # plan_tasks 写入：TaskSpec 列表与状态
    tasks: list[dict[str, Any]]
    task_status: dict[str, str]
    # task_id -> 已尝试次数（失败重试用）
    task_attempts: dict[str, int]
    # dispatch 追加：每个 worker 的结构化结果（含 attempt）
    worker_results: list[dict[str, Any]]
    orchestrator_round: int
    orchestrator_degraded: bool
    # synthesize 写入：交给主 agent 的压缩汇总与计数
    orchestrator_summary: str
    orchestrator_completed: int
    orchestrator_failed: int
    worker_context: str
    # 最近一次 act/respond 的上下文裁剪报告（字符/估算 token）。
    context_trim: dict[str, Any]
    # fan-in 的结构化产物索引；完整结果在 .agent/artifacts/ 下。
    worker_brief: str
    orchestration_artifacts: list[dict[str, Any]]
    # fan-in 的结构化交接（任务级状态、文件、发现、artifact 路径、预算）。
    # 与 `worker_context` 的区别：后者是给模型读的压缩文本，这里是供宿主/
    # 工具/前端按字段消费的稳定结构。
    orchestration_handoff: dict[str, Any]
    # Phase 4D：worker 提议的只读任务扩展
    # 已扫描到 worker_results 的哪个位置（避免重复采纳同一批提议）
    task_extensions_seen: int
    # extend_tasks 本轮实际追加的任务（runtime 翻译成事件）
    task_extensions_applied: list[dict[str, Any]]
    # 被拒绝的提议原因（只记录，不阻断运行）
    task_extension_errors: list[str]

    # call_id -> auto | approved | denied，由 approval_gate 写入、tools 消费后清空
    approvals: dict[str, str]

    # ---------- Steering（Phase 3D）----------
    # 用户取消：路由直接去收尾，不再开始新的 worker/步骤。
    steering_cancelled: bool
    # 用户要求重规划剩余工作（orchestrated 回到 plan_tasks；direct 去 replan）。
    steering_replan: bool
    # 用户显式重规划的次数（上限复用 max_replans），防止 /replan 无限续命。
    steering_replans: int
    # 本轮已消费的指令（runtime 翻译成 SteeringApplied 事件）
    steering_applied: list[dict[str, Any]]
    # plan_tasks 在用户重规划后写入，runtime 翻译成 WorkflowRevised
    workflow_revised_reason: str
