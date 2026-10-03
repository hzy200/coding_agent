# 架构

面向实现：分层、数据流、不变量与取舍理由。开发方案与排期见 [PLAN.md](PLAN.md)。

## 1. 一条原则

> **宿主内置工具，模型只输出意图。**

LLM 拿不到操作系统句柄，只能产出结构化调用 `{tool, args}`；宿主校验后在 WSL2 沙箱里执行。
文件修改走专用工具（精确替换 + diff + 写前备份），不经 shell。

由此推出四件事：

| 后果 | 实现 |
|---|---|
| 模型说什么不算数，宿主说了算 | 命令分级在 `sandbox/policy.py`，不看模型自报 |
| 危险操作过不去 | L2/L3 先挂起等审批；不批就不执行 |
| 改动可回滚 | 写盘前快照，恢复前再留底 |
| 一切可追溯 | 审计由 runtime 写，前端绕不过 |

## 2. 分层与依赖方向

```
┌──────────────────────────────────────────────────────────┐
│ 前端  cli/   tui/   web/                                  │
│   只消费领域事件，**不得导入 tools/ 或 sandbox/**           │
└───────────────────────┬──────────────────────────────────┘
                        │ AsyncIterator[Event]
┌───────────────────────▼──────────────────────────────────┐
│ runtime.py   AgentRuntime                                 │
│   唯一编排入口：持有图、沙箱、安全策略、审计、快照存储        │
│   把 LangGraph 的原始流翻译成领域事件                       │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ graph/  state.py routing.py build.py nodes/               │
│   planner → act ⇄ (approval_gate → tools) → verify → …    │
│   节点只读状态、返回状态增量；不碰 I/O 以外的宿主资源         │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ tools/   shell files git deps search testrun              │
│   能力层：执行 + 路径守卫 + 产出结构化 artifact             │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ sandbox/ wsl_exec fs snapshots policy pathguard limits    │
│   WSL2 进程、路径校验、命令分级、资源上限                   │
└──────────────────────────────────────────────────────────┘
        events.py  config.py  messages.py  diffing.py  audit/  memory/
                          （横向：各层都可依赖）
```

**依赖方向是单向的**，`tests/unit/test_architecture.py` 用 AST 扫描守住最关键的一条：
`tui/` 与 `web/` 若导入 `coding_agent.tools` 或 `coding_agent.sandbox` 直接失败。

为什么这条最重要：一旦前端能直接调工具，就会出现**绕过审批的第二条执行路径** ——
Web 页面上一个按钮就能跳过命令分级。这条约束是安全属性，不是代码风格。

> `cli/` 是唯一的例外：`doctor` 与 `sandbox-init` 需要直接探测沙箱。
> 它们是诊断命令，不执行模型意图。

## 3. 目录职责

| 路径 | 职责 | 不该做的事 |
|---|---|---|
| `runtime.py` | 编排、事件翻译、审计、生命周期 | 不含业务规则（分级在 policy） |
| `events.py` | 前端契约（9 个事件） | 不含逻辑 |
| `graph/state.py` | 跨节点传递的状态 | 不放能推导出来的派生值 |
| `graph/nodes/` | 单个节点的纯函数 | 不直接 yield 事件（节点只返回状态增量） |
| `graph/routing.py` | 条件边 | 不做副作用 |
| `tools/` | 能力：执行 + 回报 | 不做安全判定（判定在 approval_gate） |
| `sandbox/policy.py` | 命令分级、会话策略 | 不执行命令 |
| `sandbox/fs.py` | 沙箱内文件读写 + 路径双重校验 | 不做业务判断 |
| `audit/` | JSONL 审计 | 不解析工具文本输出 |
| `memory/` | checkpoint / 会话索引 / 长期记忆 | 不持有业务状态 |

## 4. 一次完整运行

```
前端                  Runtime                     Graph                  沙箱
 │                      │                          │
 │─ run(prompt, tid) ──▶│                          │
 │                      │ 组装 inputs（含 memories 快照）
 │                      │ 组装 config（trace metadata）
 │                      │ 审计 run_start
 │                      │─ astream(payload) ──────▶│
 │                      │                          │─ planner ─┐
 │                      │◀─ updates{planner} ──────│           │ 分解子任务
 │◀─ PlanCreated ───────│                          │           │
 │◀─ StepStarted ───────│                          │◀──────────┘
 │                      │                          │
 │                      │◀─ messages{tokens} ──────│─ act ─────┐ 调模型
 │◀─ AssistantToken ────│                          │           │
 │                      │◀─ updates{act} ──────────│           │
 │◀─ ToolCallStarted ───│                          │◀──────────┘
 │                      │                          │
 │                      │                          │─ approval_gate ─┐
 │◀─ ApprovalRequested ─│◀─ __interrupt__ ─────────│                 │ 需要人工？
 │                      │                          │◀────────────────┘
 │  （前端收集答复后 resume）
 │                      │                          │─ tools ───▶│─ 校验 + 执行 ─┐
 │                      │◀─ updates{tools} ────────│◀───────────│◀─────────────┘
 │◀─ ToolCallFinished ──│  写审计 tool_call        │
 │◀─ FileChanged ───────│  写审计 file_change       │
 │                      │                          │
 │                      │◀─ updates{verify} ───────│─ verify ──▶│ 跑测试/构建
 │◀─ Verification ──────│  写审计 verify            │◀───────────│
 │                      │                          │
 │                      │                          │ 失败且还有预算 → repair → act
 │◀─ RepairStarted ─────│◀─ updates{repair} ───────│
 │                      │  写审计 repair            │
 │                      │                          │
 │                      │                          │─ advance / respond
 │◀─ StepStarted ───────│                          │
 │◀─ AssistantToken ────│  （respond 的 token）      │
 │◀─ RunFinished ───────│  写审计 run_end            │
```

要点：

- **节点不产生事件**。节点只返回状态增量；事件由 runtime 从 `updates` 里翻译出来。
  这样节点的测试不需要关心事件，前端的测试不需要关心图。
- **审计与事件同源**。两者都从同一份 `updates` 产生，不存在"界面显示了但没记账"。
- **挂起时以 `ApprovalRequested` 收尾**，不发 `RunFinished` —— 前端据此判断该 resume 还是收工。

## 5. 图状态与生命周期

| 字段 | 含义 | 谁重置 |
|---|---|---|
| `messages` | 对话与工具轨迹 | `add_messages` 追加 |
| `cwd` | 沙箱内工作目录 | 每次 run 由 runtime 注入 |
| `memories` | 项目长期事实 | 每次 run 从文件重读 |
| `plan` / `step_idx` | 子任务与进度 | planner 初始化，advance 递增 |
| `tool_rounds` | 当前步骤已用轮次 | planner / advance / repair 清零 |
| `budget_exhausted` | 本步因预算耗尽收尾 | 同上 |
| `dirty` | 本步是否改过东西 | 由 tools 置位，advance/planner 清零，repair 置位 |
| `verification` | 最近一次验证结果 | planner / advance 清空 |
| `retry` | 当前步骤的修复次数 | planner / advance 清零，repair 递增 |
| `approvals` | call_id → 审批结果 | approval_gate 写入，tools **消费后清空** |

`approvals` 的一次性语义是安全属性：不清空的话，下一轮的调用会复用上一轮的批准。
有测试守着（`test_approvals_do_not_leak_into_next_round`）。

## 6. 不变量

这些是几周里踩出来并逐一固化成测试的。改动相关代码前请先读它们。

| # | 不变量 | 为什么 | 测试 |
|---|---|---|---|
| 1 | **act 只在还有预算时产出 `tool_calls`** | 路由因此可无条件信任"有 tool_calls 就送去执行"，不会留下没人应答的 tool_calls（那会让下一轮 API 直接 400） | `test_act_stops_without_tool_calls_when_budget_exhausted` |
| 2 | **审批结果一次性** | 否则跨轮次误放行 | `test_approvals_do_not_leak_into_next_round` |
| 3 | **上下文裁剪只截断内容，绝不丢弃消息** | 丢消息会破坏 `tool_calls` ↔ `tool_call_id` 配对 | `test_preserves_tool_call_pairing` |
| 4 | **工具产物是结构化 artifact，不解析文本** | 文本是给模型看的，格式随时会变 | `test_parse_artifact_dispatches_on_kind` |
| 5 | **前端不得导入 tools / sandbox** | 否则出现绕过审批的第二条执行路径 | `test_ui_does_not_import_capability_or_sandbox` |
| 6 | **fail closed** | 审批记录缺失 / 答复无法解析 / 非交互环境，一律拒绝 | `test_missing_approval_fails_closed` |
| 7 | **审计失败显式报出** | 审计有缺口是这个项目不能接受的失败模式 | `test_audit_write_failure_surfaces_as_run_failed` |
| 8 | **所有写操作先留底** | 回滚本身也要可回滚 | `test_restore_is_itself_undoable` |
| 9 | **路径校验做两次**（词法 + realpath） | 词法挡不住"先建符号链接再穿透" | `test_symlink_escape_is_rejected` |

## 7. 关键取舍

### 为什么不直接用 `ToolNode`

`response_format="content_and_artifact"` 在 `tool.invoke()` 下**会丢弃 artifact**
（实测；该语义只在 `ToolNode` 内生效，而 `ToolNode` 在 langgraph 1.2.x 下无法脱离图调用）。
这条契约是事件层与审计层的地基，不适合押在语义不明的框架行为上，
改成工具返回 `artifacts.pack(text, artifact)` 自描述封装，由宿主显式拆开。

### 为什么审批在节点，不在工具里

审批结果是**逐次调用**的图状态，而工具是无状态的。把结果塞进模型可见的参数会被模型伪造；
塞进隐藏参数又要跟框架的 schema 校验搏斗。所以：

- `approval_gate` **只做决定**（放行 / 询问 / 拒绝），保持纯函数
  （`interrupt()` 恢复时节点会从头重跑）
- `tools` 节点**负责强制** —— 它是所有工具执行的唯一咽喉，没拿到 `auto`/`approved` 的调用一律不执行

### 为什么自动 verify 不走审批

区别在**谁决定执行什么**：verify 跑的是宿主从项目清单推导出的固定命令，
模型影响不了跑什么；模型主动调 `run_tests` 才走 L2 审批。

### 为什么结构化工具而不是让模型拼 shell

Git、依赖、检索都有专用工具。换来三件事：参数无法逃逸（逐参数 `shlex.quote`、
包名白名单正则）、等级可判定（不再对命令字符串猜正则）、审计可读。

### 为什么 `.env` 优先于环境变量

通用约定是反过来的，这里刻意偏离。原因是踩过真实的坑：某些工具链会在启动时
往进程环境注入值，用户既看不到来源也删不掉，导致改 `.env` 完全不生效。
项目目录里的 `.env` 是显式写下、看得见、可编辑的意图。详见 [USAGE.md](USAGE.md#配置来源)。

## 8. 扩展点

### 加一个工具

1. 在 `tools/` 写 `build_xxx_tools(settings, sandbox)`，返回 `BaseTool` 列表
2. 返回值用 `artifacts.pack(text, artifact)` —— 文本给模型，artifact 给事件层与审计层
3. 在 `tools/registry.py::build_tools` 注册
4. 在 `graph/nodes/approve.py::tool_level` **登记风险等级**
   （未登记的按最危险的 L3 处理，这是故意的 fail closed）

路径参数记得走 `ensure_inside`；命令参数逐项 `shlex.quote`。

### 加一个节点

1. 在 `graph/nodes/` 写节点函数：签名 `(state, config) -> dict`，返回**状态增量**
2. 在 `graph/build.py` 接线
3. 在 `graph/routing.py` 加条件边
4. 若产生用户可见的东西，在 `runtime._stream` 里把对应的 `updates` 翻译成事件

节点里不要 yield 事件、不要写审计 —— 那些是 runtime 的职责。

### 加一个前端

1. 只依赖 `runtime.AgentRuntime` 与 `events`
2. 消费 `AsyncIterator[Event]`，不认识 LangGraph
3. **不要导入 `tools/` 或 `sandbox/`**（架构测试会拦）

参考 `tui/app.py` 的事件映射表，或 `web/app.py` 里 `model_dump_json()` 直接当 SSE 帧的写法。
