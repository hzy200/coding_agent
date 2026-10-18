# CodingAgent

基于 LangGraph 的终端原生编程智能体。

核心设计只有一条：**宿主内置工具，模型只输出意图**。LLM 不直接接触操作系统，
只产出结构化工具调用 `{tool, args}`，由宿主校验后在 WSL2 沙箱里执行。
文件修改走专用工具（精确替换 + diff + 写前备份），不经 shell。

编排层采用 harness 范式：

> **orchestrator + 动态任务图 + 并发 worker + fan-in 汇总**

同时保留 `direct` 快速路径：小任务直接由单 agent 执行，大任务进入独立
orchestrator 子图，拆成 DAG 并发处理，最后把结构化结果交回主 agent 收尾。

## 当前状态

| 项 | 结果 |
|---|---|
| 测试 | `pytest -m "not llm"` **1420 passed**（含真实 WSL） |
| 覆盖率 | **89.1%** |
| CI 子集 | `pytest -m "not wsl and not llm"` **1213 passed**，覆盖率 **81.5%** |
| lint | `ruff check src tests` 干净 |
| 说明 | 另有 3 个 `llm` 标记用例需要 `DEEPSEEK_API_KEY`，默认排除；不加 `-m` 时会真实调用模型 |

四个选题创新点均已落地并有测试守着：双工具架构、可审计的命令安全策略、
可回滚的修改流程、失败驱动的修复循环。

## 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│ 前端：CLI / TUI / Web                                                │
│ 只消费 Event；不导入 tools / sandbox                                 │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ AsyncIterator[Event]
┌───────────────────────────────▼──────────────────────────────────────┐
│ AgentRuntime                                                         │
│  · 唯一编排入口   · checkpoint / audit / steering                    │
│  · 根据仓库规模选择 direct 或 orchestrated                           │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ LangGraph（subgraphs=True）
          ┌─────────────────────┴─────────────────────┐
          │ direct 快速路径                          │ harness 路径
          ▼                                           ▼
  planner → act ⇄ approval_gate → tools        orchestrator 子图
          → verify → review → advance            survey
          → steering_step → replan               → select_and_plan
          → respond                              → scheduler ⇄ steering_dispatch
                                                 → fan_in
                                                       │
                                                       ▼
                                                  planner → act → ...
          │
          │ 所有工具调用都是 {tool, args}
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ WSL2 沙箱（非 root；可选 bwrap 挂载命名空间）                        │
│  stdin → bash；ulimit/timeout；词法 + realpath 路径校验；写前快照     │
└──────────────────────────────────────────────────────────────────────┘
```

### 两条执行路径

**direct（快速路径）**

适合小仓库、单点修改、单一功能线。`planner` 分解步骤，`act ⇄ tools` 执行，
每步结束过 `verify`（行为对不对）和 `review`（代码干不干净）；失败进入
`repair`，修复预算用尽后由 `replan` 换做法。

**orchestrated（harness 路径）**

满足 `AGENT_ORCHESTRATOR_MIN_FILES` 且编排开启时进入。它在父图中是一个
**独立子图**：

```
survey
  → select_and_plan        # 规则优先；模糊时一次结构化调用同时给出 workflow + 任务图
  → scheduler              # 事件驱动 DAG
  ⇄ steering_dispatch      # 边界消费 /pause /cancel /add /replan
  → fan_in                 # 结构化汇总 + artifact
  → planner → act → ...    # 主 agent 负责最终实现、验证与答复
```

### 调度器

`orchestrator/scheduler.py` 不再按批 `gather`，而是：

- ready 最小堆 + `asyncio.wait(FIRST_COMPLETED)`；
- 任一任务完成就立即解锁依赖方，不必等整批；
- 失败任务立即短路下游；有重试额度时带上次失败上下文重跑；
- 动态只读提议在完成点校验后立即入队；
- `AGENT_ORCHESTRATOR_CHECKPOINT_EVERY` 到达时 drain 在飞任务再返回父图，
  形成 checkpoint / steering 边界；
- 全局 deadline 与 worker 级 timeout 兜底。

### Worker 与并发

| 角色 | 能做什么 | 隔离 |
|---|---|---|
| `explore` | 只读定位代码、总结事实 | 独立上下文；只读工具白名单 |
| `review` | 只读审查风险/遗漏 | 独立上下文；只读工具白名单 |
| `test` | 宿主运行项目测试并结构化输出 | 不调模型 |
| `implement` | 修改 `write_scope` 内文件 | 独立 git worktree；文件工具白名单 |

- worker 启动/结束通过 LangGraph custom stream 实时发出；
- 并发上限由 `AGENT_ORCHESTRATOR_MAX_CONCURRENT_WORKERS` 控制；
- `WslSandbox` 另有 `AGENT_SANDBOX_MAX_CONCURRENT_COMMANDS` 背压，避免并发
  worker 同时拉起过多 `wsl.exe`；
- implement worker 只绑定 `file_read/file_write/file_edit/search_code/find_files`，
  不绑定 shell/git/deps/test。

### 写隔离与 fan-in

每个 implement worker 在独立 worktree 中修改，完成后导出 patch：

```
git apply --check → 解析路径 → 为主工作区留快照 → git apply
```

合并由宿主串行执行，失败不会应用补丁。`write_scope` 有**写时校验**：
文件工具在 I/O 前就拒绝越界路径，事后 diff 校验保留为第二道防线。

fan-in 不再只做位置截断：

- 失败/阻塞任务优先展示；
- 每个任务有独立预算；
- 输出 `worker_context`（planner/act 使用）与 `worker_brief`（紧凑副本）；
- 完整结果落 `.agent/artifacts/<run>/<task>/attempt-N/result.json`；
- 报告多 worker 关注同一文件与未完成/阻塞任务。

### 事件边界

前端只消费 `events.py` 中的 pydantic 事件，不认识 LangGraph，也拿不到工具与沙箱。
CLI / TUI / Web 因此都无法绕过命令分级审批。这条边界由
`tests/unit/test_architecture.py` 用 AST 扫描守住。

细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与
[docs/HARNESS.md](docs/HARNESS.md)。

## 项目结构

```
CodingAgent/
├─ src/coding_agent/
│  ├─ runtime.py               # 唯一编排入口：图 + 沙箱 + 策略 + 审计 → 领域事件
│  ├─ events.py                # 前端契约：领域事件
│  ├─ config.py                # 配置（.env > 环境变量 > 默认值）
│  ├─ messages.py              # 消息文本提取
│  ├─ diffing.py               # unified diff 生成与统计
│  ├─ steering.py              # 按 thread 隔离的运行中指令队列
│  ├─ graph/                   # 主图
│  │  ├─ state.py              #   跨节点状态
│  │  ├─ routing.py            #   条件边（纯函数）
│  │  ├─ build.py              #   组装图 + recursion_limit 推导
│  │  └─ nodes/                #   planner·act·approve·tools·verify·review
│  │                           #   repair·advance·nudge·replan·respond·steering
│  ├─ orchestrator/            # 独立 harness 子图
│  │  ├─ state.py              #   OrchestratorState（窄状态契约）
│  │  ├─ subgraph.py           #   子图组装与直接/事件驱动两种拓扑
│  │  ├─ scheduler.py          #   事件驱动 DAG 调度
│  │  ├─ workflow.py           #   工作流选择 + 任务图预取
│  │  ├─ nodes.py              #   survey/plan/dispatch/extend/fan-in
│  │  ├─ workers.py            #   角色 → worker runner
│  │  ├─ implement.py          #   worktree 写 worker
│  │  ├─ merge.py              #   patch check/快照/apply
│  │  ├─ artifacts.py          #   fan-in artifact store
│  │  ├─ retry.py              #   重试上下文
│  │  ├─ graph.py              #   任务图校验 / ready / blocked
│  │  └─ contracts.py          #   TaskSpec / WorkerResult
│  ├─ agents/                  # explore/review 子智能体循环
│  ├─ sandbox/                 # 执行安全层
│  │  ├─ wsl_exec.py           #   WSL2 执行 + 可选 bwrap + 并发信号量
│  │  ├─ limits.py             #   ulimit / timeout
│  │  ├─ policy.py             #   命令分级、越界升级、引号感知、fail-closed
│  │  ├─ pathguard.py          #   词法路径守卫 + Windows↔WSL 转换
│  │  ├─ fs.py                 #   沙箱内读写 + realpath 校验 + run 级读缓存
│  │  └─ snapshots.py          #   写前快照
│  ├─ tools/                   # 能力层
│  │  ├─ registry.py           #   工具注册
│  │  ├─ artifacts.py          #   结构化产物
│  │  ├─ shell.py              #   shell_exec
│  │  ├─ files.py              #   file_read/write/edit/restore
│  │  ├─ git.py  deps.py       #   git_* / deps_*
│  │  ├─ search.py             #   search_code / find_files
│  │  ├─ testrun.py            #   run_tests + 验证输出解析
│  │  └─ delegate.py           #   delegate_explore
│  ├─ llm/                     # DeepSeek 客户端 · 提示词 · 上下文裁剪
│  ├─ memory/                  # checkpoint · 会话索引 · 长期记忆
│  ├─ audit/                   # JSONL 审计
│  └─ cli/ tui/ web/           # 三种前端
├─ tests/
│  ├─ unit/                    # 纯逻辑 + 假图驱动的 runtime 事件流
│  ├─ integration/             # 真实 WSL 沙箱
│  ├─ tui/                     # 无头界面
│  └─ eval/                    # 能力评测（需 API Key，不进 CI）
├─ docs/                       # 架构、harness、工作流、验收、威胁模型等
├─ scripts/demo.py             # 四场景演示脚本
├─ .github/workflows/ci.yml    # ruff + 快反馈测试
├─ Makefile                    # make check / test-all
├─ pyproject.toml              # 依赖与 extras
└─ README.md
```

**想改什么，看哪里**：

| 想改… | 落点 |
|---|---|
| 命令分级 / 安全规则 | `sandbox/policy.py`（新工具在 `graph/nodes/approve.py::tool_level` 登记） |
| 加一个工具 | `tools/` 写 builder → `tools/registry.py` 注册 → `approve.tool_level` 登记 |
| 加一个图节点 | `graph/nodes/` 写节点 → `graph/build.py` 接线 → `graph/routing.py` 加边 → `runtime._stream` 翻译事件 |
| 加一个 worker 角色 | `orchestrator/workers.py` 扩展 runner → `contracts.py` 角色枚举 → 工具白名单 |
| 改调度策略 | `orchestrator/scheduler.py`（ready/依赖/重试/deadline） |
| 改写隔离 | `orchestrator/implement.py` + `orchestrator/merge.py` + `tools/files.py` |
| 加一个前端 | 只依赖 `runtime.AgentRuntime` + `events`，**不要**导入 `tools/`/`sandbox/` |

**不纳入版本控制**：`.env`（密钥）、`.agent/`（审计、checkpoint、快照、artifact、
记忆）、`.venv/`。

## 快速开始

### 1. 环境准备

本项目开发环境固定使用 **conda 的 `agent` 环境**（Python 3.11）：

```bash
conda create -n agent python=3.11 -y
conda activate agent
pip install -e ".[dev,ui,web]"
```

不要用 `uv run` / `uvx` 创建临时 `.venv`；本仓库的验证命令都以
`D:/anaconda3/envs/agent/python.exe` 为准。

| extra | 装了它才能用 |
|---|---|
| （无） | `agent doctor` / `sandbox-init` / `chat` / `run` |
| `dev` | `pytest` / `ruff` / `pytest-xdist` / `pytest-cov` |
| `ui` | `agent tui`（Textual） |
| `web` | `agent web`（FastAPI + uvicorn） |

验证安装：

```bash
python -c "import sys, sysconfig; print(sys.executable); print(sysconfig.get_path('scripts'))"
agent --help
```

### 2. 配置与运行

```bash
cp .env.example .env              # 填入 DEEPSEEK_API_KEY
agent doctor                      # 环境自检（不需要 API Key）
agent sandbox-init                # 创建沙箱工作区
agent run "看看当前工作区有哪些文件"
agent tui                         # 终端界面
```

默认配置已启用 harness 路径；小仓库仍由 runtime 选择 direct 快速路径。
写 worker 需要额外设置 `AGENT_ORCHESTRATOR_WRITE_ENABLED=true` 并使用 `--write`。

### 命令一览

| 命令 | 用途 |
|---|---|
| `agent doctor` | 环境自检：Python、依赖、API Key、WSL、检索后端、shell 隔离 |
| `agent sandbox-init` | 创建并校验沙箱工作区 |
| `agent chat` | 纯流式对话（无工具），验证 API 联通 |
| `agent run <指令>` | 跑一轮任务；`-C` 指定工作区，`--write` 放开写入，`-y` 跳过确认 |
| `agent tui` | 终端界面（推荐，能力最全） |
| `agent web` | 最小 Web 验证界面（需 `.[web]`） |
| `agent snapshots` / `undo` / `diff` | 查看快照、回滚、看改动 |
| `agent sessions` | 历史会话列表 |
| `agent memory` | 查看/维护项目记忆 |
| `agent audit` | 查看审计日志 |

TUI 快捷键：`Ctrl+Q` 退出 · `Ctrl+N` 新会话 · `Ctrl+L` 清屏。

斜杠命令：`/help` `/new` `/clear` `/workspace` `/audit` `/snapshots` `/diff`
`/undo` `/sessions` `/switch` `/memory` `/remember` `/forget` `/quit`。

运行中干预：`/pause` `/resume` `/cancel` `/add [角色] <目标>` `/replan`。

## Harness 工作流

### 任务图生命周期

1. **survey**：一次 WSL 调用拿到顶层目录/文件、测试命令、git 是否干净；
2. **select_and_plan**：规则优先。规则不确定时，一次结构化调用同时返回
   `workflow` 与任务图，避免第二次规划往返；
3. **scheduler**：ready 堆调度；依赖满足即启动；失败立即短路；
4. **fan_in**：按失败/阻塞优先排序，生成紧凑汇总并落 artifact；
5. **主 agent**：带着 worker 结果进入 `planner → act → verify → review`，
   完成最终实现与答复。

### 任务图规则

- 任务必须有唯一 `id`、非空 `goal`；
- `depends_on` 必须存在、无自依赖、无环；
- `depends_mode=full` 要求依赖 success；`any` 允许消费 partial；
- implement 任务必须有非空 `write_scope`，且任务之间不能互相依赖；
- 并发 implement 的 `write_scope` 必须两两不重叠；
- 运行中只允许追加只读任务（explore/test/review）；
- 只读角色不能声明 `write_scope`。

校验失败时写模式退化为单个保守 implement；只读模式退化为单 explore，
并通过事件/审计显式标记 `degraded`。

### 运行中干预

| 命令 | 语义 |
|---|---|
| `/pause` | 在 scheduler 边界 drain 后 `interrupt()`，checkpoint 保存 |
| `/resume` | 从 checkpoint 继续 |
| `/cancel` | 停止派发；worker 在轮次/工具边界协作式退出 |
| `/add [角色] <目标>` | 追加只读任务 |
| `/replan` | 保留已完成任务，重建剩余任务图 |

### 成本与稳定性

- worker 可选用更便宜的模型：`AGENT_ORCHESTRATOR_WORKER_MODEL`；
- explore/review 收尾 JSON 可直接作为报告，省掉第二次报告调用；
- 只读 worker 共享 run 级内容缓存；
- survey 与 git 前置检查各自合并为一次 `wsl.exe`；
- 编排有全局 `AGENT_ORCHESTRATOR_RUN_DEADLINE_S`；
- sandbox 有并发命令上限。

## 安全模型

### 命令分级

| 级别 | 示例 | 行为 |
|---|---|---|
| L0 只读 | `ls` `cat` `grep` `git status` | 自动执行 |
| L1 低风险写 | `mkdir` `touch`、`file_edit`、`git add` | 需 `--write`，自动执行 + 审计 |
| L2 变更性 | `pip install`、`git commit`、`sed -i` | 需用户确认 |
| L3 危险 | `rm -rf`、`git push -f`、`curl \| sh`、`sudo` | 需用户确认（标注危险） |

复合命令按 `&&` `||` `;` `|` 分段取最高级别；无法解析的一律降级为 L2。
`cp` / `ln` / `mv` 刻意不自动放行：它们能绕过文件工具的精确替换、diff 与写前备份。

### 审批：决定与执行分离

- `approval_gate` 只做决定（放行/询问/拒绝），保持纯函数；
- `tools` 节点负责强制，是所有工具执行的唯一咽喉；
- **fail closed**：审批记录缺失、答复无法解析、非交互环境，一律拒绝；
- 审批结果一次性有效，不会跨轮次误放行。

### 并发 worker 的权限

- explore/review：只读白名单，构造时与运行时双重校验；
- test：不调模型，由宿主运行确定性验证命令；
- implement：只绑定文件/检索白名单，不绑定 shell/git/deps/test；
- implement 在独立 worktree 中运行；`write_scope` 在文件工具 I/O 前强制校验；
- 合并前 `git apply --check`、为目标文件留快照，合并串行执行；
- 并发区**不弹审批、不调用 `interrupt()`**；审批仍属于主图或编排边界。

### 可选隔离后端

默认 `AGENT_SHELL_SANDBOX=off`，靠工作区 cwd 约束 + 词法升级。要内核级隔离，
设 `AGENT_SHELL_SANDBOX=bwrap`：命令在挂载命名空间里执行，`/home`、`/root`、
`/mnt` 不可见、仅工作区可写；配置了却不可用时**拒绝执行**，不静默降级。

资产、假设与边界见 [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)。

## 工具

| 工具 | 等级 | 说明 |
|---|---|---|
| `shell_exec` | 按命令内容 | bash 命令，L2/L3 需确认 |
| `file_read` | L0 | 带行号读文件，支持 offset/limit |
| `file_write` / `file_edit` | L1 | 精确替换、diff、写前备份 |
| `file_restore` | L1 | 回滚到某次修改前 |
| `git_status` / `git_diff` / `git_log` | L0 | 只读 |
| `git_add` | L1 | 只动索引 |
| `git_commit` | L2 | 产生提交，需确认 |
| `deps_install` | L2 | 自动识别 uv/poetry/pnpm/yarn/npm/pip，需确认 |
| `deps_list` | L0 | 只读 |
| `search_code` / `find_files` | L0 | 按内容/文件名检索 |
| `run_tests` | L2 | 模型主动运行测试，需确认 |
| `delegate_explore` | L0 | 委派只读探索子智能体（需 `AGENT_EXPLORE_ENABLED=true`） |

L1 工具需要 `--write`；未登记的新工具按最危险的 L3 处理。
Git、依赖、检索做成结构化工具，参数无法逃逸、等级可判定、审计可读。

`.agent/` 禁止进入版本控制：`git_add` 拒绝暂存，`git_commit` 提交前检查，
经 shell 的 `git add -A` 在策略层升级为需确认。

## 文件修改与回滚

修改只走 `file_read` / `file_write` / `file_edit` / `file_restore`，不走 shell。
内容经 base64 在沙箱内落盘，不经过命令行解释。

- **精确替换**：`old_string` 必须唯一，出现 0 次或多次都拒绝并说明原因；
- **路径双重校验**：词法归一化 + 沙箱内 `realpath`；符号链接逃逸（含悬空链接）会被拒；
- **写前备份**：覆盖或编辑前存入 `.agent/backups/<快照id>/`；
- **编排写**：worker 在 worktree 中修改，合并前额外为主工作区留快照。

```bash
agent snapshots              # 列出快照
agent diff                   # 最近一次改动
agent undo                   # 回滚最近一次改动
```

回滚本身也可回滚：恢复前会先把当前内容留一份。

## 验证与修复循环

每步做完且改过东西时自动跑验证，把输出结构化成 `文件:行号 + 消息` 再回灌：

```
验证失败 FAILED (failures=1)
  · test_calc.py:14 self.assertEqual(average([]), 0.0)
  · ZeroDivisionError: division by zero
↻ 第 1/3 次修复 FAILED (failures=1)
```

- 命令按项目清单自动识别（Cargo / go / npm / pytest / make）；
- 识别不出测试命令但有源码时，降级为**语法解析级**验证（`kind=syntax`）；
- 什么都没跑时 `kind=none`，事件与审计的 `ok` 是三态（True/False/None）；
- 自动 verify 不走审批；模型主动调 `run_tests` 才走 L2；
- 修复次数有硬上限（默认 3，按步计算）；连续同一失败签名会改走 `replan`；
- verify 通过后还有 `review`：检查语法、被改弱的测试、调试残留、写进 `.agent/` 等。

## 会话与记忆

```bash
agent sessions                       # 历史会话
agent run --thread-id <id> "继续"     # 接着某个会话
agent memory --add "本仓库用 uv"      # 记一条项目约定
```

会话存在 SQLite 中，跨进程可恢复。会话列表直接归纳自审计日志。
项目记忆是 `.agent/memory.md` 的纯文本（一行一条），每次运行注入系统提示；
刻意做成显式增删，不做 LLM 自动提炼。

## 界面

**TUI 是主要界面**（`agent tui`）：计划面板、任务图/worker 时间线、流式输出、
审批弹窗。侧栏常驻显示会话、工作区与权限等级。

**Web 是最小验证**（`agent web`）：SSE 流式对话、多会话列表、API 联通测试。
一个自包含 HTML，不引前端构建链。Web 端固定拒绝变更类命令（无审批交互），
需要审批请用 TUI。

## 演示

```bash
python scripts/demo.py --list    # 看四个场景的说明与观察点
python scripts/demo.py           # 全部跑一遍（真实调用模型）
python scripts/demo.py --only 3  # 只跑某个
```

四个场景对应四个创新点：只读分析 → 命令被拦 → 修复循环 → 回滚。
讲稿见 [docs/DEMO.md](docs/DEMO.md)。

## 开发与测试

```bash
conda activate agent

make check            # lint + 快反馈测试
make test-all         # 全量（含真实 WSL）
make cov              # 快反馈子集 + 覆盖率门槛

pytest -m "not wsl and not llm"   # 单元 + TUI + Web
pytest -m "not llm"               # 全量（含真实 WSL）
ruff check src tests

# 能力评测（真实模型，成本自负）
python -m tests.eval.runner --suite default --repeat 2
# 噪声感知对比
python tests/eval/compare.py <baseline-a> <baseline-b>
# 干预延迟基准（不调用模型）
python tests/eval/steering_bench.py --repeat 3
```

CI 在无 WSL 的 runner 上跑 `ruff` + `pytest -m "not wsl and not llm" --cov`，
覆盖率门槛 76。沙箱层由本地 `-m wsl` 用例守着，CI 会显式提示这个缺口。

测试分层：

- `tests/unit`：纯逻辑，含假图驱动的 runtime 事件流；
- `tests/integration`：真实 WSL 沙箱，环境不具备时自动跳过；
- `tests/tui` 与 `tests/unit/test_web_app.py`：无头界面；
- `tests/eval`：能力评测，需 API Key，不进 CI。

关键不变量见
[ARCHITECTURE.md 第 6 节](docs/ARCHITECTURE.md#6-不变量)。

## 配置

复制 `.env.example` 为 `.env` 后按需修改。

> 优先级：**命令行/构造函数参数 > `.env` > 环境变量 > 默认值**。
> `agent doctor` 会列出两边不一致的键。

### 模型与追踪

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 必填 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 主模型 |
| `AGENT_ORCHESTRATOR_WORKER_MODEL` | 空 | worker 专用模型；空 = 继承主模型 |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` | `false` / — | 调用链追踪 |

### WSL2 沙箱

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_WSL_DISTRO` | `Ubuntu` | |
| `AGENT_WSL_WORKSPACE` | 空 = `$HOME/agent-ws` | 支持 Windows 路径写法 |
| `AGENT_SHELL_SANDBOX` | `off` | `off` / `bwrap`；配了 bwrap 但不可用时拒绝执行 |
| `AGENT_SHELL_TIMEOUT` | `60` | 单条命令墙钟上限 |
| `AGENT_SHELL_CPU_SECONDS` / `_MEMORY_MB` / `_MAX_FILE_MB` / `_MAX_PROCESSES` | `600` / `0` / `512` / `1024` | 0 = 不限制 |
| `AGENT_SANDBOX_MAX_CONCURRENT_COMMANDS` | `8` | 同时启动的 `wsl.exe` 上限 |

### 编排 / harness

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_ORCHESTRATOR_ENABLED` | `true` | 启用 harness；小仓库仍走 direct 快速路径 |
| `AGENT_ORCHESTRATOR_SUBGRAPH_ENABLED` | `true` | 独立 orchestrator 子图 |
| `AGENT_ORCHESTRATOR_EVENT_DRIVEN` | `true` | 事件驱动 DAG 调度 |
| `AGENT_ORCHESTRATOR_WRITE_SCOPE_ENFORCE` | `true` | 写时校验 `write_scope` |
| `AGENT_ORCHESTRATOR_FANOUT_ARTIFACTS` | `true` | fan-in 落 `.agent/artifacts/` |
| `AGENT_ORCHESTRATOR_MIN_FILES` | `30` | 文件数达到该值才进入编排 |
| `AGENT_ORCHESTRATOR_MAX_TASKS` | `6` | 任务总数（含动态扩展） |
| `AGENT_ORCHESTRATOR_MAX_CONCURRENT_WORKERS` | `4` | 并发 worker 上限 |
| `AGENT_ORCHESTRATOR_TASK_RETRIES` | `1` | 单任务失败重试次数 |
| `AGENT_ORCHESTRATOR_CHECKPOINT_EVERY` | `4` | 每完成多少个任务形成一次 checkpoint/干预边界 |
| `AGENT_ORCHESTRATOR_RUN_DEADLINE_S` | `900` | 整个编排阶段墙钟上限（0 = 不限制） |
| `AGENT_ORCHESTRATOR_CONTEXT_CHARS` | `8000` | 交给主 agent 的汇总上限（字符） |
| `AGENT_ORCHESTRATOR_ADD_EXPLORE_TESTS` | `false` | 单任务时是否强行补一个「找测试」explore |
| `AGENT_ORCHESTRATOR_MAX_ROUNDS` | `3` | 兼容旧按批 dispatch 的轮次上限 |
| `AGENT_EXPLORE_ENABLED` | `false` | 给主 agent 绑定 `delegate_explore`；orchestrator 的 explore/review runner 不受此开关影响 |
| `AGENT_ORCHESTRATOR_WRITE_ENABLED` | `false` | 并发写 worker；还需 `--write` + git 仓库根 + 干净工作区 |
| `AGENT_ORCHESTRATOR_WORKER_ROUNDS` / `_REPAIRS` / `_TIMEOUT` | `12` / `2` / `240` | implement worker 的工具预算 / 修复轮次 / 墙钟上限 |

### 审批 / 验证 / 审查

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_APPROVAL_MODE` | `ask` | `ask` / `approve` / `deny` |
| `AGENT_VERIFY_ENABLED` / `AGENT_VERIFY_COMMAND` | `true` / 空 | 自动验证；命令留空按清单识别 |
| `AGENT_VERIFY_ONLY_FINAL` | `true` | 多步任务只在 verify 步/最后一步跑全量 |
| `AGENT_REVIEW_ENABLED` | `true` | 验证通过后的确定性代码审查 |
| `AGENT_NUDGE_EMPTY_STEPS` | `true` | 一步无改动时给一次带提示的重做机会 |

### 上下文与循环

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_CONTEXT_MAX_CHARS` / `_KEEP_RECENT` / `_TOOL_CHARS` | `60000` / `12` / `1500` | 上下文裁剪；max=0 关闭 |
| `AGENT_MAX_PLAN_STEPS` / `_TOOL_ROUNDS` / `_REPAIR_ROUNDS` / `_MAX_RESUMES` | `5` / `12` / `3` / `50` | 循环与恢复上限 |
| `AGENT_MAX_REPLANS` | `2` | 执行中重建剩余计划的次数 |
| `AGENT_MAX_DELEGATE_CALLS` / `AGENT_MAX_CONCURRENT_AGENTS` | `6` / `4` | 主 agent 委派预算与并发 |

### 持久化与审计

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_CHECKPOINT_PATH` | 空 = `<cwd>/.agent/checkpoints.sqlite` | 填 `:memory:` 强制不落盘 |
| `AGENT_AUDIT_ENABLED` / `AGENT_AUDIT_DIR` / `AGENT_AUDIT_MAX_MB` | `true` / `<cwd>/.agent/audit` / `0` | 审计日志；0 = 不轮转 |

状态文件位置（宿主侧与沙箱侧）见 [docs/USAGE.md](docs/USAGE.md)。

## 已知限制与后续

- 按改动文件的测试影响分析尚未实现；implement worker 目前仍跑完整验证命令；
- 共享文件的多个 implement 仍靠 `write_scope` 不重叠来避免冲突，尚未实现 3-way 合并；
- steering 队列只在进程内，重启后未消费的 add/replan 会丢失；
- fan-in reducer、任务级 token 硬预算、429 自适应降并发尚未落地；
- 最近一轮只跑了非 LLM 回归；真实模型评测需自行执行并承担 API 费用。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层与依赖方向、运行数据流、状态生命周期、不变量、取舍理由 |
| [docs/HARNESS.md](docs/HARNESS.md) | 独立 orchestrator 子图、事件驱动 DAG、并发 worker、fan-in、干预与评测 |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | 运行 / 开发 / 使用三套工作流、路由与状态生命周期 |
| [docs/COMPLEXITY.md](docs/COMPLEXITY.md) | 复杂度度量、热点分布、耦合方向与降复杂度建议 |
| [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) | 环境配置、依赖版本快照、复现与导出、常见问题 |
| [docs/USAGE.md](docs/USAGE.md) | 常见工作流、配置来源、故障排查 |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | 需求逐条对照、自检清单、已知限制 |
| [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) | 保护对象、对手假设、T1/T2 边界 |
| [docs/DEMO.md](docs/DEMO.md) | 答辩演示讲稿（配 `scripts/demo.py`） |
