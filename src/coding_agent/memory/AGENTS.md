<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# memory/ · 记忆与会话状态

三类状态严格分开：**长期记忆**是项目事实，**checkpoint** 是会话状态，
**快照**是文件内容。混用会造成“回滚了文件却回滚不了对话”这类事故。

## 职责与入口

| 文件 | 入口 | 作用 |
|---|---|---|
| `longterm.py` | `LongTermMemory` / `resolve_memory_root` / `normalize_fact` | 工作区内的 `.agent/memory.md`，显式增删与冲突替换 |
| `checkpointer.py` | `CheckpointStore` | MemorySaver / AsyncSqliteSaver 生命周期 |
| `sessions.py` | `SessionIndex` | 从审计日志归纳历史会话，不另建会话表 |

## 禁区

- **不做 LLM 自动提炼**（ADR-0008）：记忆只显式增删，原型阶段宁可少而准。
  自动提炼会积累噪声，且难以审计“这条结论哪来的”。
- 写入前必须做 secret 扫描；冲突替换要保留来源与时间（`memory.meta.json`）。
- `MAX_FACTS=50` / `MAX_FACT_CHARS=300` 是上下文预算的一部分；
  达到上限要显式报错，不要静默丢弃旧事实。
- 不可信仓库 / Web 租户必须把记忆根移出仓库（`resolve_memory_root`），
  否则仓库内文件可以伪造记忆并注入模型。
- checkpoint 必须用 `AsyncSqliteSaver`；同步版在异步编排下会直接失败。
- 会话索引从审计日志归纳；审计关闭时列不出会话是可接受降级，不要为此另建状态表。

## 依赖与测试

- 允许依赖 `sandbox.fs` / `pathguard` / `snapshots`、`security.secrets`、`audit`；
  不要 import `graph` / `orchestrator` / `tools`。
- 回归：`pytest -m "not llm" tests/unit/test_memory.py tests/unit/test_checkpointer.py tests/unit/test_sessions.py`
- 真实文件链路：`pytest -m "not llm" tests/integration/test_longterm_memory.py`
