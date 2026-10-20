# 工作流设计

> 面向实现：说清项目里**三套工作流**（运行 / 开发 / 使用）的设计、关键状态与决策，
> 以及已知张力与改进项。架构分层与不变量见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 1. 三套工作流

| | 工作流 | 载体 | 回答的问题 |
|---|---|---|---|
| **A** | **运行工作流**（Agent 编排图） | `graph/` + `runtime.py` | 模型怎么从一句话走到完成任务 |
| B | 开发工作流 | conda env + `Makefile` + `.github/workflows/ci.yml` | 人怎么改、测、交付 |
| C | 使用工作流 | `cli/` `tui/` `web/` | 用户怎么驱动与干预 |

---

## 2. 运行工作流（核心）

### 2.1 拓扑

```
START → route_entry
  ├─ direct ─→ planner → act ⇄ approval_gate → tools
  │                     → verify → review → advance → steering_step → replan → act/respond
  └─ orchestrated ─→ orchestrator 子图：
                       survey → select_and_plan
                       → scheduler ⇄ steering_dispatch → fan_in
                       → planner → ...
```

direct 路径的失败分支：verify/review 没过且还有预算 → repair → act；预算用尽 /
同一失败签名停滞 → replan（换做法或砍掉剩余步骤）→ act/respond。
`nudge` 给“一步没有任何改动”的实现步一次重做机会。

它由**三个正交机制**拼成，是设计里最关键的一点：

1. **规划-步进-重规划**（`planner` ⇄ `advance` ⇄ `replan`）：把请求拆成有序子任务，
   一次只做一步（`compose_system_prompt` 注入"← 现在只做这一步"）。
   `replan` 在每次步进后重新审视**剩余步骤** —— 计划可以在执行中被修正。
2. **执行内核**（`act` ⇄ `tools`）：出意图 → 审批 → 执行 → 结果回灌，循环到本步做完或轮次用尽。
3. **验证-修复**（`verify` ⇄ `repair`）：改过东西就自动跑测试，失败带结构化错误重来，硬性上限。
   多步任务只在 `kind=verify` 或最后一步跑全量 —— 早期实现步只完成了一部分，
   每步都跑全量会被后面还没做的测试卡死（`AGENT_VERIFY_ONLY_FINAL=false` 可关）。
   `kind=verify` 步即使 `dirty=False` 也会跑，因为它的职责就是全量确认。

**修复与重规划的分工**：`repair` 修的是**这一步**（验证没过，重做），`replan` 修的是
**后面**（计划本身不对了）。两者都有硬性次数上限，都不是"发现问题就无限重试"。

三者通过**条件边**（`graph/routing.py`）组合；节点彼此不知道对方存在，加一个机制不用改其它两个。
节点**只读状态、返回状态增量**，不产事件、不写审计——那是 `runtime` 的职责；
唯一例外是 `scheduler` 用 custom stream 实时推送 worker 开始/结束。

### 2.2 状态生命周期

`AgentState`（`graph/state.py`）只放**必须跨节点**的东西，且每条字段都有明确的"谁重置"：

| 字段 | 生命周期 | 重置者 |
|---|---|---|
| `messages` | 全程累积（`add_messages`） | 只追加 |
| `plan` / `plan_kinds` / `step_idx` | 本任务 | planner 初始化，advance 递增，replan 改写剩余部分（文本与 kind 同步） |
| `replan_count` | **整个任务**的额度 | planner 清零、replan 递增（advance 不动它） |
| `tool_rounds` | **本步** | planner / advance / repair 归零 |
| `dirty` | **本步**是否改过东西 | planner / advance 清零、tools 置位、repair 置位 |
| `verification` | 本步最近一次验证 | planner / advance 清空 |
| `retry` | 本步修复次数 | planner / advance 清零、repair 递增 |
| `same_failure_count` / `last_failure_signature` | 连续同一失败签名的次数与签名 | verify 写入；repair / replan 不清；只有 planner / advance 归零 |
| `repo_profile` / `delegation_allowed` / `delegation_budget` | 仓库画像与委派预算 | runtime 注入 |
| `budget_exhausted` | 本步是否撞上工具轮次上限 | planner / advance / repair 复位 |
| `approvals` | **逐次调用**，用后即清 | approval_gate 写入、tools 清空 |

**为什么重要**：把"每步"与"每轮"两类生命周期分开，是图能收敛的前提；
`approvals` 的"用后即清"是**安全属性**而非清理——不清空会导致下一轮复用上一轮的批准。

`plan_kinds` 与 `plan` 逐项对齐，取值 `explore / implement / verify / unknown`：
探索与验证步允许零改动直接步进；实现步没有实际改动时先 nudge 一次，仍不落笔
就如实收尾，而不是静默进入下一步。这是「委派探索做完读、主 agent 却声称实现完成」
的直接修复。

### 2.3 路由决策（全部条件边）

| 边 | 判定 | 依据 |
|---|---|---|
| `route_after_act` | 有 `tool_calls` → **approval_gate**；否则 → **verify** | 信任"有 tool_calls 就一定有预算"（act 超预算时不产 tool_calls，见 `nodes/act.py` 不变量） |
| `route_after_verify` | `failed` 且同一签名连续 ≥ `max_same_failures` → **replan** | 原地打转，继续 repair 只会重复同一类修改；replan 后同一签名继续累积（不变量 30） |
| | `failed` 且 `retry < max` → **repair** | 失败驱动修复 |
| | `failed` 且到上限 → **respond** | 如实上报"试过了没修好" |
| | `budget_exhausted` 且 `not dirty` → **respond** | 空转步骤不静默跳过 |
| | 其余 → **advance**（还有步）/ **respond** | 步进或收尾 |
| `route_after_review` | `kind in (explore, verify)` → **advance/respond** | 这两类步骤本来就不产生改动 |
| | `kind=implement` 且 `not dirty` 且未 nudge → **nudge** | 给一次真正落笔的机会 |
| | `kind=implement` 且 nudge 过仍 `not dirty` → **respond** | 不能把"只读了"当成实现完成（不变量 29） |

路由是**纯函数、无 I/O、无副作用**，因此可单独测（`tests/unit/test_routing.py` 覆盖每个分支）。

### 2.4 审批语义：决定与执行分离

- **`approval_gate` 只决定**（放行/询问/拒绝），保持纯函数——`interrupt()` 恢复时节点会从头重跑。
- **`tools` 节点强制**：所有工具执行的唯一咽喉，没拿到 `auto`/`approved` 一律不执行。
- **fail closed**：审批记录缺失、答复无法解析、`call_id` 空/重复、非交互环境，一律拒绝。
- **跨挂起的状态保留**：审批结果配对表（`_pending_calls`）与翻译事件用的进度状态
  （`_progress`：`plan`/`step_idx`/`last_verification`）都放在 **runtime 实例级、按 `thread_id` 分桶**——
  因为挂起与恢复是**两次** `_stream`，只存局部变量会在恢复后丢参数、丢文案。

### 2.5 事件与审计：同源产出

`runtime._stream` 消费 LangGraph 的 `updates`，**同一份更新**既翻译成领域事件（给前端）又写成审计：

- 节点不产事件（例外：`scheduler` 的 custom stream）；前端测试不关心图，
  节点测试不关心事件。
- 流式只在 `act` / `respond`（`messages` 模式），planner 的结构化调用不流式。
- **挂起时以 `ApprovalRequested` 收尾，不发 `RunFinished`** → 前端据此判断该 resume 还是收工。
- 前端三种实现（CLI/TUI/Web）共用同一套 `Event`；前端不得导入 `tools`/`sandbox`（AST 测试守着）。

### 2.6 防失控的闸

| 闸 | 默认 | 作用 |
|---|---|---|
| `max_tool_rounds` | 12/步 | 单步工具调用轮次 |
| `max_plan_steps` | 5 | 子任务数 |
| `max_repair_rounds` | 3/步 | 修复次数硬上限 |
| `max_replans` | 2/任务 | 重建剩余计划的次数上限（到顶后不再问模型） |
| `recursion_limit` | 由 `estimate_recursion_limit()` 推导 | 图超步防跑飞；公式覆盖 repair/replan/nudge/steering/orchestrator/任务扩展，改拓扑必须同步 |
| 沙箱资源 | CPU 600s / 文件 512MB / 进程 1024 / 墙钟 60s | 单条命令层面 |

### 2.7 错误处理与降级

- **工具异常回灌模型**（`tools` 节点捕获），不中断图——这是"失败驱动修复"的基础。
- **编排异常 → `RunFailed` 事件**；**启动即失败**（如读记忆需要 WSL 而不可用）同样以 `RunFailed`
  收尾并记 `run_error`，不让异常裸抛给前端。
- **审计写失败显式报出**——宁可运行失败，也不静默丢记录。
- **planner 规划失败降级**为"整个请求当一步"，图不因规划失败而崩。

### 2.8 评价（优点）

1. **正交机制 + 纯路由**：三个循环各自可测、可替换。
2. **不变量可测**：`ARCHITECTURE.md` 第 6 节逐条有测试，改动相关代码前有据可依。
3. **安全属性而非代码风格**：前端无旁路执行。
4. **事件契约稳定**：加前端不动编排。

### 2.9 已知张力与改进项

| # | 问题 | 影响 | 状态 |
|---|---|---|---|
| W1 | `plan/step_idx/last_verification` 曾是 `_stream` 局部 | 恢复后 `StepStarted` 空文案 | **已修**（提升为 `_progress`） |
| W2 | `dirty` 曾只认工具名 | shell `sed -i` 不触发 verify | **已修**（按命令等级判定） |
| W3 | `budget_exhausted` 曾不参与路由 | 空转步骤被静默跳过 | **已修** |
| W4 | 预算按轮次/字符，非 token | 长会话成本不精确 | 设计取舍 |
| W5 | 单线程假设 | 同一 runtime 跨 thread 复用靠分桶状态 | 被前端单会话使用遮掩 |
| W6 | `run()` 读记忆是 WSL 硬依赖 | 无沙箱环境不可用 | **已缓解**（失败转事件 + 单测注入） |
| W7 | `recursion_limit` 系数靠注释解释 | 改拓扑易再次算歪 | **已修**（`estimate_recursion_limit()`，含单测） |

---

### 2.10 Phase 1：只读 Explore 子智能体

主模型可以在 `act` 阶段调用 `delegate_explore(goal, paths, reason)`，把一次只读探索
交给独立上下文的子智能体：

```
act ──delegate_explore──▶ approval_gate（L0，自动放行）──▶ tools（宿主拦截）
                                                             │
                          ┌──────────────────────────────────┘
                          ▼
                  agents/explore.py
                  独立 messages（只有系统提示 + 本次任务）
                  file_read / search_code / find_files 循环
                  ↓ 独立结构化调用
                  SubAgentArtifact（summary / findings / files / tokens）
                          │
                          ▼
                  ToolMessage 回灌主模型（只有压缩摘要）
```

要点：

- **上下文隔离**：子智能体的工具输出不进入主 `messages`；主模型只拿到摘要。
- **不能写**：只绑定 `EXPLORE_TOOL_NAMES`；构造时与运行时各校验一次。
- **仍走审批**：delegate 本身是 L0 工具，经过 `approval_gate → tools`；
  子智能体内部只读工具不再逐条审批，因为白名单保证没有副作用。
- **并发 fan-out**：同一条 `act` 消息里的多个 `delegate_explore` 由 `tools` 节点
  用 `asyncio.Semaphore + gather` 并发执行，结果按 `call_id` 回填。并发度由
  `AGENT_MAX_CONCURRENT_AGENTS`（默认 4）限制；单个子智能体失败只影响自己。
  fan-in 发生在主模型下一轮：它一次性收到所有子智能体的摘要。
- **不要混写**：同一条消息里不要同时委派探索和 `file_write/file_edit`；
  探索读取的是改动前的状态。提示词里已明确要求。
- **成本感知**：runtime 在 `run()` 时探一次仓库文件数，按规模给委派预算：
  `<30` 文件不委派；`30–99` 最多 2 次；`100–299` 最多 4 次；`≥300` 用
  `AGENT_MAX_DELEGATE_CALLS`。只有 `kind=explore` 的步骤允许委派；
  实现/验证步一律由主 agent 自己完成。
- **有界**：`AGENT_EXPLORE_MAX_ROUNDS`（8）、`AGENT_EXPLORE_TIMEOUT`（120s）、
  `AGENT_MAX_DELEGATE_CALLS`（6/任务）、`AGENT_MAX_CONCURRENT_AGENTS`（4）。
- **默认关闭**：`AGENT_EXPLORE_ENABLED=false`，不改变既有行为与评测基线。
- **事件**：`SubAgentStarted` / `SubAgentFinished`；多个并发子智能体按
  `call_id` 配对。审计里每条子工具调用都带 `agent_id` / `parent_call_id`。

### 2.11 编排模式（独立子图 + 事件驱动 DAG，默认启用）

> 完整方案见 [HARNESS.md](HARNESS.md)。

运行模式：

```
START → route_entry
  ├─ direct ───────────→ planner → act → ...          （小任务快速路径）
  └─ orchestrated ─────→ orchestrator 子图
                          survey → select_and_plan
                          → scheduler ⇄ steering_dispatch
                          → fan_in → planner → ...
```

- 开关：`AGENT_ORCHESTRATOR_ENABLED`（默认 `true`）；
- 模板：`direct` / `parallel_readonly` / `orchestrated_implement`；
- **事件驱动调度**：scheduler 用 ready 堆 + `asyncio.wait(FIRST_COMPLETED)`，
  任务完成立即解锁依赖；失败立即短路，不等整批；
- 只读 worker：explore/test/review 并发；任务级失败重试带上次失败上下文
  （`AGENT_ORCHESTRATOR_TASK_RETRIES`）；`depends_mode=any` 的 review/test
  可消费 partial 产出；
- **动态任务图**：explore/review 可提议最多 2 个只读任务，scheduler 在完成点
  做 host 校验（角色/去重/依赖/总量/无环）后立即入队；
- 写 worker：需要 `AGENT_ORCHESTRATOR_WRITE_ENABLED=true`、`--write`、
  git 仓库根 + 干净工作区；每个 implement 在独立 worktree 中修改，
  `write_scope` 在 I/O 前强制校验，完成后由宿主串行合并 patch 并留快照；
- fan-in：按失败/阻塞优先、任务级预算输出 `worker_brief`，完整结果落
  `.agent/artifacts/`；
- 实时干预（TUI）：`/pause` `/resume` `/cancel` `/add` `/replan`；
  scheduler 在 `checkpoint_every` 边界 drain 后消费，pause 走 checkpoint；
- worker 的启动/结束通过 custom stream 实时发出；
- 关闭时完全走现有单 agent 路径，行为与基线一致。

编排结束后主 agent 仍负责最终验证与答复；写路径的审批、快照与审计仍是唯一
安全边界。烟雾结果见 [HARNESS.md §14](HARNESS.md)。

---

## 3. 开发工作流

```
conda activate agent → make check（ruff + 快反馈）→ make test-all（含真实 WSL）
测试分三层：unit（假图驱动，无沙箱） / integration（真实 WSL，自动跳过） / tui·web（无头驱动）
CI（ubuntu，无 WSL）: pip install -e ".[dev,ui,web]" → ruff → pytest -m "not wsl and not llm"
```

- **本地三档**：`not wsl and not llm`（快，约 1 分钟）→ `wsl`（真实沙箱）→
  `llm`（真调模型，需 Key，仅 3 条）。
- **CI 与本地互补**：CI 无 WSL，只跑无沙箱用例。因此有两条硬约束——
  ① 标为 unit 的用例**真的不能碰 WSL**；② CI 必须装齐 `ui`/`web` extras（TUI/Web 测试需要）。
  这两条都曾导致过 CI 失败。
- **自动守卫**：`tests/conftest.py` 对未标 `wsl`/`wsl_env` 的用例把 `WslSandbox._exec` 换成当场失败，
  任何平台生效——"漏标"在本地就红，而不是等 Linux CI。显式探测宿主环境的用例（`doctor`）标
  `@pytest.mark.wsl_env`。
- **DoD**：`ruff` 干净 + 全量测试通过 + 不破坏 `ARCHITECTURE.md` 第 6 节不变量。

## 4. 使用工作流

| 前端 | 驱动 | 审批 | 特点 |
|---|---|---|---|
| CLI `agent run` | 消费事件 → 渲染 → 收集 `ApprovalRequested` → 提问 → `resume` 循环 | 逐条问，非交互默认拒绝 | 最简 |
| TUI `agent tui` | Textual：事件→渲染→弹窗→`resume` 直到跑完 | `ApprovalScreen`（Esc 默认拒绝） | 主界面，斜杠命令 |
| Web `agent web` | SSE 直接推 `event.model_dump_json()` | **固定 deny**（无审批交互） | 最小验证 |

三者的 resume 循环结构相同，都基于"**挂起以 `ApprovalRequested` 收尾**"这条契约。

### 4.1 运行中干预（TUI）

运行中仍可输入斜杠命令；它们进入 `SteeringController`，由图节点在边界消费：

| 命令 | 生效点 | 行为 |
|---|---|---|
| `/pause` | `steering_plan` / `steering_dispatch` / `steering_step` / `steering_final` | `interrupt()` 挂起；当前节点结束后不再派发新 worker |
| `/resume` | 挂起状态 | `Command(resume=...)` 从 checkpoint 继续 |
| `/cancel` | 同上 | 协作式停止 worker；未合并 patch 丢弃，已合并改动保留 |
| `/add [角色] <目标>` | orchestrated：任务图；direct：计划 | 只接受 explore/test/review |
| `/replan` | orchestrated：`plan_tasks`；direct：`replan` | 只重建剩余工作，已完成任务不重跑 |

暂停期间普通 prompt 会被拒绝，必须先 `/resume` 或 `/cancel`。事件侧新增
`SteeringApplied` 与 `WorkflowRevised`；审计写 `kind=steering`（含来源与动作）。
CLI 用 `agent steer --thread-id <id> pause|cancel|add|replan` 注入指令；
Web 端点提供 `/api/steer`、`/api/resume`、`/api/resume_steering`，
页面也带审批与暂停/恢复按钮。

---

## 5. 总体评价

| 维度 | 评价 |
|---|---|
| 编排清晰度 | **A**：三机制正交、路由纯函数、状态生命周期明确 |
| 安全性 | **A-**：决定/执行分离 + fail-closed + 路径/审批无旁路；纵深项（bwrap）可选 |
| 可测性 | **A-**：不变量即测试；"unit 不得触达 WSL"有自动守卫 |
| 收敛性 | **A**：预算 / 重试 / 超步三重上限 |
| 鲁棒性 | **A-**：启动失败也事件化；剩余为成本与并发项 |
| 成本控制 | **B**：按轮次/字符，非 token；worker token 已计量但未设硬预算 |

## 6. 后续改进（按性价比）

| 优先级 | 改进 | 要点 |
|---|---|---|
| — | 全局/任务/worker token 预算 | **已实现**：任务级硬停 + scheduler 派发前角色预留准入 |
| — | Web/CLI 实时控制入口 | **已实现**：Web SSE 审批/steering/resume + `agent steer` |
| — | 共享文件的多 worker 合并 | **已实现**：`AGENT_ORCHESTRATOR_ALLOW_OVERLAP` + 临时 index 3-way 预演 |
| — | steering 队列持久化 | **已实现**：`.agent/steering/` 原子落盘，`resume` 跨进程恢复 |

**已完成**：`recursion_limit` 拓扑推导（W7）、shell 变更"未留底"提示、每 thread 恢复次数上限
（`AGENT_MAX_RESUMES`）、token 用量累计（`usage_metadata` → `run_end` 审计，未引入 tokenizer）、
审计按大小轮转（`AGENT_AUDIT_MAX_MB`，读取合并当天各片段）。

**并发假设**：单个 `AgentRuntime` 实例按「单会话、单线程」使用；跨 thread 复用靠按 thread
分桶的状态（`_pending_calls` / `_progress` / `_resume_counts`）。若要并发跑多个会话，
应为每个会话各建一个 runtime——当前三种前端（CLI/TUI/Web 各请求）都满足这一假设。

## 7. 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 分层、数据流、**不变量**、扩展点
- [THREAT_MODEL.md](THREAT_MODEL.md) —— T1/T2 威胁模型（沙箱边界）
- [ENVIRONMENT.md](ENVIRONMENT.md) —— 环境配置完整流程
- [USAGE.md](USAGE.md) —— 工作流用法、配置、故障排查
