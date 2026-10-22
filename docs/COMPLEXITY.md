---
layer: L3
doc_id: complexity
title: 项目复杂度分析
owner: 项目作者
status: active
last_reviewed: 2026-10-22
review_cadence: 每轮大改后重跑脚本
note: |
  数字由 `scripts/measure_complexity.py` 从源码生成，不要再手抄。
  重跑：`python scripts/measure_complexity.py`；引用前先确认日期。
---

# 项目复杂度分析

> 面向维护：用静态度量给出规模、复杂度分布与热点，并区分**本质复杂度**与**偶然复杂度**。
> 度量口径：Python `ast` 静态分析；圈复杂度为 McCabe 近似（判定点 + 布尔项）。
> 数据为 2026-10-22 实测（`scripts/measure_complexity.py`）。

## 1. 规模总览

| 项 | 值 |
| --- | --- |
| 源文件 | **108** |
| 源码行数 | **28,893** |
| 函数/方法 | **927** |
| 测试文件 / 行数 | **125 / 35,148** |
| 测试:源码 | **1.22 : 1** |
| 测试用例 | **1,765**（`pytest -m "not llm"`，2026-10-22 实跑） |
| 覆盖率 | **86.6%**（同一次全量实跑） |

测试体量仍是源码的 1.22 倍；覆盖率说明大多数分支有验证。测试用例数包含
TUI/Web 无头测试与真实 WSL 用例。

## 2. 复杂度分布（927 个函数）

| 指标 | 中位 | 均值 | 超阈值 |
| --- | --- | --- | --- |
| 函数长度 | **12 行** | 28.7 | >40 行：**14.2%** |
| 圈复杂度 | **3** | 5.2 | >10：**9.6%**，最大 **163** |

**结论：复杂度依然高度集中。** 约 90% 的函数圈复杂度 ≤10；复杂度主要落在
事件翻译、编排调度与工具执行几处“天然分支多”的位置。函数长度均值较上轮上升
（28.7 行），主要来自 `runtime._stream` 与 scheduler 等少数长函数，而非整体漂移。

## 3. 复杂度热点（当前）

| 函数 | 长度 | CC | 类型 | 评价 |
| --- | --- | --- | --- | --- |
| `runtime.py::_stream` | 814 | **163** | **真实逻辑 + 事件分发** | 唯一的“重型”函数；继续增长会难以维护 |
| `orchestrator/scheduler.py::scheduler` | 625 | 90 | **编排调度** | 并发、重试、合并、取消、实时事件集中在这里 |
| `graph/nodes/tools.py::run_tools` | 442 | 80 | 工具执行 | 并发只读工具 + 审批 + 结果回填 |
| `tui/app.py::_apply` | 239 | 77 | if/elif 分发 | 事件→渲染映射，可表驱动化 |
| `cli/app.py::render` | 185 | 57 | 分发 | 与 TUI 同构，可共享表驱动 |
| `orchestrator/graph.py::validate_tasks` | 129 | 57 | 真实逻辑 | 任务图校验，规则固有 |
| `orchestrator/fanin.py::synthesize` | 217 | 52 | fan-in 汇总 | 结构化结果合并 + worker 上下文 |
| `llm/ratelimit.py::_sync_from_shared_locked` | 140 | 48 | 共享状态合并 | 多进程限流快照的校验与合并 |
| `orchestrator/plan.py::plan_tasks` | 212 | 42 | 任务图生成 | 规则拆解 + 校验/降级 |
| `runtime.py::_worker_events` | 123 | 40 | 事件翻译 | worker 事件 → 主图事件 |
| `agents/explore.py::run` | 277 | 39 | 子智能体循环 | 只读循环 + 报告 |

**两类高 CC 要区分**：

- **分发/装配型**（`_apply`/`render`/`_worker_events`）：分支彼此独立，属
  **偶然复杂度**，可表驱动化。
- **逻辑型**（`_stream`、scheduler、`run_tools`、`validate_tasks`、fan-in）：
  并发/重试/合并/事件规则本身复杂，属**本质复杂度**；应拆分而不是压平。

## 4. 最大文件

| 文件 | 行 | 函数 | 备注 |
| --- | --- | --- | --- |
| `runtime.py` | 2,474 | 77 | 编排中枢 + 事件翻译 + worker 协调，复杂度天然集中 |
| `cli/app.py` | 1,334 | 36 | 命令 + 渲染 |
| `sandbox/wsl_exec.py` | 1,131 | 53 | WSL 执行、并发槽位与环境探测 |
| `tools/files.py` | 1,107 | 19 | 文件工具 |
| `tui/app.py` | 1,011 | 46 | Textual 界面 + 事件映射 |
| `orchestrator/scheduler.py` | 743 | 20 | 事件驱动 DAG、重试、合并与预算 |
| `orchestrator/implement.py` | 733 | 18 | 写 worker |
| `config.py` | 689 | 13 | 配置模型与校验 |

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
| 规模控制 | **需关注**：`runtime.py` 已到 2,474 行、`_stream` 814 行，是事实上的上帝对象 |
| 函数粒度 | **好**：中位 12 行 / CC 3；但 >40 行占 14.2% |
| 复杂度集中度 | **可接受**：9.6% 函数 CC>10，位置可解释 |
| 真·复杂点 | `_stream`、scheduler、`run_tools`、fan-in |
| 耦合 | **好**：单向、分层清晰、前端无旁路 |
| 可测性 | **好**：1.22:1 测试比 + 86.6% 覆盖（1,765 个用例） |

## 7. 可选降复杂度动作（按性价比）

| # | 动作 | 收益 | 风险 |
| --- | --- | --- | --- |
| 1 | `runtime._stream` 的 per-node 分支拆成 `_emit_*` 或 `{node: handler}` | **最高**：把最大函数降到若干小函数；已有事件流测试兜底 | 中（编排核心，须行为不变） |
| 2 | `orchestrator/scheduler.py::scheduler` 拆成并发派发 / 重试 / 合并三块 | 高：编排逻辑可单测、可组合 | 中 |
| 3 | `cli.render` / `tui._apply` 改表驱动 | 高：消掉最长 if/elif；两个前端可共享形状 | 低 |
| 4 | `graph/nodes/tools.py::run_tools` 拆成审批 / 并发执行 / 结果回填 | 中：工具执行路径更容易审查 | 中 |

前两项**不影响外部契约**，现有 1,765 个用例足以保证行为不变。

## 8. 相关文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 分层、不变量、扩展点
- [WORKFLOW.md](WORKFLOW.md) —— 工作流设计与评价
- [`scripts/measure_complexity.py`](../scripts/measure_complexity.py) —— 本文数字的事实源，改代码后重跑
