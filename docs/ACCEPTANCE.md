---
layer: L3
doc_id: acceptance
title: 需求对照与自检
owner: 项目作者
status: active
last_reviewed: 2026-10-22
review_cadence: 每次发版前
note: |
  第 1 节是需求 → 落点 → 验证的对照表。
  机读的 Given-When-Then 版本见 docs/acceptance/acceptance-criteria.yaml。
  第 4 节的「待办」已抽到 docs/tech-debt.yaml。
---

# 需求对照与自检

逐条对照原始需求 → 实现落点 → 怎么验证。附**已知限制**（诚实列出，不藏）。

## 1. 原始需求逐条对照

| 原始需求 | 实现落点 | 怎么验证 |
|---|---|---|
| 基于 LangGraph 的终端原生编程智能体 | `graph/build.py` 的 `StateGraph`；`cli/` 为终端入口 | `agent tui` / `agent run` |
| 接入 DeepSeek V4 API | `llm/deepseek.py`（OpenAI 兼容协议，改 `base_url` 即可） | `agent chat` 流式对话 |
| WSL2 作为执行安全沙箱 | `sandbox/wsl_exec.py`；非 root 用户 | `agent doctor` 看沙箱用户与发行版 |
| **模型只输出意图**，宿主代执行 | `tools/` 全部是宿主实现的 `BaseTool`；模型拿不到任何句柄 | 任一 `agent run` 的工具行 |
| 宿主经子进程启动 bash 解释执行 | `WslSandbox._argv()` → `wsl.exe -d <distro> -- /bin/bash -l -s` | `pytest tests/integration/test_wsl_exec.py` |
| 文件修改走专用工具 | `tools/files.py`；不走 shell | `agent run --write` 的 `✎` 行 |
| 路径安全 | `sandbox/pathguard.py`（词法）+ `sandbox/fs.py`（realpath）双重校验 | `test_symlink_escape_is_rejected` |
| 修改精确 | `file_edit` 要求 `old_string` 唯一，0 次/多次都拒绝 | `test_edit_refuses_ambiguous_match` |
| 可回滚 | `sandbox/snapshots.py` + `agent undo` | `test_restore_is_itself_undoable` |
| 多轮会话 | LangGraph checkpoint（SQLite）+ `--thread-id` | `agent run --thread-id X "接着上次"` |
| 任务规划 | `graph/nodes/planner.py`（结构化输出 + 重试 + 降级） | `agent run` 输出里的「计划：」 |
| 仓库检索 | `tools/search.py`（rg 优先 / grep 兜底） | `test_search_tools.py` |
| Shell 执行 | `tools/shell.py` | `test_shell_tool.py` |
| 编译测试 | `tools/testrun.py` + `graph/nodes/verify.py` | `test_verify.py` |
| Git 操作 | `tools/git.py`（status/diff/log/add/commit） | `test_git_tools.py` |
| 依赖管理 | `tools/deps.py`（uv/poetry/pnpm/yarn/npm/pip 自动识别） | `test_deps_tools.py` |
| 失败修复 | `graph/nodes/repair.py` + `routing.make_route_after_verify` | `test_repair.py` |
| diff 生成 | `diffing.py`；CLI/TUI 着色渲染 | `agent diff` |
| 命令分级审批 | `sandbox/policy.py` + `graph/nodes/approve.py` + `interrupt()` | 场景 2 演示 |
| 审计日志 | `audit/`；由 runtime 写，任何前端绕不过 | `agent audit` |
| checkpoint 回滚 | checkpoint 负责**会话状态**恢复；快照负责**文件内容**回滚 | `test_checkpointer.py` + `agent undo` |
| 会话分叉/回退 | `AgentRuntime.fork/rewind`；历史不删除，回退即新建分支 | `agent fork` / `agent rewind`；`test_runtime_checkpoints.py` |
| 上下文治理 | 字符 + 估算 token 双预算、400 降档重试、确定性摘要、`ContextTrimmed` 指标 | `tests/eval/context_memory_bench.py` |
| 长期记忆治理 | top-K 相关注入、`memory_search`/`memory_save`、冲突替换、过期、来源、写入锁与密钥扫描 | `test_memory.py` + `context_memory_bench.py` |
| 分层项目指令 | 发现并合并 `AGENTS.md` / `CLAUDE.md`（根目录到 4 层子目录） | `agent run` 系统提示；`test_instructions.py` |
| 运行中实时干预 | `steering.py` + `graph/nodes/steering.py` + TUI 斜杠命令 | TUI `/pause` `/resume` `/cancel` `/add` `/replan`；`test_steering.py` |
| 动态任务图 + 并发 worker | `orchestrator/`（survey/plan/scheduler/fan-in） | `test_orchestrator.py`；[tests/eval/README](../tests/eval/README.md) |
| 流式输出 | 事件流 → CLI/TUI 逐字渲染 / Web SSE | `agent chat`；`/api/run` |
| Web/UI 最小验证 | `web/`（SSE 对话 + 多会话 + 联通测试） | `agent web` |
| LangSmith 追踪 | `runtime._trace_metadata` + `config.apply_tracing_env` | `agent doctor` + LangSmith 项目 |
| 冒烟测试 / 场景演示 | `scripts/demo.py` 四场景 | `python scripts/demo.py` |

### 一处刻意的偏离

原始描述写的是「启动 `/bin/bash -lc` 解释执行」。实现改成了 **`/bin/bash -l -s`，脚本经 stdin 传入**。

原因：Windows → `wsl.exe` → Linux 之间有一层命令行转义。把命令放进 argv 时，
引号、换行、`$` 会被各层重新解释；经 stdin 传则完全绕开这一层。
行为等价（同一份脚本、同一套 shell 语义），但不会因为转义问题出岔子。

## 2. 四个创新点

| 创新点 | 代码 | 测试 | 演示 |
|---|---|---|---|
| Shell 与文件工具分离的双工具架构 | `tools/shell.py` + `tools/files.py`；文件不走 shell | `test_file_tools.py`、`test_shell_tool.py` | 场景 1 |
| 可审计的命令安全策略 | `sandbox/policy.py`（四级 + 复合命令取最高）+ `audit/` | `test_policy.py`、`test_approve.py` | 场景 2 |
| 失败驱动的命令修复循环 | `verify` → `repair` → `act`，硬性上限 3 次 | `test_repair.py`（图级收敛与上限） | 场景 3 |
| 可回滚的修改流程 | `snapshots.py`；**回滚本身也可回滚** | `test_restore_is_itself_undoable` | 场景 4 |

**两道质量关**（`verify` 管行为对不对，`review` 管代码干不干净）：

| 关 | 代码 | 检查什么 |
|---|---|---|
| `verify` | `tools/testrun.py`、`nodes/verify.py` | 跑项目自己的测试/构建，把失败结构化 |
| `review` | `tools/review.py`、`nodes/review.py` | 语法坏没坏、测试有没有被改弱、调试残留（`breakpoint()`/`pdb`）、有没有写进 `.agent/`；Markdown 代码块闭合、相对链接与标题层级；探测到 ruff/mypy/gofmt/rustfmt 时一并跑，**改动文件**的 lint/格式问题默认阻断（mypy 因空配置噪声只作告警） |

review 的「改动前」**直接复用快照**（不变量 8：所有写操作先留底），不另建基线；
只审水位线之后**新增**的行，水位线跨步骤保留 —— 否则每一步都会重审前面所有步骤
的改动。阻断项（结构性缺陷）与验证失败**共用**同一份修复预算，走同一条
`repair` 出口；其余告警只记录，不阻断。`AGENT_REVIEW_LINT_BLOCKING=false`
可把 lint/格式问题降回告警。

## 3. 自检清单

代码冻结前跑一遍，全部应当通过。

```bash
# 1. 静态检查
ruff check .

# 2. 全量测试（含真实 WSL 沙箱）
pytest -m "not llm"          # 期望 1561 passed
# 另有 3 条 llm 标记用例需要 DEEPSEEK_API_KEY；不要用裸 pytest 代替 ——
# 不加 -m 时它们会真的调用模型（本机配了 key 就产生费用）。

# 3. 环境自检
agent doctor                 # 期望「全部通过」

# 4. 端到端冒烟（无需 API Key 的部分）
agent sandbox-init
pytest -m wsl -q             # 沙箱链路

# 5. 场景演示（需要 API Key，会真实调用模型）
python scripts/demo.py --list
python scripts/demo.py --only 1
```

当前状态（2026-10-18，Phase 4D 动态任务图落地后）：

| 项 | 结果 |
|---|---|
| 测试 | **1561 passed**（`pytest -m "not llm"`，含真实 WSL），其中 `wsl` 标记 **211**，无失败；另 3 条 `llm` 用例需 API Key |
| 覆盖率 | **87.5%**（`pytest -m "not llm" --cov`，含真实 WSL）；**CI 子集 80.7%**，门槛 `fail_under=76` |
| lint | 干净（`ruff check src tests`） |
| 快反馈 | `pytest -m "not wsl and not llm"` = **1350 passed**（deselect 214 = 211 个 `wsl` + 3 个 `llm`，两组无交集）；需装齐 `.[dev,ui,web]`，缺 `textual` 时 TUI 整目录 skip（TUI 49 条） |
| 能力评测 | **两个套件，各记各的基线**：`default` 29 个短任务最新两轮 **29/29、29/29（100%，spread 0，无翻转）** —— 该套件已**饱和**，测不出进一步改进；旧基线 12/29 = 41.4% 只能当历史坐标。`long` 原有 5 个长程任务（38 文件 / 近千行仓库，改动跨度 3–8 文件，通过率 **2/5 = 30%**，`spread` 20pp；新增 `parallel_modules` 后共 6 个，旧 5 任务基线只能当历史坐标）。报告带机制指标、worker 成本/并发/冲突与噪声块；对比与干预延迟见 [tests/eval/README](../tests/eval/README.md) |
| LangSmith | `coding-agent-full-v1` 共 **150** 条（139 capability + 11 mechanism）：**11/11 mechanism、6/6 smoke capability、139 capability 全量 138/139 = 99.3%**；`full` 套件 **104/104** 种子失败且参考解通过；量化报告见 [LANGSMITH_REPORT.md](LANGSMITH_REPORT.md)；真实 CLI direct 修复与 4-worker 并发写编排均通过 |
| CI | `.github/workflows/ci.yml`：`pip install -e ".[dev,ui,web]"` → ruff → `pytest -m "not wsl and not llm"` |

> **口径提醒**：单次运行的通过率**说明不了任何事**。同一份代码曾跑出 52% 与 83%
> （`spread` 31 个百分点、11/29 任务翻转）—— 上一版把这读作「已落进 30–70% 区间、
> 有区分度」是过度解读。版本对比至少要 `--repeat 2`，且差异必须大于 `spread`。
>
> **当前 default 套件已饱和**：两轮都是 100%，`spread=0`。这不是「智能体完美」，
> 而是这把尺子对当前模型没有区分度；继续用它比较任何改动都只会得到 0pp。
> 需要扩展更难的任务（或换 `long` 套件）才能量出 Phase 4 的并发/成本收益。

> **口径提醒**：全量一律写 `pytest -m "not llm"`。裸 `pytest` 会把那 3 条 `llm` 用例一起跑、
> 真的调用模型 —— 本机 `.env` 配了 key 就计费，而它们本意是"需要时手动跑"。
>
> **CI 与本地不是一回事**：CI 是 Linux runner、没有 WSL2，只跑快反馈那 1309 个用例，
> 覆盖率 80.7%。沙箱层与能力层的覆盖（`tools/files.py` 85%、`sandbox/snapshots.py` 88%）
> 全靠 `-m wsl` 的 211 个用例，它们**只在本地执行**。CI 会以 `::warning::` 显式提示这个缺口 ——
> 详见 [archive/README.md](archive/README.md)（原 `IMPROVEMENT_PLAN.md` 的 Q1 去向）；
> 缺陷与待办现集中在 [tech-debt.yaml](tech-debt.yaml)。
>
> 上面每个数字都是量出来的，命令见本节第 2–4 条；改动后请重跑核对，别手抄。

## 4. 已知限制

按“会不会影响答辩结论”排序。前四条是**有意的取舍**，其余是**待办**。

### 有意为之

**Web 支持审批、多用户与公网部署。**
`web/app.py::WEB_APPROVAL_MODE = "ask"`：L2/L3 通过 SSE 发
`approval_requested`，页面收集答复后调用 `/api/resume` 恢复；同时支持
pause/cancel/add/replan。认证使用 Bearer Token；每个用户有独立
workspace/audit/checkpoint/memory；非 loopback 必须 token + TLS（本进程
或可信反向代理）。默认仍只监听 `127.0.0.1`，未配置 token 时只允许 loopback
匿名。主交付界面仍是 TUI/CLI。

**长期记忆只支持显式增删，不做 LLM 自动提炼。**
自动记忆容易积累噪声，也难以回答"这条结论是哪来的"。
在一个主打可审计的系统里，这个交换不划算。模型可以在运行中调用
`memory_search` 检索、在 `--write` 会话调用 `memory_save` 写一条具体事实，
但宿主不做后台蒸馏；每一条记忆仍是一次明确的、可见的工具调用。

**内存上限默认关闭。**
`ulimit -v` 默认 0（不限）。JVM / Node / 编译器会索取远超实际使用的
虚拟地址空间，贸然开启会把正常构建打死。CPU 时间、文件大小、进程数都有限制。

**经 shell 的修改不产生快照。**
文件修改的正道是 `file_*` 工具（精确替换 + diff + 写前备份）。模型若用 shell
改文件（`sed -i` 等，属 L2、需确认），会置 `dirty` 触发验证，但**不生成快照、
不发 `FileChanged`** —— shell 输出无法结构化解析。「可回滚的修改流程」覆盖文件
工具路径与 orchestrator 的 patch 合并（合并前对目标文件留底），不覆盖模型经
shell 的修改。

### 待办

**思考模式的 `reasoning_content` 未修。**
`deepseek-v4-flash` 偶发 400。W12 查清了根因（langchain-openai 不提取该字段，
且序列化时不透传任意 `additional_kwargs`），但**捕获侧无法稳定复现**，
所以没有把无法验证的改动放进请求路径。规避方式是 `DEEPSEEK_MODEL=deepseek-chat`。
详见 [USAGE.md 故障排查](USAGE.md#模型报-reasoning_content-must-be-passed-back)。

**实时干预只在节点边界生效。**
pause/cancel 不会打断正在跑的 LLM 轮次或同步工具；direct 模式的 cancel 额外在
每轮 act/tools 之间检查，但长工具本身仍不可中断。入口包括 TUI 命令、Web 控制
按钮与 CLI `agent steer`；steering 队列与暂停标记原子持久化，重启后未消费的
add/replan 仍可恢复。

**检索默认后端是 grep。**
沙箱里没装 ripgrep，自动退到 `grep`/`find`。两个后端输出格式一致（grep 分支显式 `-E`），
但大仓库上 grep 会更慢。`agent doctor` 会显示当前用的是哪个。

**上下文裁剪用字符 + 估算 token 双预算，不引入 tokenizer；用量则按 provider 回报累计。**
`AGENT_CONTEXT_MAX_TOKENS` 默认 0（只按字符），打开后按「CJK 约 1 字/token、
其余约 4 字符/token」的保守估算再兜一层。遇到 context-length 400 时 act/respond
会用减半预算重裁并重试一次。每次运行的 token 用量仍取自模型的 `usage_metadata`，
累计写入 `run_end` 审计（provider 未回报时为 `None`）。
**成本预算与模型分层未实现**：worker/总 token 只计量与报告，运行中不会因超预算硬停。

**共享文件的并发写未实现。**
当前用 write_scope 不重叠 + 顺序 patch 合并规避；three-way rebase / 冲突自动
合并留给后续。写 worker 取消时未合并的 patch 丢弃，已合并改动保留。

### 未验证

**TUI 在真实交互终端下的视觉效果。**
自动化测试用 Textual 无头驱动（45 个用例），覆盖了事件映射、计划面板、
审批弹窗、斜杠命令，但**配色、边框、滚动行为需要人工看一眼**。

**沙箱隔离：默认到 WSL 发行版级，可选用 bwrap 做内核级隔离。**
默认（`AGENT_SHELL_SANDBOX=off`）是非 root 用户 + 工作区限定 + 资源上限 +
**命令参数的词法越界升级**（只读命令引用 `~`/工作区外绝对路径/`..`、或含 `$()`/变量替换时
升级到人工确认）。若要**内核级**隔离，设 `AGENT_SHELL_SANDBOX=bwrap`：命令在挂载命名空间里执行，
`/home` `/root` `/mnt`（Windows 盘）不可见、仅工作区可写；配置了 bwrap 但沙箱内不可用时会
**拒绝执行**（fail closed），不静默降级。默认威胁模型仍是"防误操作与静默越权"；
完整的资产/假设/边界与"从 T1 升到 T2 还差什么"见 [THREAT_MODEL.md](THREAT_MODEL.md)。

## 5. Harness 阶段验收（3A–4D，已实现）

> 完整方案见 [HARNESS.md](HARNESS.md)。5.1–5.3 是编排/写/干预，5.4–5.7 是
> 评测、实时事件、依赖语义与动态任务图。

- [x] `AGENT_ORCHESTRATOR_ENABLED=false` 时，事件、状态与行为与现有基线一致。
- [x] 开启后，能根据提示词与仓库画像在 `direct` / `parallel_readonly` 间选择工作流。
- [x] `parallel_readonly` 至少 2 个只读 worker 并发执行（事件与机制指标可见）。
- [x] worker 失败不影响同批其它 worker；依赖失败的任务标记 `blocked`。
- [x] `synthesize` 的 `worker_context` 进入主 agent 的 planner/act，但不写回主消息历史。
- [x] `WorkflowSelected` / `TaskGraphCreated` / `WorkerStarted` / `WorkerFinished` /
      `OrchestrationFinished` 事件与审计齐全。
- [x] `estimate_recursion_limit` 覆盖编排路径。
- [x] `ruff`、全量 unit、CI 子集、WSL 集成通过。

单任务烟雾：`long/discount_and_audit` 通过，工作流 `parallel_readonly`，
5 个任务 / 4 个并发 worker。单次运行不能代表稳定通过率，需 `--repeat 2` 量化。

### 5.1 Phase 3B（已实现）

- [x] 任务失败后按 `AGENT_ORCHESTRATOR_TASK_RETRIES` 重试；重试耗尽才标 `failed`。
- [x] 默认 `partial` 不满足依赖；依赖 failed/blocked/**partial** 的任务标 `blocked`。
      5.6 增加 `depends_mode=any`，允许 review/test 消费 partial 产出。
- [x] `review` worker 可被任务图使用，复用只读工具循环并返回审查发现。
- [x] fan-in 只取每个任务**最后一次**尝试，并报告多 worker 关注同一文件。
- [x] `WorkerFinished` / 审计 / eval 记录 `attempt` 与 `worker_retries`。
- [x] `ruff`、全量 unit、CI 子集、WSL 集成通过。

### 5.2 Phase 3C（已实现，默认关闭）

- [x] `implement` 任务必须开启写编排、会话 `--write`、干净的 git 工作区；
- [x] implement 必须给出 `write_scope`；不同 implement 的 scope 不重叠，
      且 implement 之间不能互相依赖；
- [x] implement worker 在独立 git worktree 中运行，工具只作用于该 worktree；
- [x] 补丁先在主工作区 `git apply --check`，通过后才 `git apply`；
      冲突时主工作区不被修改，任务标 `failed`；
- [x] worker 内部工具调用逐条写审计；eval 记录 `worker_tool_calls` / `worker_merged`；
- [x] 写模式任务图校验失败时退化为单个 implement 任务：优先顶层目录，扁平仓库
      退到顶层文件；两者都没有时显式报告降级，写路径不会静默消失；
- [x] 写范围互不重叠的两个 implement worker 可同批并发（`long/parallel_modules`：
      2 worker / 最大同批 2 / 2 个 patch 合并）；
- [x] worker 脏检查排除 `.agent/` 与 `__pycache__/`，不会把并发 worktree/patch
      误判成用户未提交改动（真实 WSL 回归测试）；
- [x] **survey 脏检查同样排除 `.agent/`/`__pycache__/`**，并要求工作区是
      git 仓库根；否则第二次运行会静默关闭写编排（P0 回归）；
- [x] **合并前为主工作区目标文件留快照**，`worker_results.snapshot_ids`
      可被 `agent undo` 使用（真实 WSL 集成测试）；
- [x] 超过 `orchestrator_max_tasks` 的任务图按校验失败整图降级，不再静默截断；
- [x] worktree/patch 文件名截断后附加哈希，避免长任务 id 共用路径；
- [x] `ruff`、全量 unit、CI 子集、WSL 集成通过（含真实 worktree/patch 集成测试）。

单任务烟雾：`long/discount_and_audit` 通过；workflow `orchestrated_implement`，
1 个 implement worker 完成 33 次工具调用，patch 成功合并，最终验证通过。

多写并发烟雾：`long/parallel_modules` 通过；2 个 implement worker 同批并发
（最大同批 2），各自 worktree 中完成独立文件，2 个 patch 顺序合并，主 agent
最终验证 7 passed。两个 worker 状态均为 `partial`（各自全量验证时对方功能尚未
合并），依赖它们的 test 任务按 Phase 3B 规则标 `blocked`。
单次运行不代表稳定通过率。

### 5.3 Phase 3D（已实现）

- [x] `SteeringController` 按 `thread_id` 隔离；runtime `steer()` 入队并写
      `kind=steering` 审计（含来源/动作）；
- [x] `/pause` 在四个节点边界消费，`interrupt()` + checkpoint 挂起；
      `/resume` 通过 `resume_steering()` 继续，恢复次数受 `AGENT_MAX_RESUMES` 限制；
- [x] `/cancel` 协作式取消：只读/写 worker 在轮次与工具执行前检查；
      implement worker 取消后不生成 patch；scheduler 合并前再检查一次；
- [x] 已合并的 implement 改动保留（明确策略），未合并 patch 丢弃；
- [x] `/add` 只追加只读任务（explore/test/review）；direct 模式追加计划步骤；
- [x] `/replan` 保留 success/partial 任务，只重建剩余任务；已合并写补丁后
      新任务只允许只读；
- [x] `SteeringApplied` / `WorkflowRevised` 事件；TUI 暂停状态、提示与
      暂停期间拒绝新任务；
- [x] `ruff`、CI 子集、全量 unit、WSL 集成、无头 TUI 通过。

### 5.4 Phase 4 评测与对比（首切片，已实现）

- [x] 评测补充 **worker token / total token / worker_seconds /
      worker_parallelism / 角色分布 / 合并尝试与冲突率**；
- [x] `tests/eval/compare.py`：通过率 Δ 与两臂 `spread` 比较，
      带内时明确拒绝下结论；逐任务翻转与 `noise_suspect` 一并报告；
- [x] `tests/eval/steering_bench.py`：pause/resume/cancel 延迟，
      `wait`（提交→边界）与 `apply`（边界→事件）分开；
- [x] default 套件 `--repeat 2` 实测（`tests/eval/phase4_baseline.json`，
      工作区未提交改动 → `comparable=false`）：**两轮均 29/29、spread 0**；
- [x] 无模型、无 WSL 的单测覆盖 compare / bench / 新机制字段。

实测补充：compare 对旧 `baseline.json`（12/29）报 Δ=+58.6pp、超出 6.9pp
噪声带，但右侧 `comparable=false` 且跨版本，不能归因于本次改动。default
套件已饱和，Phase 4 的后续调优应换更难任务或 `long` 套件。steering bench
（delay=50ms，repeat=3）实测 pause apply 中位 **9.4ms**、resume **20.2ms**、
cancel apply **8.6ms**。

### 5.5 B 批：实时事件与成本口径（已实现）

- [x] `WorkerStarted` 通过 LangGraph custom stream 在拿到并发槽位后**实时**发出，
      不再等整批 worker 结束回放；`attempt` 一并给出；
- [x] 只读 worker 完成即发 `WorkerFinished`；implement 在主工作区合并后发，
      `merged`/`merge_error` 准确；
- [x] runtime 同时消费 `custom` 与 `updates`，用 `worker_id:attempt` 去重，
      直接调用节点时由 updates 兜底；
- [x] `RunFinished` / `RunFailed` 增加 `worker_input_tokens` /
      `worker_output_tokens`，TUI 显示；主 agent token 口径不变；
- [x] eval 的 `max_worker_batch` 改为按活跃 worker 数统计，适配实时交错事件；
- [x] implement 的 git/worktree/快照/patch 与 scheduler 合并统一
      `asyncio.to_thread`，避免阻塞事件循环；
- [x] `ruff`、CI 子集、全量、WSL 集成、TUI 通过。

### 5.6 C 批：依赖语义与写编排烟雾（已实现）

- [x] `TaskSpec.depends_mode`：`full`（默认，必须 success）/ `any`
      （success 或 partial 即可开始）；
- [x] `ready_tasks` / `mark_blocked` 按任务各自的 mode 判定；
      failed/blocked/cancelled 任何模式都不满足；
- [x] implement 不允许 `any`（写任务不能建在未通过完整验证的依赖上），校验失败降级；
- [x] planner 提示要求 review/test 在只需产出时使用 `any`；
- [x] 真实 WSL 烟雾：带未跟踪 `.agent/` 的仓库 → survey 判定干净且为仓库根
      → 规则选择 `orchestrated_implement` → implement patch 合并并留下快照；
- [x] `ruff`、CI 子集、全量、WSL 集成通过。

**模型烟雾实测**（带 `.agent/leftover` 的临时仓库，写编排开启）：
workflow `orchestrated_implement`；planner 生成 2 个 implement + 1 test + 1 review；
两个 implement worker 均 `partial` 但 patch 都合并；test 与 review 均执行成功；
主 agent 最终 `pytest` **7 passed**。`depends_mode=any` 对 partial 依赖的
放行由 `test_partial_dependency_is_consumable_with_any_mode` 与
`test_dispatch_runs_an_any_mode_review_after_a_partial_dependency` 单测锁定。

### 5.7 D 批：worker 驱动的动态任务图（已实现）

- [x] explore/review 的结构化报告可带 `proposed_tasks`（每个 worker 最多 2 个）；
- [x] `scheduler` 在 worker 完成点消费提议，经 host 校验后加入
      pending：仅 explore/test/review、非空 goal、去重、依赖必须已存在、
      总任务数不超过 `orchestrator_max_tasks`、拒绝 write_scope 与环；
- [x] failed/cancelled worker 的提议不采纳；partial worker 的提议仍可采纳；
- [x] `TaskGraphExtended(added, rejected)` 事件 + `task_graph` 审计 + TUI 展示；
- [x] 递归上限同步增加 `extend_tasks` 超步；
- [x] 单测覆盖校验、节点、worker 报告映射、runtime 事件、TUI；
- [x] `ruff`、CI 子集、全量、WSL 集成通过。

## 6. 冻结

代码冻结后不再改动功能，只接受缺陷修复。任何改动都应：

1. 跑 `pytest -m "not llm"`（1420 个用例全过；见第 3 节口径提醒）
2. 跑 `ruff check .`
3. 如果是安全相关的改动，对照本文件第 2 节确认对应测试仍在
