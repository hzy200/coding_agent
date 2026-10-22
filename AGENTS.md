<!-- owner: 项目作者 · last_reviewed: 2026-10-22 · review_cadence: 命令或边界变动时 -->
<!-- 用 HTML 注释而不是 front-matter：本文件会被注入 LLM 上下文，元数据要尽量不占 token -->

# AGENTS.md

面向 AI 的操作手册。**只写执行规则，不写叙事**；架构故事在 `docs/`。
本文件会被 `instructions.py` 自动发现并注入系统提示（最多 4 层、12 个文件、总量 24k 字符），
所以务必保持精简——每多写一行，都在挤占真正的任务上下文。

术语有歧义时以 [`docs/glossary.md`](docs/glossary.md) 为准。
契约类事实（事件/配置/工具等级/审计）以 [`docs/contracts/`](docs/contracts/index.md) 为准，
那里是代码生成的，不要相信任何手抄的副本。

---

## 1. 确切命令

先激活环境，否则 `agent` / `pytest` 都不在 PATH 上：

```bash
conda activate agent                 # Python 3.11；不要用 uv run / uvx 建临时 .venv
```

| 做什么 | 命令 |
|---|---|
| lint | `ruff check src tests` |
| 快反馈测试（单元+TUI+Web，无 WSL/无 Key） | `pytest -m "not wsl and not llm"` |
| 全量测试（**含真实 WSL**） | `pytest -m "not llm"` |
| 只跑沙箱层 | `pytest -m wsl` |
| 覆盖率（与 CI 同口径） | `pytest -m "not wsl and not llm" --cov --cov-report=term-missing` |
| 提交前一把梭 | `make check`（= lint + 快反馈） |
| 环境自检（不需要 API Key） | `agent doctor` |
| 重生成 L0 契约 | `python scripts/gen_contracts.py` |
| 文档一致性检查 | `python scripts/check_docs.py` |

三条硬规矩：

1. **永远带 `-m`**。裸 `pytest` 会把 3 条 `llm` 标记用例也跑掉，
   真的调用模型、真的计费。它们本意是"需要时手动跑"。
2. **改了 `events.py` / `config.py` / `approve.py` / `audit/models.py`
   必须重跑 `gen_contracts.py`**，否则 CI 的契约漂移检查会红。
3. 集成测试慢是 `wsl.exe` 进程启动开销，`pyproject.toml` 的 `addopts`
   已默认 `-n auto`，不要改成串行。

## 2. 目录结构与模块边界

```
src/coding_agent/
  runtime.py        唯一编排入口：图 + 沙箱 + 策略 + 审计 → 领域事件
  events.py         前端契约（领域事件）★ 改它要重生成契约
  config.py         配置（构造函数参数 > .env > 环境变量 > 默认值）★
  graph/            主图：state / routing（纯函数）/ build / nodes/
  orchestrator/     独立 harness 子图：survey → plan → scheduler ⇄ steering → fan_in
  sandbox/          执行安全层：wsl_exec / policy / pathguard / fs / snapshots / limits
  tools/            能力层：shell / files / git / deps / search / symbols / testrun / delegate
  agents/           explore / review 子智能体循环
  llm/              DeepSeek 客户端 · 提示词 · 上下文裁剪 · provider 路由
  memory/           checkpoint · 会话索引 · 长期记忆
  audit/            JSONL 审计 ★
  cli/ tui/ web/    三种前端
```

依赖方向（单向，无环，`tests/unit/test_architecture.py` 用 AST 守着）：

```
cli/tui/web → runtime → graph → orchestrator → agents/tools → sandbox
```

**前端（cli/tui/web）不得 import `tools/` 或 `sandbox/`**——这是安全属性，
不是代码风格。它保证任何界面都绕不过命令分级审批。

按子目录分层注入的模块级指令：`src/coding_agent/{graph,orchestrator,sandbox,tools}/AGENTS.md`。

## 3. 禁区清单

**改下面任何一处之前，先说清为什么，并确认对应测试还在。** 这些是承重墙。

| 禁区 | 为什么 |
|---|---|
| `sandbox/policy.py` 的命令分级表与 `DANGER_PATTERNS` | 改它就是改安全边界。新增"看起来无害"的白名单词会直接放大攻击面 |
| `graph/nodes/approve.py::tool_level` | **未登记的新工具按 L3 处理是故意的 fail-closed**。不要为了"跑通"给未登记工具加默认值 |
| `graph/build.py::estimate_recursion_limit` | 公式改过三次才对；加节点必须同步公式，否则正常任务被中断 |
| `review_watermark` 的归零者只能是 `planner` | 不变量 19。改了会让每一步都重审前面所有步骤的改动 |
| `approvals` 用后即清 | 不变量 2。**这是安全属性不是清理**——不清空会导致下一轮复用上一轮的批准 |
| 自动 `verify` 的 `allow_manifest=False` | 放开会让"写一个恶意 Makefile"变成绕过审批的任意代码执行路径 |
| 前端 import `tools/` / `sandbox/` | 不变量 5，有 AST 测试 |
| `.agent/` 纳入版本控制 | 含审计、快照、checkpoint、记忆。`git_add` 拒绝暂存、`git_commit` 提交前检查，不要绕过 |
| 评测判定命令改成 `pytest` | 判定固定用 stdlib `unittest`。历史上沙箱无 pytest 时导致所有任务判失败，出现"看起来干净、实际没测"的假象 |
| `tests/eval` 的隐藏测试文件写进工作区 | 判定前由纯净副本覆盖回去，这是"判定不可被篡改"的来源 |

已知易踩的坑（都是真出过事的）：

- **不要"优化"掉 fail-closed 分支**。审批记录缺失、答复无法解析、`call_id` 空/重复、
  非交互环境——一律拒绝，不是"可恢复的路径"。
- **不要给 `plan`/`step_idx` 等状态加"顺手的"默认值**，它们的生命周期
  （每步 vs 每轮）在 `WORKFLOW.md §2.2` 有明确表格，谁来归零都写清楚了。
- **不要改写 `tests/eval` 里的种子**让它"看起来能过"。种子必须失败，参考解必须通过。

## 4. 分支与提交

- 默认分支 `main`；直接在 `main` 上小步提交，或按需开短生命分支。
- 提交信息用 **Conventional Commits**：`feat:` / `fix:` / `docs:` / `chore:`，
  可选 scope，如 `fix(ci):` `feat(harness):` `chore(eval):`。
- **文档与代码同 PR**。改了行为却没改文档，CI 的契约检查与断链检查会拦。
- 不要提交 `.env`（密钥）、`.agent/`（审计/checkpoint/快照/artifact/记忆）、`.venv/`。

## 5. 工具使用

- **需要用到的**：`file_read` 读文件、`search_code`/`find_files` 检索、
  `symbol_search` 定位符号、`run_tests` 跑测试。它们返回结构化结果，
  比 `shell_exec` 拼 `grep` 可靠（且 `xargs` 之类不在 L0 白名单，会被降级为 L2）。
- **不要用的**：用 `shell_exec` 改文件（`sed -i` 等）。文件修改走 `file_write`/`file_edit`
  ——精确替换 + diff + 写前备份，可回滚。经 shell 的修改**不产生快照、不发 `FileChanged`**。
- L1 写工具需要 `--write`；L2/L3 会挂起等审批；**`-y` 只跳过 L2，L3 始终要人确认**。
- 写文件前先读。模型读过的文件若在写前被外部修改，`file_write`/`file_edit` 会拒绝覆盖。

## 6. 敏感数据

- 密钥只放 `.env`（已在 `.gitignore`）。**不要把真实 key 写进文档、测试或提交**。
- 审计、shell 输出、检索结果、文件内容在回灌模型前都会脱敏（`sk-*`、token、JWT、
  私钥块、`password=`、数据库连接串）。这是**模式匹配的 best-effort，不是完整 DLP**——
  不要因为"有脱敏"就去读凭据文件。
- 默认拒绝读取 `.env`、私钥/证书、`.npmrc`、`.netrc`、`.aws/**`、`.kube/**`、
  `credentials*.json`、`*.tfstate`。需要追加用 `AGENT_READ_DENY_GLOBS` /
  `AGENT_WRITE_DENY_GLOBS`，不要改默认列表本身。
- 每次 WSL 命令以 `env -i` 启动，宿主环境变量不会进入 Linux 子进程；
  `AGENT_SHELL_ENV_ALLOWLIST` 放行时，含 KEY/TOKEN/SECRET/PASSWORD 的名称始终忽略。
- LangSmith 追踪**只放标识与开关**，不放提示词或文件内容——追踪数据会离开本机。

## 7. 变更批准

- 安全相关改动（`sandbox/`、`approve.py`、审批语义、路径守卫）：对照
  `docs/ARCHITECTURE.md` 第 6 节不变量表与 `docs/THREAT_MODEL.md` 第 4 节防线分级逐条确认。
- 每个不变量都应有测试守着；动之前先看测试名字。
- 不确定时**先问**，不要猜。这个项目的默认值大多是"故意保守"，
  看起来像 bug 的地方，先查 ADR。

## 8. 文档地图

不知道该读什么，按这个顺序：

| 想… | 读 |
|---|---|
| 上手跑起来 | `README.md` → `docs/onboarding.md` |
| 查配置项 | `docs/contracts/config.md`（全量，生成） |
| 查工具/命令等级 | `docs/contracts/tools.yaml`（生成） |
| 查某个术语什么意思 | `docs/glossary.md` |
| 想知道"为什么这么做" | `docs/adr/`（决策记录，只增不改） |
| 想新增跨模块能力或改安全边界 | `docs/rfc/`（先写 RFC，再落 ADR、契约与验收锚点） |
| 想知道"哪里不许碰" | 本文件第 3 节 + `docs/THREAT_MODEL.md` |
| 改某个子系统 | 该目录下的 `AGENTS.md`，再看 `docs/ARCHITECTURE.md` |
| 出问题了 | `docs/runbook.yaml` |
| 验收某条需求 | `docs/acceptance/acceptance-criteria.yaml` |

**不要**把 README 的配置表当事实源——README 未纳入本次分层改造，其中的
配置摘录与测试数字可能已过期，以 `docs/contracts/` 与实测为准。
