# ADR-0004：审批决定与执行分离，执行集中在唯一咽喉

- **Status**：accepted · 事后补录
- **Date**：2026-10-22
- **Owner**：项目作者
- **Tags**：安全 · 架构

## Context

审批逻辑如果和执行写在一起，会有两个问题：

1. `interrupt()` 恢复时节点会**从头重跑**，如果节点里有副作用，同一动作可能被做两次；
2. 前端（CLI/TUI/Web）如果各自实现审批，就能各自绕过——安全性取决于最弱的那个前端。

## Decision

- **`approval_gate` 只做决定**（放行 / 询问 / 拒绝），保持**纯函数**：
  同样的 state 必须得到同样的结论，除了 `interrupt()` 之外无副作用。
- **`tools` 节点负责强制**：它是所有工具执行的**唯一咽喉**，
  没拿到 `auto`/`approved` 一律不执行。
- **fail closed**：审批记录缺失、答复无法解析、`call_id` 空/重复、非交互环境，一律拒绝。
- 审批结果**一次性有效**，`approvals` 用后即清。

## Consequences

### 好的

- 前端绕不过：三种前端共用同一套语义，都无法自行放行；
- 挂起/恢复安全：纯函数保证重跑得到同样的结论；
- 审批配对表（`_pending_calls`）放在 runtime 实例级、按 `thread_id` 分桶，
  挂起与恢复是两次 `_stream`，只存局部变量会丢参数。

### 坏的 / 代价

- 每个新工具都要在 `approve.py::tool_level` 登记等级，漏了就按 L3——有意的摩擦；
- `approvals` 用后即清容易被误当成"清理逻辑"而优化掉。
  **它是安全属性**：不清空会导致下一轮复用上一轮的批准。

### 中性 / 需要留意

- 并发区（orchestrator 的 implement worker）**不弹审批、不调用 `interrupt()`**；
  审批仍属于主图或编排边界，否则会并发弹多个交互。

## 复核

- **Verified by**：`pytest -m "not llm" tests/unit/test_approve.py tests/unit/test_nodes.py`
- **Revisit when**：审批语义、审批记录或非交互场景变化时

若未来需要"策略引擎外置"（如按租户配置审批规则），
要加的是规则来源，而不是把决定权下放给执行点。
