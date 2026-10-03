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

**当前状态**：687 个测试通过、覆盖率 89%、lint 干净，代码已冻结。
逐条需求对照与已知限制见 [ACCEPTANCE.md](docs/ACCEPTANCE.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层与依赖方向、一次运行的数据流、图状态生命周期、**9 条不变量**、取舍理由、扩展点 |
| [docs/USAGE.md](docs/USAGE.md) | 常见工作流、配置来源、**故障排查** |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | **需求逐条对照**、自检清单、已知限制 |
| [docs/DEMO.md](docs/DEMO.md) | 答辩演示讲稿（配 `scripts/demo.py`） |
| [docs/PLAN.md](docs/PLAN.md) | 开发方案、四个月排期、风险与进度 |

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/activate            # macOS/Linux 用 source .venv/bin/activate
pip install -e ".[dev,ui,web]"    # ui=TUI，web=Web 最小版，dev=测试

cp .env.example .env              # 填入 DEEPSEEK_API_KEY

agent doctor                      # 环境自检（不需要 API Key）
agent sandbox-init                # 创建沙箱工作区
agent run "看看当前工作区有哪些文件"
agent tui                         # 终端界面
```

### 命令一览

| 命令 | 用途 |
|---|---|
| `agent doctor` | 环境自检：Python、依赖、API Key、WSL 沙箱、检索后端 |
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
                          └─→ verify ─┬─(通过)──────→ advance ─→ act
                                      ├─(失败，有预算)→ repair ─→ act
                                      └─(失败，预算尽)→ respond → END
                                      │ {tool, args}
                          ┌───────────▼──────────────┐
                          │   WSL2 沙箱（非 root）     │
                          └──────────────────────────┘
```

**前端只消费事件**，不认识 LangGraph，也拿不到工具与沙箱 ——
因此 CLI / TUI / Web 都无法绕过命令分级审批。事件是 pydantic 模型，
`model_dump_json()` 出来即 SSE 数据帧，所以接 Web 不需要动编排层。

这条边界是**安全属性而非代码风格**，由 `tests/unit/test_architecture.py` 用 AST 扫描守住：
`tui/` 与 `web/` 若导入 `coding_agent.tools` 或 `coding_agent.sandbox` 直接失败。

细节见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。

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
`git_add` 拒绝暂存、`git_commit` 提交前检查暂存区，用 shell `git add -A` 绕过也拦得住。

## 文件修改与回滚

修改只走 `file_read` / `file_write` / `file_edit` / `file_restore`，不走 shell。
内容经 base64 在沙箱内落盘，不经过命令行解释。

- **精确替换**：`old_string` 必须唯一，出现 0 次或多次都拒绝并说明原因，让模型补充上下文
- **路径双重校验**：词法归一化 + 沙箱内 `realpath`，符号链接指向外部会被拒
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

```bash
pytest -m "not wsl"   # 快反馈循环：单元 + TUI，约 45 秒
pytest                # 全量（含真实 WSL 沙箱），约 2 分钟
ruff check .          # lint
```

默认并行（`-n auto`）。集成测试的开销几乎全在 `wsl.exe` 进程启动上（每次约 0.3 秒），
完全受 I/O 限制，所以并行几乎线性加速 —— 串行跑一遍要十分钟。

代码内部也按这个原则设计：`SandboxFs` 把「校验 + 状态 + 读取」压进**一次**进程启动。

测试分三层：`tests/unit`（纯逻辑，含**用假图驱动的 runtime 事件流测试**）、
`tests/integration`（真实 WSL 沙箱，环境不具备时自动跳过）、
`tests/tui` 与 `tests/unit/test_web_app.py`（无头驱动界面，注入假 runtime ——
不需要 API Key 也不需要 WSL）。

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
| `DEEPSEEK_MODEL` | `deepseek-chat` | |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` | `false` / — | 调用链追踪 |
| `AGENT_WSL_DISTRO` | `Ubuntu` | |
| `AGENT_WSL_WORKSPACE` | 空 = `$HOME/agent-ws` | 沙箱内工作区，支持 Windows 路径写法 |
| `AGENT_SHELL_TIMEOUT` | `60` | 单条命令墙钟上限（秒） |
| `AGENT_SHELL_CPU_SECONDS` / `_MEMORY_MB` / `_MAX_FILE_MB` / `_MAX_PROCESSES` | `600` / `0` / `512` / `1024` | 0 表示不限制 |
| `AGENT_CHECKPOINT_PATH` | 空 = `<cwd>/.agent/checkpoints.sqlite` | 填 `:memory:` 强制不落盘 |
| `AGENT_AUDIT_ENABLED` / `AGENT_AUDIT_DIR` | `true` / `<cwd>/.agent/audit` | |
| `AGENT_APPROVAL_MODE` | `ask` | `ask` / `approve` / `deny` |
| `AGENT_VERIFY_ENABLED` / `AGENT_VERIFY_COMMAND` | `true` / 空 | 自动验证；命令留空则按清单识别 |
| `AGENT_CONTEXT_MAX_CHARS` / `_KEEP_RECENT` / `_TOOL_CHARS` | `60000` / `12` / `1500` | 上下文裁剪；max=0 关闭 |
| `AGENT_MAX_PLAN_STEPS` / `_TOOL_ROUNDS` / `_REPAIR_ROUNDS` | `5` / `12` / `3` | 循环上限 |

状态文件位置（宿主侧与沙箱侧是两处，别混淆）见
[USAGE.md](docs/USAGE.md)。
