# 项目复杂度分析

> 面向维护：用静态度量给出规模、复杂度分布与热点，并区分**本质复杂度**与**偶然复杂度**。
> 度量口径：Python `ast` 静态分析；圈复杂度为 McCabe 近似（判定点 + 布尔项）；
> 数据为 2026-10-18（Phase 4D 后）实测。

## 1. 规模总览

| 项 | 值 |
| --- | --- |
| 源文件 | **70** |
| 源码行数 | **15,252** |
| 函数/方法 | **480** |
| 测试文件 / 行数 | **83 / 24,870** |
| 测试:源码 | **1.63 : 1** |
| 测试用例 | **1,420**（不含 LLM） |
| 覆盖率 | **89.1%** |

测试体量已是源码的 1.6 倍；89.1% 覆盖率说明大多数分支有验证。测试用例数包含
TUI/Web 无头测试与 207 个真实 WSL 用例。AST 行为改造前基线；新增
`orchestrator/scheduler.py`、`artifacts.py`、`merge.py` 后应重跑度量脚本。

## 2. 复杂度分布（480 个函数）

| 指标 | 中位 | 均值 | 超阈值 |
| --- | --- | --- | --- |
| 函数长度 | **11 行** | 18.9 | >40 行：约 **10%** |
| 圈复杂度 | **3** | 5.1 | >10：约 **10%**，最大 **114** |

**结论：复杂度依然高度集中。** 约 90% 的函数圈复杂度 ≤10；复杂度主要落在
事件翻译、编排调度与工具装配几处“天然分支多”的位置。

## 3. 复杂度热点（当前）

| 函数 | 长度 | CC | 类型 | 评价 |
| --- | --- | --- | --- | --- |
| `runtime.py::_stream` | 623 | **114** | **真实逻辑 + 事件分发** | 唯一的“重型”函数；继续增长会难以维护 |
| `tui/app.py::_apply` | 222 | 70 | if/elif 分发 | 事件→渲染映射，可表驱动化 |
| `tools/files.py::build_file_tools` | 368 | 63 | 装配 | 多个闭包工具，线性 |
| `orchestrator/graph.py::validate_tasks` | 111 | 51 | 真实逻辑 | 任务图校验，规则固有 |
| `cli/app.py::render` / `_make_renderer` | 167 / 176 | 50 / 50 | 分发 | 与 TUI 同构，可共享表驱动 |
| `orchestrator/nodes.py::make_dispatch_node` / `dispatch` | 259 / 249 | 48 / 48 | **编排调度** | 并发、重试、合并、取消、实时事件集中在这里 |
| `orchestrator/implement.py::make_implement_runner` / `run` | 274 / 261 | 45 / 45 | **写 worker** | worktree、工具循环、验证、补丁 |
| `agents/explore.py::make_readonly_runner` / `run` | 228 / 191 | 39 / 35 | 子智能体循环 | 只读循环 + 报告 |

**两类高 CC 要区分**：

- **分发/装配型**（`_apply`/`render`/`build_*`）：分支彼此独立，属**偶然复杂度**，
  可表驱动化。
- **逻辑型**（`_stream`、`dispatch`、`validate_tasks`、implement runner）：
  并发/重试/合并/事件规则本身复杂，属**本质复杂度**；应拆分而不是压平。

## 4. 最大文件

| 文件 | 行 | 函数 | 备注 |
| --- | --- | --- | --- |
| `runtime.py` | 1,661 | 55 | 编排中枢 + 事件翻译，复杂度天然集中 |
| `tui/app.py` | 937 | 42 | Textual 界面 + 事件映射 |
| `orchestrator/nodes.py` | 901 | 22 | survey / plan / dispatch / extend / synthesize |
| `cli/app.py` | 865 | 26 | 命令 + 渲染 |
| `tools/testrun.py` | 548 | 17 | 测试命令识别与输出解析 |
| `tools/files.py` | 491 | 9 | 文件工具 |
| `sandbox/policy.py` | 489 | 14 | 安全规则（含大段模式常量） |
| `orchestrator/implement.py` | 461 | 13 | 写 worker |

## 5. 耦合与依赖方向

```
runtime -> audit config diffing events graph memory messages sandbox tools
           orchestrator steering                         (中枢，合理)
graph   -> config llm messages sandbox steering tools orchestrator.graph
orchestrator -> agents config graph.state llm sandbox steering tools
agents  -> config llm messages sandbox tools
tools   -> config diffing sandbox
sandbox -> config diffing
cli     -> ... runtime sandbox tui web   (doctor/sandbox-init 的既定例外)
tui/web -> audit config events runtime
```

**关键结论**：

- 分层约束成立：`tui`/`web` **没有**指向 `tools`/`sandbox` 的边。
- `runtime` 扇出广是编排层本职；orchestrator 只依赖 graph.state 与既有能力层。
- 无环、方向单一；没有双向或循环依赖。

## 6. 总体评价

| 维度 | 评价 |
| --- | --- |
| 规模控制 | **可接受**：最大文件 1,661 行（中枢），无“上帝对象” |
| 函数粒度 | **好**：中位 11 行 / CC 3 |
| 复杂度集中度 | **可接受**：约 10% 函数承担主要复杂度，位置可解释 |
| 真·复杂点 | `_stream`、scheduler、implement runner |
| 耦合 | **好**：单向、分层清晰、前端无旁路 |
| 可测性 | **好**：1.63:1 测试比 + 89.1% 覆盖 |

## 7. 可选降复杂度动作（按性价比）

| # | 动作 | 收益 | 风险 |
| --- | --- | --- | --- |
| 1 | `runtime._stream` 的 per-node 分支拆成 `_emit_*` 或 `{node: handler}` | **最高**：把最大函数降到若干小函数；已有事件流测试兜底 | 中（编排核心，须行为不变） |
| 2 | `orchestrator/nodes.py` 的 dispatch/plan 拆到独立模块（并发、合并、校验） | 高：编排逻辑可单测、可组合 | 中 |
| 3 | `cli.render` / `tui._apply` 改表驱动 | 高：消掉最长 if/elif；两个前端可共享形状 | 低 |
| 4 | implement runner 拆成 worktree / loop / patch 三块 | 中：写路径更容易审查 | 中 |

前两项**不影响外部契约**，现有 1,403 个用例足以保证行为不变。

## 8. 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 分层、不变量、扩展点
- [WORKFLOW.md](WORKFLOW.md) —— 工作流设计与评价
