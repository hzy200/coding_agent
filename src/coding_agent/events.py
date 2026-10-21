"""领域事件：前端与编排层之间的唯一契约。

前端（CLI / TUI / Web）只消费这些事件，不认识 LangGraph、不认识工具实现。
所有安全判定与编排都发生在 AgentRuntime 内部，前端无法绕过。

事件全部是 pydantic 模型，`model_dump()` 出来即可直接作为 SSE 数据帧。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str = ""


# --------------------------------------------------------------------------
# 规划与步进
# --------------------------------------------------------------------------

class PlanCreated(Event):
    """planner 完成分解。

    `degraded` 为真表示**规划其实失败了**：模型没给出可解析的步骤，
    已退化成「整条请求当成一步」。这时 `steps` 只有一项、就是请求原文，
    后面不会再有真正的多步推进。前端必须把它显示出来 ——
    静默降级的话，一次完全失效的规划看起来和正常工作一模一样。
    """

    type: Literal["plan_created"] = "plan_created"
    steps: list[str] = Field(default_factory=list)
    # 与 steps 逐项对齐：explore / implement / verify / unknown
    kinds: list[str] = Field(default_factory=list)
    degraded: bool = False


class PlanRevised(Event):
    """执行中重建了剩余计划（planner 只在开局跑一次，之后靠它修正）。

    `steps` 是**完整的**新计划（含已完成的部分，它们不会被改动），
    `step_idx` 是当前所处步骤，前端据此重新渲染计划面板。
    修订次数有硬上限（`AGENT_MAX_REPLANS`）。
    """

    type: Literal["plan_revised"] = "plan_revised"
    steps: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    step_idx: int = 0


class StepStarted(Event):
    """进入某个子任务。total == 1 时前端可不渲染步进标题。"""

    type: Literal["step_started"] = "step_started"
    index: int = 0
    total: int = 1
    text: str = ""


class StepFinished(Event):
    """当前子任务收尾（模型给出了小结，或工具预算耗尽被强制叫停）。

    `cancelled` 表示这一步**根本没机会执行完**：重规划判定它做不成、把剩余步骤
    砍掉了，于是路由直接去收尾。它与 `budget_exhausted` 的区别是——后者是"试过
    但工具轮次用完"，前者是"还没试就被放弃了"。前端需要区分，否则用户会以为
    模型已经尝试过。
    """

    type: Literal["step_finished"] = "step_finished"
    index: int = 0
    budget_exhausted: bool = False
    cancelled: bool = False
    text: str = ""


# --------------------------------------------------------------------------
# 生成与执行
# --------------------------------------------------------------------------

class AssistantToken(Event):
    """流式吐字。node 标明来自 act（步骤小结）还是 respond（最终答复）。"""

    type: Literal["assistant_token"] = "assistant_token"
    node: str = ""
    text: str = ""


class ToolCallStarted(Event):
    """模型请求了一次工具调用，即将执行。

    level 供前端着色，不是安全判定。call_id 用于和 ToolCallFinished 配对 ——
    同一轮可能有多个并行工具调用，只靠顺序无法可靠对应。
    """

    type: Literal["tool_call_started"] = "tool_call_started"
    call_id: str = ""
    name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    level: str | None = None


class ToolCallFinished(Event):
    """工具执行完毕。

    ok/rejected 语义：rejected 表示被安全策略拦下（此时 exit_code 为 None）；
    两者都为 False 表示执行了但失败。文件工具没有退出码，exit_code 恒为 None。
    """

    type: Literal["tool_call_finished"] = "tool_call_finished"
    call_id: str = ""
    name: str = ""
    ok: bool = False
    rejected: bool = False
    # 环境不具备该能力（例如未配置 LSP）：不是成功，也不该计入失败率。
    skipped: bool = False
    # auto / approved / denied / rejected —— 审计据此区分「谁拒的」
    decision: str = "auto"
    exit_code: int | None = None
    duration_ms: int | None = None
    level: str | None = None
    preview: str = ""


class FileChanged(Event):
    """文件被创建/覆盖/编辑。只有真正改动文件的动作才会发这个事件。"""

    type: Literal["file_changed"] = "file_changed"
    path: str = ""
    action: str = ""
    added: int = 0
    removed: int = 0
    diff: str = ""
    snapshot_id: str | None = None


class Verification(Event):
    """一步做完后自动跑验证的结果。

    status: ok / failed（跑过）、error（验证环境异常，没跑起来）、
    not_configured（没有可用的测试命令）、skipped（没跑）。
    """

    type: Literal["verification"] = "verification"
    status: str = "skipped"
    command: str = ""
    # 三态：True 通过 / False 失败 / **None 没验证**（`not_configured` / `skipped`）。
    # 与审计里的 `ok` 刻意保持同源同形 —— 曾经两边口径不同，同一次「没跑验证」
    # 在事件与审计里结论相反，事后对账对不上（见 BUG_AUDIT 的 B7）。
    ok: bool | None = None
    summary: str = ""
    issues: list[str] = Field(default_factory=list)
    # 解析出的失败测试 id，供前端/评测直接展示与统计
    failed_tests: list[str] = Field(default_factory=list)


class ReviewFinished(Event):
    """一步做完、验证通过后，代码审查的结果。

    与 `Verification` 分开而不是复用：审查回答的不是「行为对不对」，而是
    「代码干不干净」（调试残留、被改弱的断言、语法坏掉的分支、往 `.agent/` 里写）。
    合成一个事件会让「这一步到底卡在哪一关」看不出来。

    status: clean / warned / blocked / skipped
    """

    type: Literal["review_finished"] = "review_finished"
    status: str = "skipped"
    blocked: bool = False
    summary: str = ""
    findings: list[str] = Field(default_factory=list)


class RepairStarted(Event):
    """验证失败后开始第 N 次修复。"""

    type: Literal["repair_started"] = "repair_started"
    attempt: int = 1
    limit: int = 0
    summary: str = ""
    issues: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 子智能体
# --------------------------------------------------------------------------

class SubAgentStarted(Event):
    """主智能体把一次只读探索委派给了子智能体。

    与 `ToolCallStarted` 同时发出：delegate 本身也是一次工具调用。前端按
    `agent_id` 把后续的 `SubAgentFinished` 配对回来。
    """

    type: Literal["subagent_started"] = "subagent_started"
    agent_id: str = ""
    parent_call_id: str = ""
    role: str = "explore"
    goal: str = ""


class WorkflowSelected(Event):
    """编排器选择了一个工作流模板。"""

    type: Literal["workflow_selected"] = "workflow_selected"
    workflow: str = ""
    reason: str = ""


class TaskGraphCreated(Event):
    """编排器生成了任务图（可能已降级为单任务）。"""

    type: Literal["task_graph_created"] = "task_graph_created"
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    degraded: bool = False


class TaskGraphExtended(Event):
    """运行中由 worker 提议、host 校验后追加的只读任务（Phase 4D）。"""

    type: Literal["task_graph_extended"] = "task_graph_extended"
    added: list[dict[str, Any]] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)


class WorkerStarted(Event):
    """一个 worker 开始执行。

    Phase 4：由 dispatch 通过 LangGraph custom stream 实时发出，而不是等整批
    worker 结束后回放。`attempt` 便于前端区分重试。
    """

    type: Literal["worker_started"] = "worker_started"
    worker_id: str = ""
    task_id: str = ""
    role: str = ""
    goal: str = ""
    attempt: int = 1
    # 进入 ready 队列后等待并发槽位的时间。
    queue_wait_ms: int = 0


class WorkerFinished(Event):
    """一个 worker 结束；status: success / partial / failed / blocked。"""

    type: Literal["worker_finished"] = "worker_finished"
    worker_id: str = ""
    task_id: str = ""
    role: str = ""
    status: str = ""
    summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    # 该任务的第几次尝试（1 = 首次；>1 表示重试）
    attempt: int = 1
    # Phase 3C：implement worker 的补丁是否已合并 / 合并错误
    merged: bool = False
    merge_error: str = ""
    # worker 内部执行的工具调用数（审计里逐条落账）
    tool_calls: int = 0
    # ---- Phase 5：成本与审计指标 ----
    queue_wait_ms: int = 0
    llm_ms: int = 0
    tool_ms: int = 0
    timed_out: bool = False
    artifact_id: str = ""
    depth: int = 0
    priority: int = 0
    scope_violations: list[str] = Field(default_factory=list)


class OrchestrationFinished(Event):
    """编排阶段结束，worker 汇总即将交给主 agent。"""

    type: Literal["orchestration_finished"] = "orchestration_finished"
    completed: int = 0
    failed: int = 0
    summary: str = ""


class ContextTrimmed(Event):
    """一次 act/respond 的上下文裁剪报告。"""

    type: Literal["context_trimmed"] = "context_trimmed"
    node: str = ""
    trimmed: int = 0
    before_chars: int = 0
    after_chars: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    escalated: bool = False


class CostReport(Event):
    """一次 run 的成本对账与预算告警。"""

    type: Literal["cost_report"] = "cost_report"
    run_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    budget_usd: float = 0.0
    ratio: float = 0.0
    billed_usd: float = 0.0
    billing_delta_usd: float = 0.0
    billing_error: str = ""
    alerts: list[str] = Field(default_factory=list)
    # provider prefix cache：命中/未命中 token；未回报的 provider 记 0。
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    providers: dict[str, dict[str, float]] = Field(default_factory=dict)
    billing_providers: dict[str, dict[str, float]] = Field(
        default_factory=dict
    )


class SubAgentFinished(Event):
    """子智能体结束。

    `status`: success / partial / failed / rejected。partial 表示跑满了轮次、超时，
    或报告生成失败但有兜底摘要 —— 主模型仍拿到可用信息，但不应把它当成完整结论。
    rejected 表示宿主没让它跑（审批拒绝、任务级委派预算耗尽），与「跑了但失败」不同。
    """

    type: Literal["subagent_finished"] = "subagent_finished"
    agent_id: str = ""
    status: str = ""
    summary: str = ""
    relevant_files: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


class WorkflowRevised(Event):
    """用户要求重规划后，剩余任务图被重建。

    已完成/已合并的任务不会重跑；`tasks` 是重建后的**完整**任务列表
    （含保留的已完成任务），前端据此刷新树状进度。
    """

    type: Literal["workflow_revised"] = "workflow_revised"
    reason: str = ""
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class SteeringApplied(Event):
    """一条用户干预指令被图节点消费。

    action: pause | resume | cancel | add | replan。
    pause 在 `interrupt()` 时发出（事件流到此收尾，等待
    `AgentRuntime.resume_steering`）；其余动作在节点更新时发出。
    """

    type: Literal["steering_applied"] = "steering_applied"
    action: str = ""
    task_id: str = ""
    detail: str = ""


# --------------------------------------------------------------------------
# 人工审批
# --------------------------------------------------------------------------

class ApprovalRequested(Event):
    """图已挂起，等待人工确认。

    前端收集答复后调用 `AgentRuntime.resume(thread_id, {request_id: bool})`。
    若事件流以本事件收尾（而不是 RunFinished），即表示处于挂起状态。
    """

    type: Literal["approval_requested"] = "approval_requested"
    request_id: str = ""
    tool: str = ""
    command: str = ""
    level: str = ""
    reason: str = ""


# --------------------------------------------------------------------------
# 收尾
# --------------------------------------------------------------------------

class RunStarted(Event):
    """一轮运行开始。前端用它标记事件流的起点（Web SSE 的第一帧）。"""

    type: Literal["run_started"] = "run_started"
    thread_id: str = ""


class RunFinished(Event):
    """一轮运行正常收尾。

    `input_tokens` / `output_tokens` 是整轮的用量总计（跨挂起-恢复累加），
    取自 provider 回报的 `usage_metadata`。未回报时为 None —— 与 0 不同，
    消费方（如能力评测）不该把缺失当成"用了零 token"。
    """

    type: Literal["run_finished"] = "run_finished"
    thread_id: str = ""
    answer: str = ""
    # 主 agent 的 run_total（含委派子智能体，runtime 会并入）。
    input_tokens: int | None = None
    output_tokens: int | None = None
    # 编排 worker 的用量。dispatch 不把 worker 并进 run_total，必须单列；
    # 前端要显示总成本时自行相加。
    worker_input_tokens: int | None = None
    worker_output_tokens: int | None = None
    # 本次运行的资源池快照（worker/tool/wsl/llm 的并发、排队与等待时间）。
    concurrency: list[dict] | None = None


class RunFailed(Event):
    """编排层自身出错（模型/沙箱不可用等），与「工具执行失败」不同。

    同样带上已累计的用量：失败的那一轮**花费可能最多**（反复重试、长上下文），
    把它排除在统计外会让成本口径系统性偏低。
    """

    type: Literal["run_failed"] = "run_failed"
    message: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    worker_input_tokens: int | None = None
    worker_output_tokens: int | None = None


EventType = (
    PlanCreated
    | PlanRevised
    | StepStarted
    | StepFinished
    | AssistantToken
    | ToolCallStarted
    | ToolCallFinished
    | FileChanged
    | Verification
    | ReviewFinished
    | RepairStarted
    | WorkflowSelected
    | TaskGraphCreated
    | TaskGraphExtended
    | WorkerStarted
    | WorkerFinished
    | OrchestrationFinished
    | SubAgentStarted
    | SubAgentFinished
    | WorkflowRevised
    | SteeringApplied
    | ContextTrimmed
    | CostReport
    | ApprovalRequested
    | RunStarted
    | RunFinished
    | RunFailed
)
