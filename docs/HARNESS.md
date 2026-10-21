# Harness 化改进方案：动态工作流 + 并发子智能体

> **状态**：harness 新架构已落地：**独立 orchestrator 子图 + 事件驱动动态
> DAG + 并发 worker + 结构化 fan-in**。已实现动态只读任务扩展、写 worker
> worktree 隔离与串行合并、写时 `write_scope` 校验、运行中干预、artifact
> store、任务级 token 预算、成本/并发指标、噪声感知对比与干预延迟基准。
> direct 仍保留为小任务快速路径。已有 AST import 图 + 反向依赖闭包的测试
> 影响分析、轻量 repo map（survey → planner → explore/implement）、
> AST 符号检索 `symbol_search`、可选 LSP 客户端（initialize /
> workspace-symbol / definition / references / pull+publish diagnostics）、
> 批量快照读写、常驻 WSL shell 会话池（opt-in，自动回退）、
> 共享文件多 worker 的 3-way 合并（临时 index 预演，冲突整批回滚）、
> steering 队列跨进程持久化、Web/CLI 实时控制入口（SSE 审批 /
> pause/cancel/add/replan / resume + `agent steer`）、
> scheduler 全局 token 准入（派发前按角色预留）、fan-in reducer、
> 429 自适应降并发。
> 多 provider 路由（429/错误 fallback）与跨进程限流协调已实现
> （`.agent/rate_limits.json`，文件锁 + 原子替换）。
> provider 级成本/配额路由、task_type/capabilities 过滤、
> `CostReport` 成本对账与预算告警已实现。
> 已支持 provider 账单 API 对账（`openai_costs` / 通用 JSON）。
> **仍计划中**：多币种/税/折扣的账单归一化。
> 当前实现见 [ARCHITECTURE.md](ARCHITECTURE.md)，运行工作流见 [WORKFLOW.md](WORKFLOW.md)。

目标是把系统演进成 Claude Code / Codex / Workbuddy / DeepSeek Harness 那类
工作区智能体：

1. 用户指定一个文件夹作为工作区；
2. 智能体读取提示词与仓库内容，**自主选择并构建工作流**；
3. 把工作拆成显式任务图，派生多个子智能体**并发处理**；
4. 运行中可接收用户干预，实时调整工作流与任务；
5. 最终汇总答案，并在用户授权下修改工作区。

---

## 1. 目标与边界

### 1.1 目标

| 能力 | 目标行为 |
|---|---|
| 工作区 | 用户用 `-C/--workspace` 指定文件夹；所有读写、命令、快照、审计都限定在该根目录 |
| 动态工作流 | 根据提示词、仓库规模/结构、测试形态选择模板；必要时让模型在有限模板中选择 |
| 任务图 | 显式 `TaskSpec` DAG：目标、角色、依赖、验收、预算 |
| 并发 worker | 满足依赖的任务并发执行；只读 worker 默认可并发，写 worker 需隔离 |
| Fan-in | 结构化 `WorkerResult` 汇总；部分失败如实标注；主 agent 基于汇总继续 |
| 实时干预 | 用户可暂停、取消、追加任务、要求重规划、审批/拒绝 |
| 授权 | 写操作仍走现有 L0–L3 分级与审批；并发写必须在预授权范围内 |

### 1.2 非目标（当前阶段）

- 不做无限制的 agent 递归（worker 深度固定为 1）。
- 不允许绕过 `approval_gate`/`tools` 的第二条执行路径。
- 不追求「无人值守地自动提交/推送」；交付动作仍需授权。
- 不在 Phase 3A 引入并发写文件。

### 1.3 与现有单 agent 模式的关系

现有单 agent 图继续作为：

- `direct` 工作流的执行器；
- 编排路径汇总后的**实现执行器**；
- feature flag 关闭时的完整回退路径。

新能力通过 `AGENT_ORCHESTRATOR_ENABLED`（默认 `false`）逐步启用，
保证旧基线与旧行为可比。

---

## 2. 现状（已实现）

| 能力 | 落点 | 状态 |
|---|---|---|
| 单 agent 状态机 | `graph/build.py`、`graph/nodes/` | 已实现 |
| 结构化计划 `plan_kinds` | `graph/plan.py`、`nodes/planner.py` | 已实现 |
| 只读 Explore 子智能体 | `agents/explore.py`、`tools/delegate.py` | 已实现 |
| 同批并发委派 | `graph/nodes/tools.py` | 已实现 |
| 委派成本策略 | `agents/policy.py` | 已实现 |
| 验证/修复/停滞检测 | `nodes/verify.py`、`routing.py` | 已实现 |
| 审批 / 沙箱 / 审计 / 快照 | `sandbox/`、`audit/` | 已实现 |
| 领域事件 | `events.py`、`runtime.py` | 已实现 |
| 端到端评测 | `tests/eval/`，含 LangSmith 全功能数据集 | 已实现 |
| Orchestrator 3A | `orchestrator/`：survey / workflow select / TaskGraph / scheduler / fan-in | **已实现**（只读 worker） |
| Orchestrator 3B | explore/test/**review** worker、任务级重试、fan-in 文件重叠报告 | **已实现** |
| Orchestrator 3C | **implement** worker、git worktree 隔离、写范围校验、patch 合并/冲突 | **已实现**（默认关闭） |

**仍未实现 / 后续**：

- 多币种/税/折扣的账单归一化。
- 写计划审批已支持，但默认关闭；TUI 建议开启。

---

## 3. 目标运行模型

```
用户提示词 + 工作区
        │
        ▼
   intake / survey          ← 仓库画像、测试/构建形态、目录结构
        │
        ▼
   workflow select          ← 模板 + 规则 +（必要时）LLM
        │
        ├── direct ──────────────→ 现有 planner → act → … → respond
        │
        ▼
   plan_tasks               ← 生成 TaskSpec DAG，校验/降级
        │
        ▼
   write_gate               ← 可选：并发前一次性确认 implement write_scope
        │
        ▼
   scheduler ⇄ steering_dispatch
        │                   ← ready 任务即时启动；完成即解锁依赖
        │                   ← 失败短路、重试、动态只读扩展、429 退避
        │                   ← 每 checkpoint_every 个任务形成一次边界
        ▼
   fan-in                   ← WorkerResult 排序/预算/可选 reducer + artifact
        │
        ▼
   主 agent 实现            ← 现有 planner/act/verify/review/repair
        │
        ▼
   respond                  ← 最终答复（如实标注未完成/失败）
```

**用户干预通道**（Phase 3D）贯穿全程：在节点边界消费 `pause/cancel/add/replan`，
通过 `interrupt()`/checkpoint 与现有审批机制对齐。

---

## 4. 工作区与授权

### 4.1 工作区

- CLI：`agent run -C <folder> "..."` / `agent tui -C <folder>`；已有实现。
- 工作区在沙箱内解析为绝对根目录；`SandboxFs`/`pathguard` 做词法 + realpath 双重校验。
- 工作区外的读写、命令、符号链接逃逸一律拒绝（已实现，见 ARCHITECTURE 不变量 9/11）。
- `.agent/` 只放 checkpoint/audit/backup/eval，不参与模型业务。

### 4.2 授权

| 层 | 机制 | 现状 |
|---|---|---|
| 命令分级 | `sandbox/policy.py` L0–L3 | 已实现 |
| 审批 | `approval_gate` + `interrupt()` | 已实现 |
| 文件写入 | `file_write`/`file_edit` + 写前快照 | 已实现 |
| 回滚 | 快照可回滚，回滚本身也可回滚 | 已实现 |
| 审计 | 宿主写 JSONL，前端绕不过 | 已实现 |

**编排模式的新增约束（已实现）**：

- worker 的写权限由 orchestrator 预授权 + `write_scope` 白名单决定；
- 子智能体不能自行扩大写范围；
- 并发写 worker（Phase 3C）必须在隔离工作区中执行，合并动作回到主工作区并记审计；
- 用户拒绝/取消时，运行中的 worker 必须协作式取消，已产生的改动按快照回滚或保留并标注。

---

## 5. 动态工作流

### 5.1 输入

- 用户提示词；
- `RepoProfile`：文件数、测试文件数、语言/构建形态（已部分实现）；
- 测试命令探测（已实现）；
- 上一轮计划/失败历史（用于运行中重规划）。

### 5.2 模板（首版与后续）

| 模板 | 适用 | 行为 | 阶段 |
|---|---|---|---|
| `direct` | 小仓库、单点任务 | 现有单 agent 图 | 3A |
| `parallel_readonly` | 多模块/多交付物、需要并行定位 | 2–3 个只读 explore/test worker 并发，汇总后主 agent 实现 | 3A |
| `survey_then_implement` | 陌生仓库、先定位再改 | 单个 explore worker → 主 agent 实现 | 3A/3B |
| `review_loop` | 已有实现、要求审查/重构 | implement → 并发 review/test → repair | 3B |
| `orchestrated_implement` | 多个独立功能线 | 多个 implement worker（worktree 隔离）并发 | 3C |

### 5.3 选择策略

1. **规则优先**：文件数、测试形态、提示词中的「同时/分别/各自/多个交付物」、
   是否存在多个独立模块。
2. **LLM 兜底**：规则不确定时，用结构化输出在**有限模板**中选择，返回
   `workflow` + `reason`；失败回退规则结果。
3. **降级可见**：选择失败/任务图校验失败时，退化为 `direct` 或单个 explore，
   并发出事件、写审计，不能静默改变行为。

### 5.4 运行中调整

- 失败驱动：验证/审查失败、依赖阻塞、预算耗尽时，orchestrator 可重规划任务图。
- 用户驱动（Phase 3D）：追加任务、取消任务、改变优先级、要求换模板。
- **worker 驱动（Phase 4D）**：explore/review worker 可在结构化报告里提议最多
  2 个**只读**任务；scheduler 在完成点做 host 校验（角色、去重、依赖存在、
  无环、总任务数上限）后立即加入 ready 队列。失败/取消的 worker 的提议被忽略。
- 所有调整都受硬上限约束（`max_tasks`、`checkpoint_every`、全局 deadline、
  任务级预算）。worker 不能提议 implement —— 写范围必须来自 planner/用户。

---

## 6. 任务图与调度

### 6.1 TaskSpec

```python
class TaskSpec(TypedDict):
    id: str
    role: Literal["explore", "implement", "test", "review"]
    goal: str
    inputs: list[str]            # 文件/符号/关注点
    acceptance: list[str]        # 怎样算完成
    depends_on: list[str]
    depends_mode: Literal["full", "any"]  # full=必须 success；any=success/partial
    write_scope: list[str]       # Phase 3C 才允许非空
    max_rounds / timeout / max_tokens / priority   # 任务级预算与调度优先级
```

### 6.2 校验

- id 唯一、非空；
- 依赖存在、无自依赖、无环；
- role 在模板允许集合内；
- 任务数 ≤ `orchestrator_max_tasks`（超限按校验失败整图降级，不静默截断）；
- 只读模式的 `write_scope` 必须为空；写模式 implement 必须给出且互不重叠；
- `depends_mode` 只能是 `full`/`any`；implement 必须是 `full`（写任务不能
  建在未通过完整验证的依赖上）。

校验失败 → 单 explore 任务兜底 + `degraded=True`。

### 6.3 调度

- 事件驱动：ready 最小堆 + `asyncio.wait(FIRST_COMPLETED)`；任务完成立即解锁
  依赖方，不再等整批；
- `ConcurrencyGovernor.worker` 资源池控制真正并发（按任务优先级、带获取超时）；
- 依赖 failed/blocked → 该任务标 `blocked`；同类失败立即短路；
- 失败任务在额度内带上次失败上下文重试（`retry_context`），额度用尽才阻塞下游；
- 动态只读提议在 worker 完成点校验并立即入队；
- `orchestrator_checkpoint_every` **只按完成数**形成 checkpoint/steering 边界
  （drain 在飞任务后返回父图），不再限制每个边界能启动几个；pause 只在边界
  `interrupt()`；
- token 预留优先用同角色历史实际用量的 EWMA；预算不足时 next-fit 选择可容纳
  的任务，而不是把所有 pending 一起标 blocked；
- 全局 `orchestrator_run_deadline_s` 与每个 worker 的独立 timeout 共同兜底；
- 429/限流时临时降低并发并按 `orchestrator_rate_limit_backoff_s` 指数退避，
  成功后逐步恢复；
- `orchestrator_write_plan_approval=true` 时，scheduler 启动前由 `write_gate`
  一次性确认整批 `write_scope`；并发区自身不弹审批。

### 6.4 状态

```python
execution_mode: str          # direct | orchestrated
request: str                 # 本轮请求（子图只读这一项，不复制 messages）
session_brief: str           # 从 checkpoint 抽取的有界会话约束摘要
workflow: str
workflow_reason: str
repo_summary: dict
tasks: list[dict]
task_status: dict[str, str]  # pending | merging | success | partial | failed | blocked
worker_results: list[dict]
orchestrator_round: int
orchestrator_degraded: bool
orchestrator_summary: str
worker_context: str          # planner/act 使用（文本，含 artifact 路径）
worker_brief: str            # 紧凑副本
orchestration_artifacts: list[dict]  # .agent/artifacts/ 索引
orchestration_handoff: dict  # 任务级结构化交接（状态/文件/发现/artifact）
```

生命周期：`planner` 在每次 run 初始化；新节点只写自己的字段；`respond` 只读。

---

## 7. Worker / 子智能体

### 7.1 角色

| 角色 | 工具 | 写 | 阶段 |
|---|---|---|---|
| `explore` | 只读检索/读取 | 否 | 已实现（delegate） / 3A（worker） |
| `test` | 宿主测试命令 | 否 | 3A |
| `review` | 确定性 review + 可选 LLM | 否 | 3B |
| `implement` | 文件/命令工具子集 | 是 | 3C |

### 7.2 隔离

- 独立 `messages`，不继承主 agent 历史；
- 唯一的显式例外是 `session_brief`：宿主从 checkpoint 抽最近若干轮的
  人/助手文本，压成有界摘要（`AGENT_SESSION_BRIEF_CHARS`，默认 4000，0 关闭），
  作为「之前轮次的会话约束」注入 select/plan 与每个 worker；
- 独立工具子集与预算；
- 独立 trace 元数据（`agent_id`/`task_id`/`role`）；
- 深度固定为 1，worker 不能再派生 worker；
- 只返回结构化 `WorkerResult`，不把中间工具输出写回主历史。

### 7.3 WorkerResult

```python
class WorkerResult(TypedDict):
    task_id: str
    role: str
    worker_id: str
    status: Literal["success", "partial", "failed", "blocked"]
    summary: str
    files: list[str]
    findings: list[str]
    verification: dict
    input_tokens: int
    output_tokens: int
    duration_ms: int
```

---

## 8. 并发写与隔离（Phase 3C，已实现）

这是整个方案里**风险最高**的部分。原则：

1. **默认只读并发**；写 worker 必须显式开启。
2. **一个文件同一时刻只有一个 writer**：靠 `write_scope` 分区或文件锁。
3. **跨文件重构**：每个 writer 一个 git worktree；合并回主工作区时做冲突检测。
4. **冲突处理**：同文件/hunk 冲突时不自动覆盖；回到主 agent 或请求用户裁决。
5. **回滚**：worker 的每次落盘仍有快照；**合并前主工作区的目标文件也留底**，
   `agent undo` 可以回滚已合并的编排写；合并失败可整体回退。
6. **审批**：写 worker 的权限由 orchestrator 预授权；L2/L3 仍需审批，不能并发弹多个交互。

写 worker 默认关闭；只读编排的价值已经单独验证，写路径必须额外显式开启。

---

## 9. Fan-in 汇总

1. **确定性合并**：按 task_id 去重、按依赖顺序排列、汇总文件与验证结果。
2. **冲突检测**：同一文件被多个 worker 修改、验证结果互相矛盾时显式标注。
3. **LLM 综合**：只把压缩后的 `WorkerResult` 交给主 agent，不塞原始工具输出。
4. **部分失败**：failed/blocked 的任务必须在 `worker_context` 与最终答复中如实出现。
5. **结构化交接**：`orchestration_handoff` 保存任务级字段（状态、文件、发现、
   验证摘要、合并结果、token、artifact 路径）；`worker_context` 是它的有界文本
   投影，并在头部列出 artifact 路径，主 agent 可用 `file_read` 回读完整结果。
5. **引用**：汇总尽量带文件/符号位置，便于主 agent 直接实现。

`synthesize` 不生成最终答复；它把 `worker_context` 交给现有 `planner`，
最终答复仍由 `respond` 产出，复用现有防幻觉护栏。

---

## 10. 用户实时干预（Phase 3D，已实现）

### 10.1 命令（TUI）

| 命令 | 语义 |
|---|---|
| `/pause` | 在下一个节点边界暂停；不再派发新 worker |
| `/resume` | 从 checkpoint 继续（`Command(resume=...)`） |
| `/cancel` | 停止后续任务；未合并的 patch 丢弃，已合并改动保留 |
| `/add [角色] <目标>` | 追加只读任务（explore/test/review），进入 pending |
| `/replan` | 重建**剩余**任务图；已完成任务不重跑 |
| `/approve` / `/deny` | 复用现有审批交互 |

CLI `agent run` 与 Web 不提供实时控制入口，只透传事件。

### 10.2 机制

- `steering.SteeringController`：按 `thread_id` 隔离的指令队列；
  未消费指令与 pause 标记原子落盘到 `.agent/steering/`，跨进程可恢复；
  runtime 通过 `steer()` 入队，图节点在边界消费；
- 四个消费点：
  - `steering_plan`（任务图生成后、scheduler 前）；
  - `steering_dispatch`（每个 scheduler checkpoint 边界之后）；
  - `steering_step`（direct 模式一步之后、replan 前）；
  - `steering_final`（所有进入 respond 之前，覆盖单步任务）；
- pause 用 `interrupt()` 挂起、checkpoint 保存图状态；恢复重跑节点时，
  先回填同批其它命令，避免丢失；
- cancel 是**协作式**的：只读/写 worker 在每轮 LLM 与每次工具执行前检查；
  implement worker 取消后清理 worktree、不生成 patch；scheduler 在合并前
  再检查一次，取消时丢弃本批未合并 patch；
- `/add` 只接受只读角色；自由文本无法安全推断 write_scope；
- `/replan` 保留 success/partial 任务，只让模型生成剩余任务；若已有
  implement patch 合并，新任务只允许只读（worktree 基于 HEAD）；
- 事件：`SteeringApplied(action, task_id, detail)`、`WorkflowRevised(reason, tasks)`；
- 审计：提交时写 `kind=steering`，含 `source`/动作/目标；worker 内部工具仍逐条落账。

### 10.3 边界与已知限制

- 干预不能绕过审批与路径守卫：steering 只改控制流/任务图，不执行工具；
- pause/cancel 在**节点边界**生效；正在跑的 LLM 轮次与同步工具不会被打断；
- 取消**不清空本进程已合并**的 implement 改动（明确策略）；未合并 patch 丢弃；
- steering 队列不持久化：重启后可从 checkpoint 恢复暂停状态，但队列里
  未消费的 add/replan 会丢失；
- `/add` 只读；写任务仍必须由 planner 生成并带 write_scope；
- 并发写 scope 不重叠的约束不变。

---

## 11. 事件、审计与可观测性

事件（均已实现）：

- `WorkflowSelected(workflow, reason)`
- `TaskGraphCreated(tasks, degraded)`
- `TaskGraphExtended(added, rejected)` —— worker 提议经 host 校验后追加的只读任务
- `WorkerStarted(worker_id, task_id, role, goal, attempt, queue_wait_ms)` ——
  **实时**：scheduler 在拿到并发槽位后立即通过 `get_stream_writer()` 发出；
- `WorkerFinished(worker_id, task_id, role, status, summary, tokens, duration_ms,
  merged, merge_error, timed_out, artifact_id, scope_violations)` —— 只读
  worker 完成即发；implement 等到主工作区合并之后才发，保证 `merged` 准确；
- `OrchestrationFinished(completed, failed, summary)`
- `CostReport(run_id, tokens, cost_usd, budget_usd, ratio, alerts,
  cache_hit_tokens, cache_miss_tokens, providers)`
- `ContextTrimmed(node, trimmed, before/after_chars, before/after_tokens, escalated)` ——
  每次 act/respond 裁剪后的实际输入规模；`trimmed=0` 也会发，便于基线对比
- `SteeringApplied(action, task_id)`
- `WorkflowRevised(reason, tasks)`
- `RunFinished` / `RunFailed` 额外带 `worker_input_tokens` /
  `worker_output_tokens`（主 agent token 仍单独记在 `input_tokens`）。

runtime 同时消费 `messages` / `updates` / `custom` 三种流，并用
`worker_id:attempt` 去重：custom 已发过的事件，updates 分支不再补发；直接调用
节点（没有 custom writer）时由 updates 分支兜底。

审计扩展：`workflow`、`task_id`、`role`、`worker_id`、`parent_task_id`。
前端需要从线性时间线升级为树状进度；CLI/TUI 先做简洁树，Web 直接推事件。

---

## 12. 成本与收敛

- 全局预算：`orchestrator_run_deadline_s`、任务/worker token、工具轮次；
- 任务级预算：`max_rounds`、`timeout`、`max_tokens`（explore/implement 已接入）；
- worker 级预算：独立轮次/超时；
- 调度边界：`orchestrator_checkpoint_every`；
- 限流退避：429 时降并发 + `orchestrator_rate_limit_backoff_s` 指数退避；
- 委派预算：复用仓库规模分级；
- 停滞检测：同一失败签名跨 repair/replan 累积；
- 递归上限：按子图 + checkpoint 边界推导，新增节点必须同步公式。

成本原则：**只有当并行能带来可测量的收益时才开启**；小任务仍走 `direct`。
编排默认开启，但只对达到 `orchestrator_min_files` 的仓库生效。

### 12.1 上下文与记忆

- **双预算**：`context_max_chars` 与 `context_max_tokens`（保守估算）任一超出即降档；
  最近 `keep_recent` 条也纳入总预算，只有「常规 `tool_chars` 裁剪」不碰它们；
- **400 自动降档**：`act` / `respond` 遇到 context-length 400 时用 `tighter_budget()`
  重裁一次并重试（字符/ token 减半、窗口减半），仍失败才抛出；
- **确定性摘要**：裁剪前从历史提取「用户目标 / 最近结论 / 最近工具动作」注入
  system prompt，不额外调模型；
- **记忆检索**：每次运行按请求相关度取 top-K（`memory_top_k`）注入；
  模型可在运行中用 `memory_search` 按需检索，`--write` 会话另有 `memory_save`；
  explore/review/implement worker 也按任务目标注入相关记忆；
- **记忆治理**：写入前扫描密钥；高度相似的事实保留最新版本；来源/时间/过期
  记录在 `memory.meta.json`；跨进程写入用沙箱内 `mkdir` 锁串行化；
- **分层指令**：`AGENTS.md` / `CLAUDE.md` 从根目录到 4 层子目录发现并合并，
  与长期记忆分开管理；
- **可观测**：`ContextTrimmed` 事件给出裁剪前后字符/ token 与是否降档；
  `CostReport` 增加 provider prefix cache 命中/未命中 token；
- **会话时间旅行**：`checkpoints` 列表 + `fork` / `rewind`；回退是在旧
  checkpoint 上新建分支，历史不删除；checkpoint 按 `checkpoint_keep` 轮转；
- **专项标尺**：`tests/eval/context_memory_bench.py` 测长会话压缩后的关键信息
  保持率与记忆 recall@K（确定性、无模型）。

---

## 13. 评测

### 13.1 指标（Phase 4 已补齐）

- 完成率（按套件/档位）；
- 墙钟、**主 agent / worker / total** 输入输出 token；
- worker 数、最大并发（按 `WorkerStarted/Finished` 的**活跃数**统计）、
  worker 状态与**角色分布**；
- **`worker_seconds` 与 `worker_parallelism`**（平均并发度）；
- 任务图规模、依赖深度、blocked 比例；
- **合并尝试、冲突数、merge 成功率、冲突率**；
- **干预延迟**：`steering_bench.py` 分别报告 wait（提交→边界）/
  apply（边界→事件）/ resume，不含模型时间。

### 13.2 方法

- 先看机制指标（worker 是否真的跑了、并发是否真的发生），再看通过率；
- 单次运行不足以判断；至少 `--repeat 2`，差异必须大于观测到的 spread；
- 用 `tests/eval/compare.py` 对比两份 baseline：**Δ 落在两臂 spread 内时
  明确拒绝下结论**；臂内翻过的任务标为 `noise_suspect`；
- A/B 两臂只差一个开关（`AGENT_ORCHESTRATOR_ENABLED`）；
- 记录工作区是否干净、模型、沙箱能力（pytest 等）。

### 13.3 任务选择

- `long/discount_and_audit`：两条独立功能线，适合验证并行探索；
- `long/discount_pipeline`：单条跨文件功能线，适合验证汇总质量；
- 后续增加多模块、多交付物任务，避免只测一种形态。

---

## 14. 路线图

### Phase 3A：动态工作流选择 + 最小只读 worker（已实现）

**目标**：证明「根据提示词和仓库内容选择工作流 → 并发只读 worker → 汇总 → 主 agent」这条链路。

**已交付**：

- `direct` / `parallel_readonly` 两个模板；
- `survey`（复用 RepoProfile + 测试命令探测）；
- `select_workflow`（规则优先 + LLM 兜底）；
- `plan_tasks`（2–6 个 explore/test 任务，校验/降级）；
- `scheduler ⇄ steering_dispatch`（事件驱动并发、依赖即时解锁、失败隔离、
  checkpoint 边界、动态只读提议）；
- `synthesize` → 现有 `planner`；`worker_context` 注入 planner/act；
- `WorkflowSelected` / `TaskGraphCreated` / `WorkerStarted` / `WorkerFinished` /
  `OrchestrationFinished` 事件与审计；
- `AGENT_ORCHESTRATOR_*` 配置；小仓库仍走 `direct`。

**历史烟雾**（`long/discount_and_audit`，单轮、不可比）：通过 1/1，
`parallel_readonly`，5 任务 / 4 worker（最大同批 4）。当前评测口径与验证记录见
[tests/eval/README](../tests/eval/README.md)。

### Phase 3B：显式任务 DAG + 只读 worker 扩展（已实现）

**已交付**：

- `depends_on` 调度、依赖失败传播；默认 `partial` 不满足依赖；
- **`depends_mode=any`**：`review`/`test` 可以只消费依赖的产出（success/partial），
  不再因为实现任务未通过完整验证而永远 blocked；failed/blocked/cancelled 仍阻塞；
- 任务级失败重试（`AGENT_ORCHESTRATOR_TASK_RETRIES`，默认 1）；
- `review` worker：复用只读工具循环 + 审查提示，返回 `[blocking]/[warning]` 发现；
- fan-in 只看每个任务的**最后一次**尝试，并报告多 worker 关注同一文件；
- `WorkerFinished.attempt`、审计 `attempt`、eval `worker_retries` 指标。

**烟雾**：`long/discount_and_audit` 通过；4 任务 / 4 worker，最大同批 3，
test worker 失败后重试 1 次，最终自动验证通过。review 任务在该轮因依赖 partial
结果被阻塞，review worker 本身由单测覆盖。

**仍缺**：任务级预算/超时细分、review 与实现后的自动回灌（3C/后续）。

### Phase 3C：并发写 worker（已实现，默认关闭）

**已交付**：

- `implement` role；任务图必须给出 `write_scope`，不同 implement 的 scope
  不能重叠，implement 之间不能互相依赖；
- 每个 implement worker 在独立 `git worktree` 中运行，工具只作用于该 worktree；
- 完成后导出 binary patch，scheduler 在主工作区**顺序** `git apply`；
  先 `--check`，失败则不改主工作区并标 failed；
- 写模式前置条件：`AGENT_ORCHESTRATOR_WRITE_ENABLED=true`、会话 `--write`、
  工作区是**git 仓库根**且干净；`.agent/` 与 `__pycache__/` 不计入脏改动；
- 合并前先 `git apply --check`，再为补丁涉及的主工作区文件留快照；
  `worker_results` 记录 `snapshot_ids`，`agent undo` 可回滚合并；
- 同一批 implement 补丁作为**一个事务**：全部 check/快照后顺序 apply；
  任一失败反向撤销已应用补丁，必要时恢复快照，避免半合并状态；
- worker 内部工具调用逐条写审计（含 `agent_id`/`task_id`），eval 记录
  `worker_tool_calls` / `worker_merged`；
- 任务图校验失败时，写模式退化为**一个** implement 任务：优先覆盖源码顶层目录，
  扁平仓库退到顶层文件；两者都没有时显式报告降级，不静默消失；
- worktree/patch 文件名在截断后附加短哈希，避免长任务 id 共用路径；
- implement 的 git/worktree/快照/补丁操作都走 `asyncio.to_thread`，scheduler 的
  合并也如此，避免同步 `wsl.exe` 调用阻塞其它 worker 与事件流。

**烟雾实测 1**（`long/discount_and_audit`，写编排开启）：

| 项 | 值 |
|---|---|
| 结果 | **通过 1/1** |
| 工作流 | `orchestrated_implement` |
| 任务 / worker | 1 / 1（planner 写范围重叠，退化为单 implement） |
| worker 状态 | success |
| 合并 | **1 个 patch 已合并** |
| worker 内部工具调用 | **33** |
| 主图工具调用 | 1 |
| input / output tokens | 9,103 / 244 |
| 墙钟 | 54.1s |

**烟雾实测 2（多写并发）**（`long/parallel_modules`，两个独立文件）：

| 项 | 值 |
|---|---|
| 结果 | **通过 1/1** |
| 工作流 | `orchestrated_implement` |
| 任务 / worker | 3 / **2**（两个 implement 同批） |
| 最大同批 | **2** |
| worker 状态 | partial×2（各自验证时对方功能尚未合并） |
| 合并 | **2 个 patch 已合并** |
| worker 内部工具调用 | 12 |
| 主图工具调用 | 5 |
| input / output tokens | 34,107 / 1,085 |
| 墙钟 | 39.6s |

两个 implement worker 在各自 worktree 中并发修改 `app/alpha.py` /
`app/beta.py`，宿主顺序合并后主 agent 最终验证 7 passed。依赖 partial worker
的 test 任务被标 `blocked` —— 这是 Phase 3B 依赖规则的预期行为。

这轮还修掉一个并发缺陷：worktree 与 patch 都在主工作区 `.agent/` 下，第二个
worker 的脏检查会把它们误判为用户改动。`implement._git_ready` 现在用与补丁
收集相同的 pathspec 排除 `.agent/` 与 `__pycache__/`，并有真实 WSL 回归测试
`test_concurrent_implement_workers_ignore_agent_state`。

**已知限制**：

- 并发写要求任务之间 write_scope 不重叠。`long/discount_and_audit` 的两条功能线
  共享 `app/service.py`，planner 的写范围会重叠，因此该任务实际退化为单个
  implement worker；多写并发由 `long/parallel_modules` 覆盖。
- 共享文件的多 worker 合并（3-way rebase）未实现；当前通过 scope 校验避免。
- 自动验证失败的 implement patch 仍可能以 `partial` 合并，由主 agent 的最终
  verify/repair 收口。

### Phase 3D：实时用户干预（已实现）

- [x] `SteeringController` 按 thread 隔离；runtime `steer()` 入队并写审计；
- [x] pause 用 `interrupt()` + checkpoint 挂起，`resume_steering()` 恢复；
- [x] cancel 协作式停止 worker；未合并 patch 丢弃，已合并改动保留；
- [x] `/add` 追加只读任务；`/replan` 保留已完成任务并重建剩余任务图；
- [x] `SteeringApplied` / `WorkflowRevised` 事件与 `kind=steering` 审计；
- [x] TUI 命令、暂停状态与新提示；无头 TUI 测试覆盖；
- [x] `ruff`、CI 子集、全量 unit、WSL 集成通过。

### Phase 4：评测与调优

**首切片已实现**：

- [x] worker token、total token、worker_seconds、worker_parallelism；
- [x] worker 角色分布、合并尝试/冲突/成功率；
- [x] `compare.py`：噪声带守卫 + 逐任务翻转 + `noise_suspect`；
- [x] `steering_bench.py`：pause/resume/cancel 延迟（wait/apply 分开）；
- [x] default 套件 `--repeat 2` 基线（见 tests/eval/README）；
- [x] A/B/C 三批加固：写编排脏检查/快照、实时 worker 事件、
      worker token 口径、去阻塞、`depends_mode`；
- [x] **Phase 4D**：worker 提议只读任务 + host 校验/总量上限 +
      `TaskGraphExtended` 事件。

**仍计划中**：

- 成本感知预算（全局/任务/worker token 上限）；
- 模型分层（worker 用更便宜的模型）；
- 扩展更难的能力评测任务（default 已饱和为 29/29，无法再量收益）；
- 在噪声带之外量化编排/并发的真实收益。

---

## 15. 与现有代码的映射

| 目标组件 | 复用/新增 |
|---|---|
| 仓库画像 | 复用 `agents/policy.py::profile_workspace`；补充顶层目录/测试命令 |
| explore worker | 复用 `agents/explore.py::make_explore_runner` |
| test worker | 复用 `tools/testrun.py::run_verification` |
| 任务图校验 | 新增 `orchestrator/graph.py` |
| 工作流选择 | 新增 `orchestrator/workflow.py` |
| 调度节点 | 新增 `orchestrator/nodes.py` |
| 图接线 | 修改 `graph/build.py`（条件入口）、`graph/routing.py` |
| 状态 | 修改 `graph/state.py`（新增 orchestrator 字段） |
| 事件 | 修改 `events.py`、`runtime.py` |
| 审计 | 修改 `audit/models.py` |
| 配置 | 修改 `config.py`、`.env.example` |
| 前端 | 修改 `cli/app.py`、`tui/app.py`（树状进度） |
| 评测 | 修改 `tests/eval/harness.py`、`runner.py` |

**不建议**：

- 不要在现有节点里塞 orchestrator 逻辑；
- 不要让 worker 直接写主 `messages`；
- 不要用不受限的动态图替换整个 LangGraph 拓扑；
- 不要在 Phase 3A 打开并发写。

---

## 16. 风险与取舍

| 风险 | 影响 | 对策 |
|---|---|---|
| 并发写冲突 | 数据损坏/覆盖 | Phase 3C 前不开放；worktree + 写范围 + 冲突检测 |
| 任务过度拆分 | token 成本、调度复杂度 | max_tasks + prompt 约束 + 校验降级 |
| 上下文隔离导致 worker 缺少细节 | 汇总不可用 | worker 必须返回文件/符号引用；主 agent 仍可再读 |
| 事件/状态复杂度 | 难维护 | 字段独立、节点纯增量、事件覆盖测试 |
| 递归上限算歪 | 正常任务被中断 | 公式化 + 图级回归 |
| 评测噪声 | 误判收益 | 先看机制指标；`--repeat 2` |
| 用户干预语义复杂 | 取消/回滚不一致 | Phase 3D 单独设计，先做 pause/cancel 最小集 |
| 只读编排不提升实现能力 | 收益有限 | 首个切片只证明选择/并发/汇总；写 worker 看 3C |

---

## 17. Phase 3A 验收清单

- [x] `AGENT_ORCHESTRATOR_ENABLED=false` 时，事件/状态/行为与现有基线一致。
- [x] 开启后，`direct` 与 `parallel_readonly` 能根据请求/仓库画像被选中。
- [x] `parallel_readonly` 至少 2 个 explore worker 并发（事件/指标可见）。
- [x] worker 失败不影响同批其它 worker；依赖失败标 `blocked`。
- [x] `synthesize` 的 `worker_context` 进入主 agent 的 planner/act，但**不写回**主消息历史。
- [x] `WorkflowSelected` / `TaskGraphCreated` / `WorkerStarted` / `WorkerFinished` /
      `OrchestrationFinished` 事件与审计齐全。
- [x] `estimate_recursion_limit` 覆盖编排路径。
- [x] `ruff`、全量 unit、CI 子集、WSL 集成通过。
- [x] 文档明确区分已实现与计划中。

> 单任务烟雾见 §14；通过率/成本仍需 `--repeat 2` 量化，不能据单次运行下结论。

---

## 18. 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 当前分层、状态、不变量
- [WORKFLOW.md](WORKFLOW.md) —— 当前运行/开发/使用工作流
- [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) —— 缺陷与阶段计划
- [ACCEPTANCE.md](ACCEPTANCE.md) —— 验收与已知限制
- [PLAN.md](PLAN.md) —— 早期开发方案（历史坐标）
