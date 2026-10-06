# CodingAgent

基于 LangGraph 的终端原生编程智能体。

核心设计只有一条：**宿主内置工具，模型只输出意图**。LLM 不直接接触操作系统，
只产出结构化工具调用 `{tool, args}`，由宿主校验后在 WSL2 沙箱里执行。
文件修改走专用工具（精确替换 + diff + 写前备份），不经 shell。

## 当前进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M1**（W1–W4） | 骨架、任务规划、文件工具、审计与持久化 | ✅ |
| **M2**（W5–W8） | 命令审批、Git/依赖工具、回滚、会话与记忆 | ✅ |
| **M3**（W9–W12） | verify 节点、失败修复循环、仓库检索与上下文裁剪、追踪与 prompt 调优 | ✅ |
| **M4**（W13–W16） | 测试补全（覆盖率 75%→89%）、TUI 打磨、Web 最小验证、文档与演示脚本 | ✅ |

四个选题创新点均已落地并有测试守着：双工具架构、可审计的命令安全策略、
可回滚的修改流程、失败驱动的修复循环。

**当前状态**：`pytest -m "not llm"` **1194 passed**、覆盖率 **90.0%**、lint 干净。
（另有 3 个 `llm` 标记用例需要 `DEEPSEEK_API_KEY`，默认排除；不加 `-m` 时它们会真的调用模型。）
CI 只跑不需要 WSL2 的子集（993 用例 / 覆盖 80.2%），覆盖率门槛设在 76 —— 沙箱层那部分
只在本地守得住，原因与差距见 [docs/IMPROVEMENT_PLAN.md](docs/IMPROVEMENT_PLAN.md) 的 Q1。
代码冻结后又做了多轮**缺陷收口与安全纵深**：大文件读取上限在读前生效、shell 变更也触发验证、
只读命令参数越界升级、可选 `bwrap` 内核级隔离、写入体积不再受 argv 上限卡住（此前超过约 96 KB
必然失败）、`$'…'` 与 `~user` 形式的越界读不再漏判、自动 verify 不再执行模型可写的 manifest
里的命令，等等。逐条需求对照与已知限制见 [ACCEPTANCE.md](docs/ACCEPTANCE.md)，加固记录见
[PLAN.md](docs/PLAN.md) §8、[BUG_AUDIT_2.md](docs/BUG_AUDIT_2.md) 与
[PHASE_A.md](docs/PHASE_A.md)。

**两道质量关**：`verify` 跑项目自己的测试（行为对不对），`review` 在验证通过后做确定性
代码审查（干不干净）——语法坏没坏、测试有没有被改弱、是否留下 `breakpoint()`、有没有写进
`.agent/`。审查的「改动前」直接复用快照，只审新增行；阻断项与验证失败共用同一份修复预算。
能力评测也随之补上**机制指标**（验证/修复/重规划次数、token）与**噪声地板**（`--repeat`），
以及"不可比的数字不许冒充基线"的守卫。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层与依赖方向、一次运行的数据流、图状态生命周期、**15 条不变量**、取舍理由、扩展点 |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | **三套工作流**（运行 / 开发 / 使用）的设计、路由与状态生命周期、**评价与改进项** |
| [docs/COMPLEXITY.md](docs/COMPLEXITY.md) | **复杂度分析**：AST 度量、热点分布、耦合方向、**本质/偶然复杂度**区分与降复杂度建议 |
| [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) | **环境配置完整流程**、前置条件、**实测依赖版本快照**、复现与导出、常见问题 |
| [docs/USAGE.md](docs/USAGE.md) | 常见工作流、配置来源、**故障排查** |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | **需求逐条对照**、自检清单、已知限制 |
| [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) | **保护什么、假设对手是谁**、T1（防误操作）/ T2（防越狱）的边界与达标前置条件 |
| [docs/DEMO.md](docs/DEMO.md) | 答辩演示讲稿（配 `scripts/demo.py`） |
| [docs/PLAN.md](docs/PLAN.md) | 开发方案、四个月排期、风险与进度 |
| [docs/IMPROVEMENT_PLAN.md](docs/IMPROVEMENT_PLAN.md) | **技术分析报告 + 三个月改进计划**：以「高性能自主编码智能体」为目标的缺陷分级（P0–P3）与 W1–W12 排期 |
| [docs/BUG_AUDIT.md](docs/BUG_AUDIT.md) | **功能缺陷与 BUG 审计（第一轮）**：14 项按严重度归档的实测缺陷（含证据位置、触发路径、修复方向与优先级） |
| [docs/BUG_AUDIT_2.md](docs/BUG_AUDIT_2.md) | **第二轮审计**：修复后重新审计出的 8 项（三份沙箱实例、文件工具冗余 `resolve`、上下文预算失效、审计每条扫目录、会话回溯单位、dirty 与 ok 耦合、异步不可取消等），含对第一轮一处收益记述的更正。**C1–C8 已全部修复** |

## 快速开始

### 1. 环境准备

本项目当前使用 conda 环境 **`agent`**（Python 3.11）。环境已存在时，激活即可：

```bash
conda activate agent
```

从零复现（在新机器上，或想重建环境时）：

```bash
conda create -n agent python=3.11 -y
conda activate agent
pip install -e ".[dev,ui,web]"     # 可编辑安装 + 各组依赖，见下表
```

`pip install` 的 extra 组按需取舍：

| extra | 装了它才能用 |
|---|---|
| （无） | `agent doctor` / `sandbox-init` / `chat` / `run` 与各子命令（运行时依赖） |
| `dev` | `pytest` / `ruff` / `pytest-xdist` / `pytest-cov` —— 跑测试与 lint |
| `ui` | `agent tui`（Textual） |
| `web` | `agent web`（FastAPI + uvicorn） |

> ⚠️ **必须先激活环境**：`agent` 命令装在环境自己的 `Scripts/` 下
> （conda 为 `D:\anaconda3\envs\agent\Scripts`），未激活时不在 PATH 上，直接敲 `agent` 会「找不到命令」。

验证安装是否就绪：

```bash
python -c "import sys, sysconfig; print(sys.executable); print(sysconfig.get_path('scripts'))"
agent --help
```

> **用普通 venv 替代 conda 也可以**：`python -m venv .venv` 后激活
> （Windows `.venv/Scripts/activate`；macOS/Linux `source .venv/bin/activate`），
> 后续安装与运行命令完全一致。`scripts/demo.py` 会依次在当前环境 / PATH / `.venv`
> 中定位 `agent` 入口，两种布局都支持。

完整流程（前置条件、实测依赖版本快照、复现与导出、常见问题）见
[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)。

### 2. 配置与运行

```bash
cp .env.example .env              # 填入 DEEPSEEK_API_KEY
agent doctor                      # 环境自检（不需要 API Key）
agent sandbox-init                # 创建沙箱工作区
agent run "看看当前工作区有哪些文件"
agent tui                         # 终端界面
```

### 命令一览

| 命令 | 用途 |
|---|---|
| `agent doctor` | 环境自检：Python、依赖、API Key、WSL 沙箱、检索后端、shell 隔离后端 |
| `agent sandbox-init` | 创建并校验沙箱工作区 |
| `agent chat` | 纯流式对话（无工具），验证 API 联通 |
| `agent run <指令>` | 跑一轮任务；`-C` 指定工作区，`--write` 放开写入，`-y` 跳过确认 |
| `agent tui` | 终端界面（**推荐，能力最全**） |
| `agent web` | 最小 Web 验证界面（需 `.[web]`） |
| `agent snapshots` / `undo` / `diff` | 查看快照、回滚、看改动 |
| `agent sessions` | 历史会话列表 |
| `agent memory` | 查看/维护项目记忆 |
| `agent audit` | 查看审计日志 |

TUI 快捷键 `Ctrl+Q` 退出 · `Ctrl+N` 新会话 · `Ctrl+L` 清屏。
斜杠命令：`/help` `/new` `/clear` `/workspace` `/audit` `/snapshots` `/diff` `/undo`
`/sessions` `/switch` `/memory` `/remember` `/forget` `/quit`。

## 架构

```
                          ┌──────────────────────────┐
                          │   前端 CLI / TUI / Web    │
                          └───────────┬──────────────┘
                                      │ AsyncIterator[Event]
                          ┌───────────▼──────────────┐
                          │       AgentRuntime        │  唯一编排入口
                          │  图 + 沙箱 + 安全策略 + 审计 │
                          └───────────┬──────────────┘
                                      │ LangGraph
   START → planner → act ─┬─→ approval_gate → tools ─→ act
                          └─→ verify ─┬─(通过)─→ advance → replan ─┬─→ act
                                      │                            └─→ respond → END
                                      ├─(失败，有预算)→ repair ─→ act
                                      └─(失败，预算尽)→ respond → END
                                      │ {tool, args}
                          ┌───────────▼──────────────┐
                          │   WSL2 沙箱（非 root）     │
                          └──────────────────────────┘
```

沙箱以非 root 运行于 WSL2；可选 `AGENT_SHELL_SANDBOX=bwrap` 做挂载命名空间隔离（见「安全模型」）。

**前端只消费事件**，不认识 LangGraph，也拿不到工具与沙箱 ——
因此 CLI / TUI / Web 都无法绕过命令分级审批。事件是 pydantic 模型，
`model_dump_json()` 出来即 SSE 数据帧，所以接 Web 不需要动编排层。

这条边界是**安全属性而非代码风格**，由 `tests/unit/test_architecture.py` 用 AST 扫描守住：
`tui/` 与 `web/` 若导入 `coding_agent.tools` 或 `coding_agent.sandbox` 直接失败。

细节见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 项目结构

```
CodingAgent/
├─ src/coding_agent/           # 全部源码（依赖方向自上而下，见「架构」）
│  ├─ runtime.py               # 唯一编排入口：图 + 沙箱 + 策略 + 审计 → 领域事件
│  ├─ events.py                # 前端契约：领域事件（CLI/TUI/Web 共用）
│  ├─ config.py                # 配置（.env > 环境变量 > 默认值）
│  ├─ messages.py              # 消息取文本（str / content blocks 统一）
│  ├─ diffing.py               # unified diff 生成与统计
│  ├─ graph/                   # LangGraph 编排
│  │  ├─ state.py              #   跨节点状态（生命周期见 ARCHITECTURE §5）
│  │  ├─ routing.py            #   条件边（纯函数）
│  │  ├─ build.py              #   组装图 + recursion_limit 推导
│  │  └─ nodes/                #   planner·act·approve·tools·verify·repair·advance·respond
│  ├─ sandbox/                 # 执行安全层
│  │  ├─ wsl_exec.py           #   WSL2 执行（脚本经 stdin）+ 可选 bwrap 隔离
│  │  ├─ limits.py             #   ulimit / timeout 资源上限
│  │  ├─ policy.py             #   命令分级、越界升级、引号感知、fail-closed
│  │  ├─ pathguard.py          #   词法路径守卫 + Windows↔WSL 转换
│  │  ├─ fs.py                 #   沙箱内文件读写（词法 + realpath 双重校验）
│  │  └─ snapshots.py          #   写前快照（回滚；回滚本身可回滚）
│  ├─ tools/                   # 能力层：模型可调用的结构化工具
│  │  ├─ registry.py           #   工具注册（决定暴露哪些）
│  │  ├─ artifacts.py          #   结构化产物封装（事件/审计据此，不解析文本）
│  │  ├─ shell.py              #   shell_exec（按命令内容分级）
│  │  ├─ files.py              #   file_read/write/edit/restore（精确替换 + 备份）
│  │  ├─ git.py  deps.py       #   git_* / deps_*（参数逐条 quote、包名白名单）
│  │  ├─ search.py             #   search_code / find_files（rg 优先，grep 兜底）
│  │  └─ testrun.py            #   run_tests + 验证输出结构化解析
│  ├─ llm/                     # DeepSeek 客户端 · 提示词 · 上下文裁剪
│  ├─ memory/                  # checkpoint(SQLite) · 会话索引(归纳审计) · 长期记忆
│  ├─ audit/                   # JSONL 审计（按天分文件，可按大小轮转）
│  └─ cli/ tui/ web/           # 三种前端：只消费事件，不得导入 tools/sandbox
├─ tests/                      # 测试（四层，见「开发」）
│  ├─ unit/                    #   纯逻辑（含假图驱动的 runtime 事件流）
│  ├─ integration/             #   真实 WSL 沙箱（无环境自动跳过）
│  ├─ tui/                     #   无头驱动界面
│  └─ eval/                    #   端到端能力评测（20 任务，需 API Key，不进 CI）
├─ docs/                       # 9 份文档（见上表）
├─ scripts/demo.py             # 四场景演示脚本（对应四个创新点）
├─ .github/workflows/ci.yml    # CI：ruff + `pytest -m "not wsl and not llm"`
├─ Makefile                    # `make check` / `make test-all`
├─ pyproject.toml              # 依赖与打包（extras: dev/ui/web）
└─ README.md
```

**想改什么，看哪里**：

| 想改… | 落点 |
|---|---|
| 命令分级 / 安全规则 | `sandbox/policy.py`（新工具记得在 `graph/nodes/approve.py::tool_level` 登记等级） |
| 加一个工具 | `tools/` 写 builder → `tools/registry.py` 注册 → `approve.tool_level` 登记 |
| 加一个图节点 | `graph/nodes/` 写节点 → `graph/build.py` 接线 → `graph/routing.py` 加边 → `runtime._stream` 翻译事件 |
| 加一个前端 | 只依赖 `runtime.AgentRuntime` + `events`，**不要**导入 `tools/`/`sandbox/` |
| 配置项 | `config.py` 加字段 → `.env.example` / README 配置表同步 |

**不纳入版本控制**：`.env`（密钥）、`.agent/`（审计、checkpoint、快照、记忆）、`.venv/`。

## 安全模型

命令按风险分四级，判定**完全在宿主侧完成**，不依赖模型自我申报：

| 级别 | 示例 | 行为 |
|---|---|---|
| L0 只读 | `ls` `cat` `grep` `git status` | 自动执行 |
| L1 低风险写 | `mkdir` `touch`、`file_edit`、`git add` | 需 `--write`，自动执行 + 审计 |
| L2 变更性 | `pip install`、`git commit`、`sed -i` | 需用户确认 |
| L3 危险 | `rm -rf`、`git push -f`、`curl \| sh`、`sudo` | 需用户确认（标注危险） |

复合命令按 `&&` `||` `;` `|` 分段取最高级别；**无法解析的一律降级为 L2**。
`cp` / `ln` / `mv` 刻意不自动放行：它们能覆盖或替换文件，
会绕过文件工具的精确替换、diff 与写前备份。

### 审批：决定与执行分离

- **`approval_gate` 只做决定**（放行/询问/拒绝），保持纯函数 ——
  `interrupt()` 恢复时节点会从头重跑
- **`tools` 节点负责强制** —— 它是所有工具执行的唯一咽喉，
  没拿到 `auto`/`approved` 的调用一律不执行
- **fail closed**：审批记录缺失、答复无法解析、非交互环境（管道 / CI），一律按拒绝处理
- 审批结果一次性有效，不会跨轮次误放行

### 越界与隐藏命令

**只读命令名不代表参数安全**：`cat ~/.ssh/id_rsa` 是只读命令，却读到工作区外。因此对
本来会自动放行的命令再做一层升级，命中即降为 L2 人工确认：

- 参数里出现 `~`、工作区外的绝对路径、`..` 穿越
- 出现 `$()` / 反引号 / 变量替换（会执行隐藏命令）
- `find -exec/-delete`、`git add -A`（整树暂存会带上 `.agent/`）、任何 `.agent` 引用

危险模式判定是**引号感知**的：`grep "rm -rf" docs/` 只是搜索、不误判；而
`sh -c "rm -rf /"`、`echo "$(rm -rf /)"` 里的内容会被执行，照常拦截。

### 可选的隔离后端

默认（`AGENT_SHELL_SANDBOX=off`）靠工作区 cwd 约束 + 上面的词法升级。要**内核级**隔离，
设 `AGENT_SHELL_SANDBOX=bwrap`：命令在挂载命名空间里执行，`/home`、`/root`、`/mnt`
（Windows 盘）不可见、仅工作区可写；配置了却不可用时**拒绝执行**，不静默降级。
资产、假设与"从防误操作升到防越狱还差什么"见 [THREAT_MODEL.md](docs/THREAT_MODEL.md)。

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
| `search_code` / `find_files` | L0 | 按内容/文件名检索，返回「文件:行号:内容」 |
| `run_tests` | L2 | 运行项目测试，需确认（自动 verify 不走审批） |

L1 工具需要 `--write`；**未登记的新工具按最危险的 L3 处理**（故意的 fail closed）。

Git、依赖、检索做成结构化工具而不是让模型拼 shell 命令，换来三件事：
参数无法逃逸、等级可判定、审计可读。依赖包名走白名单正则，
`--index-url=...`、`requests; rm -rf /`、`$(whoami)` 一类输入会在执行前被拒。

另外 `.agent/`（agent 自己的工作目录）**禁止进入版本控制**：
`git_add` 拒绝暂存、`git_commit` 提交前检查暂存区；经 shell 的 `git add -A`（整树暂存）
也会在策略层升级为需确认，而不是等到提交前才发现。

## 文件修改与回滚

修改只走 `file_read` / `file_write` / `file_edit` / `file_restore`，不走 shell。
内容经 base64 在沙箱内落盘，不经过命令行解释。

- **精确替换**：`old_string` 必须唯一，出现 0 次或多次都拒绝并说明原因，让模型补充上下文
- **路径双重校验**：词法归一化 + 沙箱内 `realpath`；符号链接指向外部（含**悬空链接**）会被拒
- **写前备份**：覆盖或编辑前存入 `.agent/backups/<快照id>/`

```bash
agent snapshots              # 列出快照
agent diff                   # 最近一次改动变成了什么样
agent undo                   # 回滚最近一次改动
```

**回滚本身也可回滚**：恢复前会先把当前内容留一份，所以一次误回滚不会把
你当下的改动抹掉，再 `undo` 一次就滚回来。

## 验证与修复循环

每步做完（且该步改过东西）**自动**跑一遍项目的测试/构建，把输出结构化成
`文件:行号 + 消息` 再回灌，而不是丢一大坨 stdout 让模型自己找：

```
验证失败 FAILED (failures=1)
  · test_calc.py:14 self.assertEqual(average([]), 0.0)
  · ZeroDivisionError: division by zero
↻ 第 1/3 次修复 FAILED (failures=1)
```

命令按项目清单自动识别（Cargo / go / npm / pytest / make），识别不出返回
`not_configured`（**不算失败**）。

**自动 verify 不走审批** —— 它跑的是宿主推导出的固定命令，模型影响不了跑什么；
模型主动调 `run_tests` 才走 L2。区别在「谁决定执行什么」。

**重试上限是硬性的**（默认 3 次，按步计算）。没有上限的自动修复是个烧 token 的
无底洞，而且用户永远拿不到「我试过了但没修好」这个结论。

## 会话与记忆

```bash
agent sessions                       # 历史会话
agent run --thread-id <id> "继续"     # 接着某个会话往下做
agent memory --add "本仓库用 uv"      # 记一条项目约定
```

会话存在 SQLite 里，跨进程可恢复。**会话列表不另建存储** —— 直接归纳自审计日志，
代价是审计关闭时列不出会话。

项目记忆是工作区内 `.agent/memory.md` 的纯文本（一行一条，可直接手工编辑），
每次运行注入系统提示。刻意做成显式增删而不做 LLM 自动提炼。

## 界面

**TUI 是主要界面**（`agent tui`）：计划面板、工具时间线、流式输出、审批弹窗。
侧栏常驻显示当前会话、工作区与**权限等级** ——「我现在有没有写权限」是每次
操作前都该看得见的信息。

**Web 是最小验证**（`agent web`）：SSE 流式对话、多会话列表、API 联通测试。
一个自包含的 HTML，**不引前端构建链**。

> Web 端**固定拒绝**变更类命令（没有做审批交互）。与其让 L2/L3 命令悬着等一个
> 永远不会来的答复，不如明确拒绝并在页面上说清楚。需要审批请用 TUI。

## 演示

```bash
python scripts/demo.py --list    # 看四个场景的说明与观察点
python scripts/demo.py           # 全部跑一遍（真实调用模型）
python scripts/demo.py --only 3  # 只跑某个
```

四个场景对应四个创新点，顺序递进：只读分析 → 命令被拦 → 修复循环 → 回滚。
讲稿见 [docs/DEMO.md](docs/DEMO.md)。

## 开发

环境准备（含 `dev` 依赖）见上方「快速开始 §1 环境准备」。注意 `pytest` 的 addopts
默认带 `-n auto`，缺 `pytest-xdist` 会直接报错 —— 装 `.[dev]` 即可；
`make cov` 另外需要 `pytest-cov`（同样在 `.[dev]` 里）。

```bash
conda activate agent  # 先激活项目环境（或 .venv）

make check            # lint + 快反馈测试（推荐）
make test-all         # 全量（含真实 WSL 沙箱）
make cov              # 与 CI 同口径：快反馈子集 + 覆盖率门槛

pytest -m "not wsl and not llm"   # 快反馈循环：单元 + TUI + Web，约 1 分钟
pytest -m "not llm"               # 全量（含真实 WSL 沙箱），约 2–3 分钟
ruff check .          # lint
```

CI（`.github/workflows/ci.yml`）在无 WSL 的托管 runner 上跑 `ruff` + 
`pytest -m "not wsl and not llm" --cov`，覆盖率门槛 **76**（`pyproject.toml` 的
`[tool.coverage.report]`）。**沙箱层不在 CI 覆盖范围内**：那部分用例由本地的
`-m wsl` 守着，CI 会以 `::warning::` 显式提示这个缺口 —— 差距与原因见
[docs/IMPROVEMENT_PLAN.md](docs/IMPROVEMENT_PLAN.md) 的 Q1。

默认并行（`-n auto`）。集成测试的开销几乎全在 `wsl.exe` 进程启动上（每次约 0.3 秒），
完全受 I/O 限制，所以并行几乎线性加速 —— 串行跑一遍要十分钟。

代码内部也按这个原则设计：`SandboxFs` 把「校验 + 状态 + 读取」压进**一次**进程启动。

测试分三层：`tests/unit`（纯逻辑，含**用假图驱动的 runtime 事件流测试**）、
`tests/integration`（真实 WSL 沙箱，环境不具备时自动跳过）、
`tests/tui` 与 `tests/unit/test_web_app.py`（无头驱动界面，注入假 runtime ——
不需要 API Key 也不需要 WSL）。

此外还有一层**能力评测**（`tests/eval`，20 个任务分单文件改动 / 跨文件重构 / 排障修复）：
它度量"任务能不能做完"，不是回归，需要 API Key 且不进 CI。判定前会把测试文件
还原成纯净副本，所以改测试没有收益。见 [tests/eval/README.md](tests/eval/README.md)：

```bash
python tests/eval/runner.py --verify-seeds   # 种子自检，不需要 API Key
python tests/eval/runner.py --list           # 任务清单
python tests/eval/runner.py                  # 全量，结果写 baseline.json
```

**非 WSL 用例不得真的执行沙箱命令**：`tests/conftest.py` 的守卫会把这类调用换成
当场失败（任何平台，不只是 Linux CI）。确实要探测宿主 WSL 的用例（如 `doctor`）
标 `@pytest.mark.wsl_env`；需要真实沙箱的集成用例标 `@pytest.mark.wsl`。

关键回归都有测试守着，完整清单见
[ARCHITECTURE.md 第 6 节「不变量」](docs/ARCHITECTURE.md#6-不变量)。

## 配置

复制 `.env.example` 为 `.env` 后按需修改。

> ⚠️ 优先级是 **命令行/构造函数参数 > `.env` > 环境变量 > 默认值**，
> 这里刻意偏离了「环境变量 > .env」的通用约定（原因见
> [USAGE.md](docs/USAGE.md)）。`agent doctor` 会把两边不一致的键列出来。

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 必填 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 建议保持默认；`deepseek-v4-flash` 有已知 `reasoning_content` 400（见 [USAGE 排查](docs/USAGE.md#模型报-reasoning_content-must-be-passed-back)） |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` | `false` / — | 调用链追踪 |
| `AGENT_WSL_DISTRO` | `Ubuntu` | |
| `AGENT_WSL_WORKSPACE` | 空 = `$HOME/agent-ws` | 沙箱内工作区，支持 Windows 路径写法 |
| `AGENT_SHELL_SANDBOX` | `off` | `off` / `bwrap`（挂载命名空间隔离：`/home`、`/root`、`/mnt` 不可见，仅工作区可写）；配了 `bwrap` 却不可用时**拒绝执行**，不静默降级 |
| `AGENT_SHELL_TIMEOUT` | `60` | 单条命令墙钟上限（秒） |
| `AGENT_SHELL_CPU_SECONDS` / `_MEMORY_MB` / `_MAX_FILE_MB` / `_MAX_PROCESSES` | `600` / `0` / `512` / `1024` | 0 表示不限制 |
| `AGENT_CHECKPOINT_PATH` | 空 = `<cwd>/.agent/checkpoints.sqlite` | 填 `:memory:` 强制不落盘 |
| `AGENT_AUDIT_ENABLED` / `AGENT_AUDIT_DIR` / `AGENT_AUDIT_MAX_MB` | `true` / `<cwd>/.agent/audit` / `0` | 审计日志；超 `MAX_MB` 就轮转（0=不轮转） |
| `AGENT_APPROVAL_MODE` | `ask` | `ask` / `approve` / `deny` |
| `AGENT_VERIFY_ENABLED` / `AGENT_VERIFY_COMMAND` | `true` / 空 | 自动验证；命令留空则按清单识别 |
| `AGENT_REVIEW_ENABLED` | `true` | 验证通过后的代码审查（检查语法/被改弱的测试/调试残留） |
| `AGENT_NUDGE_EMPTY_STEPS` | `true` | 一步没产生任何改动时给它一次带提示的重做机会 |
| `AGENT_CONTEXT_MAX_CHARS` / `_KEEP_RECENT` / `_TOOL_CHARS` | `60000` / `12` / `1500` | 上下文裁剪；max=0 关闭 |
| `AGENT_MAX_PLAN_STEPS` / `_TOOL_ROUNDS` / `_REPAIR_ROUNDS` / `_MAX_RESUMES` | `5` / `12` / `3` / `50` | 循环与恢复次数上限 |
| `AGENT_MAX_REPLANS` | `2` | 执行中重建剩余计划的次数上限（0 = 不重规划） |

状态文件位置（宿主侧与沙箱侧是两处，别混淆）见
[USAGE.md](docs/USAGE.md)。
