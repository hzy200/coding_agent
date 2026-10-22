<!-- owner: 项目作者 · last_reviewed: 2026-10-22 -->

# agents/ · 子智能体与委派策略

子智能体的价值是**上下文隔离**：独立 messages、只读工具、结构化返回。
它不是“换一段提示词”，也不该变成第二个主 agent。

## 职责与入口

| 文件 | 入口 | 作用 |
|---|---|---|
| `explore.py` | `make_readonly_runner` / `make_explore_runner` / `make_review_runner` / `render_subagent_result` | 只读探索与审查循环，返回 `ExploreReport` / `SubAgentArtifact` |
| `policy.py` | `profile_workspace` / `delegation_policy` | 按仓库规模与步骤类型决定能否委派、最多几次 |

## 禁区

- 子智能体**只绑定 `EXPLORE_TOOL_NAMES`**（`file_read` / `search_code` / `find_files`）；
  构造时与运行时各校验一次，绝不接受白名单外的工具名。
- 工具输出**不进入主 `messages`**，只回灌压缩摘要；不要把 transcript 顺手写回主历史。
- 委派预算由 `delegation_policy` 决定（小仓库 0 次，大仓库按规模 2/4/6）；
  只有 `kind=explore` 的步骤允许委派，实现/验证步自己完成。
- 单个子智能体失败只影响自己，不能阻断主 agent；并发度受配置上限约束。
- 同一条 `act` 消息里不要同时委派探索和写操作——探索读取的是改动前状态。
- 不可信仓库（`trusted=False`）不注入仓库内的 `AGENTS.md` / `CLAUDE.md` / memory，
  这是提示注入防线，不要在调用侧绕过。

## 依赖与测试

- 允许依赖 `tools` / `sandbox` / `llm` / `config` / `messages`；
  不要 import `graph` / `orchestrator`。
- 回归：`pytest -m "not llm" tests/unit/test_explore_agent.py tests/unit/test_delegation_policy.py tests/unit/test_delegate_tool.py tests/unit/test_runtime_subagent.py`
- 角色与预算的声明见 `docs/agents/subagents.yaml`；威胁模型见 `docs/THREAT_MODEL.md`。
