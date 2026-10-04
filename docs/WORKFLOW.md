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
START → planner ─→ act ─┬─(有 tool_calls)→ approval_gate ─→ tools ─┐
                        │                                          │
                        │←─────────────────────────────────────────┘
                        └─(本步做完)→ verify ─┬─(失败，还有预算)→ repair ─→ act
                                              ├─(预算耗尽/空转)──→ respond → END
                                              └─(通过/还有后续步)→ advance ─→ act
```

它由**三个正交机制**拼成，是设计里最关键的一点：

1. **规划-步进**（`planner` ⇄ `advance`）：把请求拆成有序子任务，一次只做一步
   （`compose_system_prompt` 注入"← 现在只做这一步"）。
2. **执行内核**（`act` ⇄ `tools`）：出意图 → 审批 → 执行 → 结果回灌，循环到本步做完或轮次用尽。
3. **验证-修复**（`verify` ⇄ `repair`）：改过东西就自动跑测试，失败带结构化错误重来，硬性上限。

三者通过**条件边**（`graph/routing.py`）组合；节点彼此不知道对方存在，加一个机制不用改其它两个。
节点**只读状态、返回状态增量**，不产事件、不写审计——那是 `runtime` 的职责。

### 2.2 状态生命周期

`AgentState`（`graph/state.py`）只放**必须跨节点**的东西，且每条字段都有明确的"谁重置"：

| 字段 | 生命周期 | 重置者 |
|---|---|---|
| `messages` | 全程累积（`add_messages`） | 只追加 |
| `plan` / `step_idx` | 本任务 | planner 初始化，advance 递增 |
| `tool_rounds` | **本步** | planner / advance / repair 归零 |
| `dirty` | **本步**是否改过东西 | planner / advance 清零、tools 置位、repair 置位 |
| `verification` | 本步最近一次验证 | planner / advance 清空 |
| `retry` | 本步修复次数 | planner / advance 清零、repair 递增 |
| `budget_exhausted` | 本步是否撞上工具轮次上限 | planner / advance / repair 复位 |
| `approvals` | **逐次调用**，用后即清 | approval_gate 写入、tools 清空 |

**为什么重要**：把"每步"与"每轮"两类生命周期分开，是图能收敛的前提；
`approvals` 的"用后即清"是**安全属性**而非清理——不清空会导致下一轮复用上一轮的批准。

### 2.3 路由决策（全部条件边）

| 边 | 判定 | 依据 |
|---|---|---|
| `route_after_act` | 有 `tool_calls` → **approval_gate**；否则 → **verify** | 信任"有 tool_calls 就一定有预算"（act 超预算时不产 tool_calls，见 `nodes/act.py` 不变量） |
| `route_after_verify` | `failed` 且 `retry < max` → **repair** | 失败驱动修复 |
| | `failed` 且到上限 → **respond** | 如实上报"试过了没修好" |
| | `budget_exhausted` 且 `not dirty` → **respond** | 空转步骤不静默跳过 |
| | 其余 → **advance**（还有步）/ **respond** | 步进或收尾 |

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

- 节点不产事件；前端测试不关心图，节点测试不关心事件。
- 流式只在 `act` / `respond`（`messages` 模式），planner 的结构化调用不流式。
- **挂起时以 `ApprovalRequested` 收尾，不发 `RunFinished`** → 前端据此判断该 resume 还是收工。
- 前端三种实现（CLI/TUI/Web）共用同一套 `Event`；前端不得导入 `tools`/`sandbox`（AST 测试守着）。

### 2.6 防失控的闸

| 闸 | 默认 | 作用 |
|---|---|---|
| `max_tool_rounds` | 12/步 | 单步工具调用轮次 |
| `max_plan_steps` | 5 | 子任务数 |
| `max_repair_rounds` | 3/步 | 修复次数硬上限 |
| `recursion_limit` | `5×(3×12+3)+10` | 图超步防跑飞（系数按拓扑推导） |
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

## 3. 开发工作流

```
conda activate agent → make check（ruff + 快反馈）→ make test-all（含真实 WSL）
测试分三层：unit（假图驱动，无沙箱） / integration（真实 WSL，自动跳过） / tui·web（无头驱动）
CI（ubuntu，无 WSL）: pip install -e ".[dev,ui,web]" → ruff → pytest -m "not wsl and not llm"
```

- **本地三档**：`not wsl and not llm`（快，~40s）→ `wsl`（真实沙箱）→ `llm`（真调模型，需 Key）。
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

---

## 5. 总体评价

| 维度 | 评价 |
|---|---|
| 编排清晰度 | **A**：三机制正交、路由纯函数、状态生命周期明确 |
| 安全性 | **A-**：决定/执行分离 + fail-closed + 路径/审批无旁路；纵深项（bwrap）可选 |
| 可测性 | **A-**：不变量即测试；"unit 不得触达 WSL"有自动守卫 |
| 收敛性 | **A**：预算 / 重试 / 超步三重上限 |
| 鲁棒性 | **A-**：启动失败也事件化；剩余为成本与并发项 |
| 成本控制 | **B**：按轮次/字符，非 token |

## 6. 后续改进（按性价比）

| 优先级 | 改进 | 要点 |
|---|---|---|
| — | 暂无 | 工作流层面的已知项已全部处理 |

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
