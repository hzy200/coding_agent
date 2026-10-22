<!-- 由 scripts/gen_contracts.py 从源码生成，禁止手改。改代码后重跑生成，CI 会校验一致性。 -->

# 领域事件契约

事实源：`src/coding_agent/events.py`（pydantic 模型）。
机器可读版本：[`events.json`](events.json)。

前端（CLI / TUI / Web）**只**消费这里的事件，不导入 `tools/` 与 `sandbox/`。
这条边界由 `tests/unit/test_architecture.py` 用 AST 扫描守住。

共 **27** 个事件类型。

## `approval_requested`

类 `ApprovalRequested`

图已挂起，等待人工确认。

前端收集答复后调用 `AgentRuntime.resume(thread_id, {request_id: bool})`。
若事件流以本事件收尾（而不是 RunFinished），即表示处于挂起状态。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `request_id` | `str` | `""` |  |
| `tool` | `str` | `""` |  |
| `command` | `str` | `""` |  |
| `level` | `str` | `""` |  |
| `reason` | `str` | `""` |  |

## `assistant_token`

类 `AssistantToken`

流式吐字。node 标明来自 act（步骤小结）还是 respond（最终答复）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `node` | `str` | `""` |  |
| `text` | `str` | `""` |  |

## `context_trimmed`

类 `ContextTrimmed`

一次 act/respond 的上下文裁剪报告。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `node` | `str` | `""` |  |
| `trimmed` | `int` | `0` |  |
| `before_chars` | `int` | `0` |  |
| `after_chars` | `int` | `0` |  |
| `before_tokens` | `int` | `0` |  |
| `after_tokens` | `int` | `0` |  |
| `escalated` | `bool` | `false` |  |

## `cost_report`

类 `CostReport`

一次 run 的成本对账与预算告警。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `run_id` | `str` | `""` |  |
| `input_tokens` | `int` | `0` |  |
| `output_tokens` | `int` | `0` |  |
| `cost_usd` | `float` | `0.0` |  |
| `budget_usd` | `float` | `0.0` |  |
| `ratio` | `float` | `0.0` |  |
| `billed_usd` | `float` | `0.0` |  |
| `billing_delta_usd` | `float` | `0.0` |  |
| `billing_error` | `str` | `""` |  |
| `alerts` | `list[str]` | `"Field(default_factory=list)"` |  |
| `cache_hit_tokens` | `int` | `0` | provider prefix cache：命中/未命中 token；未回报的 provider 记 0。 |
| `cache_miss_tokens` | `int` | `0` |  |
| `providers` | `dict[str, dict[str, float]]` | `"Field(default_factory=dict)"` |  |
| `billing_providers` | `dict[str, dict[str, float]]` | `"Field(default_factory=dict)"` |  |

## `file_changed`

类 `FileChanged`

文件被创建/覆盖/编辑。只有真正改动文件的动作才会发这个事件。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `path` | `str` | `""` |  |
| `action` | `str` | `""` |  |
| `added` | `int` | `0` |  |
| `removed` | `int` | `0` |  |
| `diff` | `str` | `""` |  |
| `snapshot_id` | `str | None` |  |  |

## `orchestration_finished`

类 `OrchestrationFinished`

编排阶段结束，worker 汇总即将交给主 agent。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `completed` | `int` | `0` |  |
| `failed` | `int` | `0` |  |
| `summary` | `str` | `""` |  |

## `plan_created`

类 `PlanCreated`

planner 完成分解。

`degraded` 为真表示**规划其实失败了**：模型没给出可解析的步骤，
已退化成「整条请求当成一步」。这时 `steps` 只有一项、就是请求原文，
后面不会再有真正的多步推进。前端必须把它显示出来 ——
静默降级的话，一次完全失效的规划看起来和正常工作一模一样。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `steps` | `list[str]` | `"Field(default_factory=list)"` |  |
| `kinds` | `list[str]` | `"Field(default_factory=list)"` | 与 steps 逐项对齐：explore / implement / verify / unknown |
| `degraded` | `bool` | `false` |  |

## `plan_revised`

类 `PlanRevised`

执行中重建了剩余计划（planner 只在开局跑一次，之后靠它修正）。

`steps` 是**完整的**新计划（含已完成的部分，它们不会被改动），
`step_idx` 是当前所处步骤，前端据此重新渲染计划面板。
修订次数有硬上限（`AGENT_MAX_REPLANS`）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `steps` | `list[str]` | `"Field(default_factory=list)"` |  |
| `kinds` | `list[str]` | `"Field(default_factory=list)"` |  |
| `step_idx` | `int` | `0` |  |

## `repair_started`

类 `RepairStarted`

验证失败后开始第 N 次修复。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `attempt` | `int` | `1` |  |
| `limit` | `int` | `0` |  |
| `summary` | `str` | `""` |  |
| `issues` | `list[str]` | `"Field(default_factory=list)"` |  |

## `review_finished`

类 `ReviewFinished`

一步做完、验证通过后，代码审查的结果。

与 `Verification` 分开而不是复用：审查回答的不是「行为对不对」，而是
「代码干不干净」（调试残留、被改弱的断言、语法坏掉的分支、往 `.agent/` 里写）。
合成一个事件会让「这一步到底卡在哪一关」看不出来。

status: clean / warned / blocked / skipped

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `status` | `str` | `"skipped"` |  |
| `blocked` | `bool` | `false` |  |
| `summary` | `str` | `""` |  |
| `findings` | `list[str]` | `"Field(default_factory=list)"` |  |

## `run_failed`

类 `RunFailed`

编排层自身出错（模型/沙箱不可用等），与「工具执行失败」不同。

同样带上已累计的用量：失败的那一轮**花费可能最多**（反复重试、长上下文），
把它排除在统计外会让成本口径系统性偏低。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `message` | `str` | `""` |  |
| `input_tokens` | `int | None` |  |  |
| `output_tokens` | `int | None` |  |  |
| `worker_input_tokens` | `int | None` |  |  |
| `worker_output_tokens` | `int | None` |  |  |

## `run_finished`

类 `RunFinished`

一轮运行正常收尾。

`input_tokens` / `output_tokens` 是整轮的用量总计（跨挂起-恢复累加），
取自 provider 回报的 `usage_metadata`。未回报时为 None —— 与 0 不同，
消费方（如能力评测）不该把缺失当成"用了零 token"。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `thread_id` | `str` | `""` |  |
| `answer` | `str` | `""` |  |
| `input_tokens` | `int | None` |  | 主 agent 的 run_total（含委派子智能体，runtime 会并入）。 |
| `output_tokens` | `int | None` |  |  |
| `worker_input_tokens` | `int | None` |  | 编排 worker 的用量。dispatch 不把 worker 并进 run_total，必须单列； 前端要显示总成本时自行相加。 |
| `worker_output_tokens` | `int | None` |  |  |
| `concurrency` | `list[dict] | None` |  | 本次运行的资源池快照（worker/tool/wsl/llm 的并发、排队与等待时间）。 |

## `run_started`

类 `RunStarted`

一轮运行开始。前端用它标记事件流的起点（Web SSE 的第一帧）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `thread_id` | `str` | `""` |  |

## `steering_applied`

类 `SteeringApplied`

一条用户干预指令被图节点消费。

action: pause | resume | cancel | add | replan。
pause 在 `interrupt()` 时发出（事件流到此收尾，等待
`AgentRuntime.resume_steering`）；其余动作在节点更新时发出。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `action` | `str` | `""` |  |
| `task_id` | `str` | `""` |  |
| `detail` | `str` | `""` |  |

## `step_finished`

类 `StepFinished`

当前子任务收尾（模型给出了小结，或工具预算耗尽被强制叫停）。

`cancelled` 表示这一步**根本没机会执行完**：重规划判定它做不成、把剩余步骤
砍掉了，于是路由直接去收尾。它与 `budget_exhausted` 的区别是——后者是"试过
但工具轮次用完"，前者是"还没试就被放弃了"。前端需要区分，否则用户会以为
模型已经尝试过。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `index` | `int` | `0` |  |
| `budget_exhausted` | `bool` | `false` |  |
| `cancelled` | `bool` | `false` |  |
| `text` | `str` | `""` |  |

## `step_started`

类 `StepStarted`

进入某个子任务。total == 1 时前端可不渲染步进标题。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `index` | `int` | `0` |  |
| `total` | `int` | `1` |  |
| `text` | `str` | `""` |  |

## `subagent_finished`

类 `SubAgentFinished`

子智能体结束。

`status`: success / partial / failed / rejected。partial 表示跑满了轮次、超时，
或报告生成失败但有兜底摘要 —— 主模型仍拿到可用信息，但不应把它当成完整结论。
rejected 表示宿主没让它跑（审批拒绝、任务级委派预算耗尽），与「跑了但失败」不同。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `agent_id` | `str` | `""` |  |
| `status` | `str` | `""` |  |
| `summary` | `str` | `""` |  |
| `relevant_files` | `list[str]` | `"Field(default_factory=list)"` |  |
| `findings` | `list[str]` | `"Field(default_factory=list)"` |  |
| `input_tokens` | `int` | `0` |  |
| `output_tokens` | `int` | `0` |  |
| `duration_ms` | `int` | `0` |  |

## `subagent_started`

类 `SubAgentStarted`

主智能体把一次只读探索委派给了子智能体。

与 `ToolCallStarted` 同时发出：delegate 本身也是一次工具调用。前端按
`agent_id` 把后续的 `SubAgentFinished` 配对回来。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `agent_id` | `str` | `""` |  |
| `parent_call_id` | `str` | `""` |  |
| `role` | `str` | `"explore"` |  |
| `goal` | `str` | `""` |  |

## `task_graph_created`

类 `TaskGraphCreated`

编排器生成了任务图（可能已降级为单任务）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `tasks` | `list[dict[str, Any]]` | `"Field(default_factory=list)"` |  |
| `degraded` | `bool` | `false` |  |

## `task_graph_extended`

类 `TaskGraphExtended`

运行中由 worker 提议、host 校验后追加的只读任务（Phase 4D）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `added` | `list[dict[str, Any]]` | `"Field(default_factory=list)"` |  |
| `rejected` | `list[str]` | `"Field(default_factory=list)"` |  |

## `tool_call_finished`

类 `ToolCallFinished`

工具执行完毕。

ok/rejected 语义：rejected 表示被安全策略拦下（此时 exit_code 为 None）；
两者都为 False 表示执行了但失败。文件工具没有退出码，exit_code 恒为 None。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `call_id` | `str` | `""` |  |
| `name` | `str` | `""` |  |
| `ok` | `bool` | `false` |  |
| `rejected` | `bool` | `false` |  |
| `skipped` | `bool` | `false` | 环境不具备该能力（例如未配置 LSP）：不是成功，也不该计入失败率。 |
| `interrupted` | `bool` | `false` | 有副作用的调用执行到一半、结果未知：既不是成功也不是普通失败。 |
| `decision` | `str` | `"auto"` | auto / approved / denied / rejected —— 审计据此区分「谁拒的」 |
| `exit_code` | `int | None` |  |  |
| `duration_ms` | `int | None` |  |  |
| `level` | `str | None` |  |  |
| `preview` | `str` | `""` |  |

## `tool_call_started`

类 `ToolCallStarted`

模型请求了一次工具调用，即将执行。

level 供前端着色，不是安全判定。call_id 用于和 ToolCallFinished 配对 ——
同一轮可能有多个并行工具调用，只靠顺序无法可靠对应。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `call_id` | `str` | `""` |  |
| `name` | `str` | `""` |  |
| `args` | `dict[str, Any]` | `"Field(default_factory=dict)"` |  |
| `summary` | `str` | `""` |  |
| `level` | `str | None` |  |  |

## `verification`

类 `Verification`

一步做完后自动跑验证的结果。

status: ok / failed（跑过）、error（验证环境异常，没跑起来）、
not_configured（没有可用的测试命令）、skipped（没跑）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `status` | `str` | `"skipped"` |  |
| `command` | `str` | `""` |  |
| `ok` | `bool | None` |  | 三态：True 通过 / False 失败 / **None 没验证**（`not_configured` / `skipped`）。 与审计里的 `ok` 刻意保持同源同形 —— 曾经两边口径不同，同一次「没跑验证」 在事件与审计里结论相反，事后对账对不上（见 BUG_AUDIT 的 B7）。 |
| `summary` | `str` | `""` |  |
| `issues` | `list[str]` | `"Field(default_factory=list)"` |  |
| `failed_tests` | `list[str]` | `"Field(default_factory=list)"` | 解析出的失败测试 id，供前端/评测直接展示与统计 |

## `worker_finished`

类 `WorkerFinished`

一个 worker 结束；status: success / partial / failed / blocked。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `worker_id` | `str` | `""` |  |
| `task_id` | `str` | `""` |  |
| `role` | `str` | `""` |  |
| `status` | `str` | `""` |  |
| `summary` | `str` | `""` |  |
| `input_tokens` | `int` | `0` |  |
| `output_tokens` | `int` | `0` |  |
| `duration_ms` | `int` | `0` |  |
| `attempt` | `int` | `1` | 该任务的第几次尝试（1 = 首次；>1 表示重试） |
| `merged` | `bool` | `false` | Phase 3C：implement worker 的补丁是否已合并 / 合并错误 |
| `merge_error` | `str` | `""` |  |
| `tool_calls` | `int` | `0` | worker 内部执行的工具调用数（审计里逐条落账） |
| `queue_wait_ms` | `int` | `0` | ---- Phase 5：成本与审计指标 ---- |
| `llm_ms` | `int` | `0` |  |
| `tool_ms` | `int` | `0` |  |
| `timed_out` | `bool` | `false` |  |
| `artifact_id` | `str` | `""` |  |
| `depth` | `int` | `0` |  |
| `priority` | `int` | `0` |  |
| `scope_violations` | `list[str]` | `"Field(default_factory=list)"` |  |

## `worker_started`

类 `WorkerStarted`

一个 worker 开始执行。

Phase 4：由 dispatch 通过 LangGraph custom stream 实时发出，而不是等整批
worker 结束后回放。`attempt` 便于前端区分重试。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `worker_id` | `str` | `""` |  |
| `task_id` | `str` | `""` |  |
| `role` | `str` | `""` |  |
| `goal` | `str` | `""` |  |
| `attempt` | `int` | `1` |  |
| `queue_wait_ms` | `int` | `0` | 进入 ready 队列后等待并发槽位的时间。 |

## `workflow_revised`

类 `WorkflowRevised`

用户要求重规划后，剩余任务图被重建。

已完成/已合并的任务不会重跑；`tasks` 是重建后的**完整**任务列表
（含保留的已完成任务），前端据此刷新树状进度。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `reason` | `str` | `""` |  |
| `tasks` | `list[dict[str, Any]]` | `"Field(default_factory=list)"` |  |

## `workflow_selected`

类 `WorkflowSelected`

编排器选择了一个工作流模板。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `workflow` | `str` | `""` |  |
| `reason` | `str` | `""` |  |

