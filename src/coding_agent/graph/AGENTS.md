<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# graph/ · 主图

`direct` 快速路径与编排路径的公共骨架。三个机制**正交**，靠纯函数条件边组合。

## 边界

- `routing/` 是**纯函数、无 I/O、无副作用**，可单独测（`tests/unit/test_routing.py`）。
  不要在里面加 I/O 或时间依赖——它要能逐分支断言。
- 节点**只读状态、返回状态增量**，不产事件、不写审计——那是 `runtime` 的职责。
  唯一例外：`scheduler` 用 custom stream 实时推送 worker 开始/结束。
- 节点不认识彼此。加一个机制不该改另外两个。

## 状态生命周期（最容易改错的地方）

字段分「每步」与「每轮」两类生命周期，**归零者写死在 `WORKFLOW.md §2.2`**：

| 字段 | 谁归零 |
|---|---|
| `tool_rounds` / `dirty` / `verification` / `retry` / `budget_exhausted` | planner / advance / repair |
| `replan_count` | 只由 planner 清零、replan 递增（**advance 不动它**） |
| `same_failure_count` / `last_failure_signature` | 只有 planner / advance 归零（repair/replan 不清，这是刻意的） |
| `approvals` | 用后即清（**安全属性**，不清会导致下一轮复用上一轮批准） |
| `run_status` / `recovery_notice` / `transient_retries` | runtime 每轮注入 |

## 禁区

- `build.py::estimate_recursion_limit` 的公式覆盖 repair/replan/nudge/steering/
  orchestrator/任务扩展。**加节点必须同步公式**，否则正常任务被中断。
- `approve.py::tool_level` 未登记工具按 L3——**故意 fail-closed**，别加默认值。
- `plan_kinds` 与 `plan` 必须逐项对齐。

## 加节点/加边的顺序

`nodes/` 写节点 → `build.py` 接线 → `routing.py` 加边 → `runtime._stream` 翻译事件
→ 同步 `estimate_recursion_limit` → 补 `tests/unit/test_graph_event_coverage.py`。
