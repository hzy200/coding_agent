# 基于 LangGraph 的终端原生编程智能体 — 开发方案

> 开发环境：Windows 10/11 + WSL2（Ubuntu 发行版作为执行沙箱）
> 模型：DeepSeek V4（OpenAI 兼容协议）
> 追踪：LangSmith

---

## 1. 总体架构

### 1.1 设计原则：宿主内置工具、模型只输出意图

LLM **不直接连接操作系统**。模型只能输出结构化工具调用 `{tool, args}`，全部由宿主程序校验后执行：

- Shell 类操作 → 宿主通过 `subprocess` 调用 `wsl.exe -d <distro> -- /bin/bash -l -s`，命令经 stdin 传入（规避 Windows 命令行转义问题）
- 文件修改 → 走专用工具（精确 `old_string → new_string` 替换 / unified diff），不经过 shell，保证路径安全、修改精确、可回滚

### 1.2 分层

| 层 | 职责 | 关键实现 |
|---|---|---|
| CLI 表现层 | 流式输出、审批交互、斜杠命令 | `typer` + `rich` |
| 编排层 | 任务规划、状态机、human-in-the-loop | LangGraph `StateGraph` + `interrupt` |
| 能力层 | 模型可调用的结构化工具 | LangChain `BaseTool`，宿主侧实现 |
| 安全层 | 命令分级审批、路径守卫、沙箱执行 | `sandbox/` 策略引擎 |
| 持久层 | checkpoint、审计日志、长期记忆 | `SqliteSaver` + JSONL |
| 可观测层 | 调用链追踪与调试 | LangSmith |

```
┌──────────── CLI (typer/rich) ────────────┐
│  流式渲染 · 审批交互 · /undo /diff /sessions │
└───────────────────┬──────────────────────┘
                    │
┌───────────────────▼──────────────────────┐
│        LangGraph 编排（StateGraph）        │
│ planner→retrieve→act→approval_gate→execute→verify→repair │
└───────────────────┬──────────────────────┘
                    │  {tool, args}
┌───────────────────▼──────────────────────┐
│  安全层 policy / pathguard / limits       │
└───────────────────┬──────────────────────┘
                    │
┌───────────────────▼──────────────────────┐
│  WSL2 沙箱（非 root · $HOME/agent-ws · 资源限制）│
│  wsl.exe -- /bin/bash -l -s   ← stdin 传脚本 │
└──────────────────────────────────────────┘
```

---

## 2. 技术栈

| 组件 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | LangGraph 兼容性最稳 |
| 编排 | `langgraph` | StateGraph + checkpointer + interrupt |
| LLM 接入 | `langchain-openai` | DeepSeek V4 为 OpenAI 兼容协议，改 `base_url` 即可 |
| 校验 | `pydantic` v2 | 工具入参强校验 |
| 配置 | `pydantic-settings` | `.env` + 环境变量 |
| CLI | `typer` + `rich` | 流式渲染 |
| 追踪 | `langsmith` | 调用链、数据集、评估 |
| 持久化 | SQLite | checkpoint + 长期记忆 |
| 审计 | JSONL | 追加写，便于回溯 |
| 沙箱 | WSL2 独立发行版 | 非 root 用户，工作区限定 `$HOME/agent-ws`（自动推导） |
| 测试 | `pytest` | 单元 + 集成 + 冒烟 |
| 最小 Web | `fastapi` + SSE | 仅验证流式对话 / 多会话 / API 联通 |

> WSL2 与 Windows 路径映射：`D:\proj` ↔ `/mnt/d/proj`，由 `sandbox/pathguard.py` 统一转换。

---

## 3. 目录结构

```
coding_agent/
  pyproject.toml
  .env.example
  README.md
  docs/  PLAN.md  ARCHITECTURE.md  USAGE.md  DEMO.md  ACCEPTANCE.md
  src/coding_agent/
    runtime.py        # AgentRuntime：唯一编排入口，产出领域事件
    events.py         # 领域事件（前端契约）
    config.py         # pydantic-settings 配置
    diffing.py        # unified diff 生成与统计
    messages.py       # 消息取文本
    cli/              # app.py：typer 入口（doctor/run/tui/web/…）
    tui/              # app.py：Textual 界面
    web/              # app.py + static/index.html：最小验证界面
    graph/
      state.py  build.py  routing.py
      nodes/  planner.py act.py approve.py tools.py
              verify.py repair.py advance.py respond.py
    tools/    shell.py files.py git.py deps.py
              search.py testrun.py artifacts.py registry.py
    sandbox/  wsl_exec.py policy.py pathguard.py
              fs.py snapshots.py limits.py
    memory/   checkpointer.py sessions.py longterm.py
    audit/    logger.py models.py
    llm/      deepseek.py prompts.py context.py
  tests/  unit/  integration/  tui/
  scripts/  demo.py
```

---

## 4. 核心模块设计

### 4.1 图状态与节点

```python
class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    plan: list[str]            # 任务分解
    step_idx: int
    cwd: str
    last_result: ToolResult    # stdout / stderr / exit_code
    retry: int                 # 修复循环计数
    approvals: list[dict]
    snapshot_id: str | None
```

节点链路：

```
START → planner → retrieve → act ⇄ tools
                                ↓
                            verify ──成功──→ respond → END
                                │
                             失败 ↓
                             repair → act （上限 3 轮，超限上报用户）
```

- **planner**：自然语言 → 有序子任务（严格 JSON）
- **retrieve**：`ripgrep` + Python `ast` 建轻量符号索引，按需裁剪上下文
- **act**：模型输出 `{tool, args}`，经 pydantic 校验
- **approval_gate**：按策略分级，L2/L3 触发 `interrupt()` 挂起
- **verify**：执行编译 / 测试，解析 exit code + stderr，生成结构化错误摘要
- **repair**：失败驱动循环，错误摘要回灌，上限 3 次
- **respond**：生成最终答复与 unified diff

### 4.2 命令分级策略

| 级别 | 示例 | 行为 |
|---|---|---|
| L0 只读 | `ls` `cat` `grep` `find` `git status/log/diff` | 自动执行 |
| L1 低风险写 | `mkdir` `touch` `sed`(非 -i)、`git add` | 自动执行 + 审计 |
| L2 变更性 | 文件写、`cp` `ln` `mv`、`git commit`、`pip/npm install`、`sed -i` | **需确认** |
| L3 危险 | `rm -rf`、`git push -f`、`git reset --hard`、`curl\|sh`、`sudo`、`chmod 777 /`、`dd if=` | **拒绝或强确认** |

判定**不依赖 LLM**：白名单命令 + 危险正则黑名单 + 参数检查；复合命令（`&&` `||` `;` `|`）按段分类取最高级别；**不可解析的一律降级为 L2**。

`cp` / `ln` / `mv` 刻意**不**放在自动放行的 L1：它们能覆盖或替换文件，会绕过文件工具的精确替换、diff 与写前备份，让「可回滚的修改流程」出现旁路。

### 4.3 审批：决定与执行分离

```
act ──(有 tool_calls)──→ approval_gate ──→ tools ──→ act
                              │
                         interrupt() 挂起
                              ↓
                    前端收集答复 → Command(resume=...)
```

- **approval_gate 只做决定**：按 `SessionPolicy` 把风险等级翻译成 放行 / 询问 / 拒绝；
  需要询问时 `interrupt()` 挂起，图状态存进 checkpoint，前端拿 `ApprovalRequested` 事件。
- **tools 节点负责强制**：它是所有工具执行的唯一咽喉，没拿到 `auto` / `approved` 的调用
  一律不执行，只回一条说明给模型。分开的好处是判定逻辑保持纯函数，执行侧的检查无法绕过。
- **fail closed**：审批记录缺失、前端答复无法解析、`--yes` 之外的非交互环境，一律按拒绝处理。
- **一次性**：审批结果由 tools 节点消费后即清空，不会跨轮次误放行。

注意：`interrupt()` 恢复时节点会**从头重跑**，所以 gate 里除 `interrupt()` 外必须保持纯函数。

### 4.4 验证：失败要看得见

`verify` 节点在**每一步做完后**跑一遍项目的测试/构建，把输出结构化成
`文件:行号 + 消息` 再回灌，而不是丢一大坨 stdout 给模型自己找。

两个刻意的取舍：

- **只在改过东西时才验证**（状态里的 `dirty`）。只读探索跑测试纯属浪费。
- **自动验证不走审批**。它跑的是宿主从项目清单推导出的固定命令，模型影响不了跑什么；
  模型主动调用 `run_tests` 才走 L2 审批。区别在「谁决定执行什么」。

命令按清单自动识别：`Cargo.toml` → cargo、`go.mod` → go、`package.json` 带 test 脚本 →
npm、`pytest.ini`/`tests/` + pytest 可用 → pytest、`Makefile` 有 `test:` → make。
都识别不出就返回 `not_configured`，**不当成失败**。也可用 `AGENT_VERIFY_COMMAND` 覆盖。

解析按输出格式分派：Python traceback（unittest）→ 定点报错（mypy/gcc，兼收 pytest 的
`FAILED` 摘要）→ 断言细节兜底。**验证失败不改变路由**——W10 的 repair 循环才会在这里接管，
当前是把结构化错误写进状态、事件与审计，由 respond 如实报告。

### 4.5 检索与上下文

**检索走结构化工具**（`search_code` / `find_files`），不是让模型继续拼 `grep -rn … | head`：

- 结果逐条解析成「文件:行号:内容」，能报「命中 N 条，显示前 M 条」，
  模型不必自己数 head 截到哪。
- 模式、glob、路径逐参数传递，没有引号事故。
- 顺带消掉一个误伤：`find … | xargs grep …` 会被分级判成 L2 而拦下，
  有了专用工具就不必给 `xargs` 开白名单。

**双后端**：沙箱里没有 ripgrep 时退到 `grep` / `find`。值钱的是结构化接口，
不是具体哪个二进制。两个后端必须输出同一套格式 —— 因此 grep 分支**显式加 `-E`**：
grep 默认是 BRE，`(` `)` 是字面量，同一个模式在 rg 下和在 grep 下含义会不同，
双后端就失去意义了。grep 不读 `.gitignore`，还要显式排除
`.git` / `.venv` / `node_modules` 等目录，否则在有虚拟环境的仓库里会扫到天荒地老。

**上下文裁剪**（`llm/context.py`）：长会话的体积主要来自工具结果，一次 `file_read`
就可能几千字符。裁剪只发生在**发给模型的历史**上，不改写会话状态。

唯一的不变量：**只截断内容，绝不丢弃消息**。丢消息会破坏
`tool_calls` ↔ `tool_call_id` 配对，下一轮 API 直接报错 —— 这是 W2 踩过并专门立过
不变量的坑。截断内容不动结构，配对永远完整。超预算时先降到 `tool_chars`，
仍超再降到 300 字符的硬上限。

### 4.6 沙箱资源限制

两层，都在沙箱内生效：

- **`ulimit`**：CPU 时间（默认 600s）、单文件大小（512MB）、进程数（1024）。
  内存（`ulimit -v`）默认关闭 —— JVM / Node / 编译器会索取远超实际使用的虚拟地址空间，
  贸然开启会把正常构建打死。单项设置失败不影响整条命令。
- **沙箱内 `timeout`**：外层 `proc.kill()` 只能杀掉 `wsl.exe`，Linux 侧子进程未必跟着走。
  在沙箱里再套一层 `timeout --kill-after=5`，超时就能确定性清理；退出码 124 表示超时。
  外层 subprocess 超时只用 `shell_timeout + 10s` 兜底。

### 4.7 结构化工具：为什么 git 与依赖不走 shell

模型完全可以用 `shell_exec` 拼 `git commit -m "..."`，但结构化工具换来三件事：

- **参数不能逃逸**：提交信息、路径由宿主逐参数 `shlex.quote`；依赖包名走白名单正则
  （拒绝 `-` 开头、空格、`;`、`|`、反引号等），模型无法借参数构造第二条命令。
- **等级可判定**：`git add` 只动索引 → L1；`git commit` / 安装依赖 → L2 询问。
  不再依赖对整条命令字符串做正则猜测。
- **审计可读**：日志里是 `git_commit` + `{"message": ...}`，而不是一串原始命令。

另有一条纪律：**`.agent/`（agent 自己的备份目录）不许进版本控制**。
`git_add` 拒绝暂存它，`git_commit` 在提交前检查暂存区，用 shell `git add -A`
绕过也拦得住 —— 否则模型一个手滑就把 agent 的备份提交进用户仓库。

### 4.8 文件工具与回滚

- 编辑采用 `old_string → new_string` 精确替换或 unified diff，不走 shell
- `pathguard`：词法归一化后必须落在 workspace root 内，拒绝 `..` 逃逸与越界绝对路径

**快照布局**：`<工作区>/.agent/backups/<snapshot_id>/<相对路径>`，
`snapshot_id` 形如 `20261003T024018-a1b2c3` —— 时间戳前缀让目录名天然按时间排序，
所以**不需要额外索引文件**，列目录就能找到某个文件的历史版本。

**恢复语义是「先留底再覆盖」**：回滚前把当前内容也存一份，
因此**回滚本身也可回滚**，一次误回滚不会把用户当下的改动直接抹掉。
新建文件不留底（没有「改之前」可言，否则回滚会变成删文件）。

入口有三处，共用同一个 `SnapshotStore`：
模型侧 `file_restore` 工具（可自纠错）、用户侧 `agent undo/diff/snapshots`
与 TUI 的 `/undo` `/diff` `/snapshots`。回滚记录以 `rollback` 类型写入审计。

### 4.9 会话与长期记忆

**会话列表不另建存储**：审计日志里已有 `run_start`（thread_id / 工作区 / 用户提问）
与逐条 `tool_call`，`SessionIndex` 直接据此归纳出「有哪些会话、聊了什么、动了多少工具」。
再维护一张会话表就是重复状态，还得处理两边不一致。代价是审计关闭时列不出会话 —— 可接受的降级。

**长期记忆**落在工作区内的 `.agent/memory.md`（纯文本、一行一条、可手工编辑）。
放工作区而不是宿主目录，因为事实是关于**项目**的。

刻意做成**显式增删**，不做 LLM 自动提炼：自动记忆既容易积累噪声，
又难以回答"这条结论是哪来的"。原型阶段宁可少而准。

注入点是 `act` 的系统提示，每次 `run` 从文件重读 —— 会话中途 `/remember`
加的事实能立刻生效，且不写回消息历史。

### 4.10 事件层与前端解耦

前端（CLI / TUI / Web）**不直接驱动图，也不持有工具**，只消费 `AgentRuntime` 产出的领域事件：

```
AgentRuntime（图 + 沙箱 + 安全策略）
        │  AsyncIterator[Event]
        ├──→ cli/   Rich 渲染
        ├──→ tui/   Textual
        └──→ web/   SSE（Event 是 pydantic 模型，model_dump 即数据帧）
```

事件：`PlanCreated` `StepStarted` `StepFinished` `AssistantToken`
`ToolCallStarted` `ToolCallFinished` `ApprovalRequested`(W5) `RunFinished` `RunFailed`。

两条硬约束：

1. **前端不得导入 `coding_agent.tools` 或 `coding_agent.sandbox`**，否则会出现"Web 端绕过审批"的旁路。
2. **工具除文本外必须产出结构化 artifact**，事件层与审计层据此渲染与记账，绝不解析工具的文本输出。
   实测 `response_format="content_and_artifact"` 在 `tool.invoke()` 下会丢弃 artifact
   （该语义仅在 `ToolNode` 内生效，而 `ToolNode` 在 langgraph 1.2.x 下无法脱离图调用），
   因此改用 `artifacts.pack/unpack` 显式封装。

### 4.11 多会话与审计

**checkpoint**：`AsyncSqliteSaver` 落盘到 `<cwd>/.agent/checkpoints.sqlite`，
`--thread-id` 复用同一会话即可跨进程恢复。必须用 Async 版 —— 同步的 `SqliteSaver`
方法名齐全但 `aget/aput` 全是抛 `NotImplementedError` 的占位。

**审计**：由 runtime 而非前端负责写入，因此任何前端都绕不过。
按天一个 JSONL 文件，条目复用工具 artifact，不解析工具文本输出。
写入失败会抛 `AuditError` 并转成 `RunFailed` —— 审计有缺口是这个项目不能接受的
失败模式，宁可显式失败也不静默丢记录。

关键字段：`ts / kind / thread_id / call_id / tool / args / level / decision / ok /
exit_code / duration_ms / path / snapshot_id`。`args` 落库前统一截断，
避免 `file_write` 的 content 把日志撑爆。



会话隔离靠 LangGraph checkpointer 的 `thread_id`；长期记忆存项目画像 / 约定 / 历史决策，新会话自动注入。

审计条目格式：

```json
{"ts":"2026-10-02T10:00:00Z","thread_id":"...","tool":"shell","args":{"command":"ls -la"},
 "level":0,"decision":"auto","exit_code":0,"duration_ms":42,"snapshot_id":null}
```

---

## 5. 四个月开发计划（16 周）

### M1（W1–W4）骨架与只读闭环
| 周 | 任务 | 产出 |
|---|---|---|
| W1 | 环境搭建：WSL2 发行版、venv、pyproject、DeepSeek 客户端、LangSmith 接入、流式 CLI 壳 | `agent doctor` 自检通过 |
| W2 | `StateGraph` + `AgentState` + act/tools 节点，打通单轮只读 shell 调用 | `agent run "..."` 可执行只读命令 |
| W3 | 文件工具（read/write/edit）+ pathguard + diff 生成 | 精确单文件编辑 |
| W4 | L0/L1 策略 + 审计日志 + `SqliteSaver` checkpoint | 会话可持久化 |

🎯 **里程碑 Demo**：自然语言问答 + 精确单文件编辑

### M2（W5–W8）执行、审批、回滚
| 周 | 任务 |
|---|---|
| W5 | L2/L3 审批 `interrupt` + CLI 确认交互 |
| W6 | Git 工具、依赖管理（pip/uv/npm）、超时与资源限制 |
| W7 | 备份 / 回滚机制、`/undo` `/diff` |
| W8 | 多会话记忆（`/sessions`、长期记忆雏形） |

🎯 **里程碑 Demo**：改代码 + 装依赖 + 一键回滚

### M3（W9–W12）修复循环与验证
| 周 | 任务 |
|---|---|
| W9 | verify 节点：编译 / 测试执行 + 错误结构化解析 |
| W10 | repair 循环（失败驱动命令修复，含重试上限与上报） |
| W11 | 仓库检索增强（ripgrep 索引 + 上下文裁剪） |
| W12 | LangSmith 链路追踪与 prompt 调优 |

🎯 **里程碑 Demo**：给定 bug 全自动修复闭环

### M4（W13–W16）打磨与交付
| 周 | 任务 |
|---|---|
| W13 | 单元 / 集成测试补全 + 边界与异常处理 |
| W14 | 最小 Web 验证（SSE 流式对话、多会话、API 联通） |
| W15 | 典型场景演示脚本 + README / 架构图 / 使用文档 |
| W16 | 缓冲周：缺陷修复、演示录制、材料整理、代码冻结 |

---

## 6. 风险与应对

| 风险 | 应对 |
|---|---|
| DeepSeek 结构化输出不稳定 | 强制 JSON Schema + pydantic 校验 + 失败自动重试 |
| 危险命令绕过 | 判定权在宿主策略层；不可解析一律 L2；沙箱内非 root + 资源限制 |
| WSL 与 Windows 路径错配 | 统一路径转换层，禁止跨边界绝对路径；`wsl.exe` 默认继承 Windows cwd，故始终显式 `cd` 到工作区 |
| Windows 控制台 GBK 编码 | CLI 启动时把 stdout/stderr 强制重配置为 UTF-8 |
| 上下文超限 | 检索裁剪 + 历史摘要压缩 |
| 回滚不彻底 | 所有写操作统一走 snapshot 抽象，禁止旁路 |
| 修复循环不收敛 | 硬性重试上限 + 失败原因分类上报 |
| 思考模式下 API 报 `reasoning_content must be passed back` | 见 §6.1 |

| WSL 命令行转义踩坑 | 命令经 stdin 传给 `bash -l -s`，不走 argv |

### 6.1 已知问题：思考模式的 `reasoning_content`

在 `DEEPSEEK_MODEL=deepseek-v4-flash` 下偶发：

```
400 - The `reasoning_content` in the thinking mode must be passed back to the API.
```

W12 查清了成因，结论是**当前不修，只规避**。

**根因（已确认，非推测）**：`langchain_openai/chat_models/base.py` 的模块文档
明确写着 `reasoning_content` / `reasoning_details` **不被提取**；响应的
`additional_kwargs` 里实测只有 `refusal`。而序列化函数
`_convert_message_to_dict` 对 assistant 消息**只透传** `name` / `tool_calls` /
`function_call` / `audio`，任意 `additional_kwargs` 不会进请求体。
所以即使把 `reasoning_content` 塞进 `additional_kwargs` 也**传不回去**。

**为什么不在这一轮修**：真正的修复要同时改两侧的私有方法 ——
`_get_request_payload`（回传）与 `_create_chat_result` /
`_convert_delta_to_message_chunk`（捕获）。而**捕获侧无法验证**：
几轮尝试都不能稳定触发思考模式（单轮简单/复杂提示、流式多轮工具循环都不触发），
只在「多轮工具调用 + 校验失败 + repair 重入 act」时遇到过一次。
把无法验证的改动放进**请求路径**，风险是所有正常运行都可能被改坏，
这比容忍一个偶发错误更糟。

**当前处理**：
- 以 `RunFailed` 事件显式报出（含原始 API 文本），不崩栈；审计留 `run_error` 记录。
- **规避方式：`DEEPSEEK_MODEL=deepseek-chat`**（也是配置默认值），同一场景实测正常。

**真要修的话**：需要在项目里维护一个 `ChatOpenAI` 子类，重写上述私有方法；
私有 API 会随版本变动，且必须先有稳定的复现手段才能验证。

---

## 7. 验收指标

| 指标 | 目标 |
|---|---|
| 单轮工具调用成功率 | ≥ 95% |
| 典型任务端到端成功率 | ≥ 80% |
| 平均修复轮次 | ≤ 2 |
| 危险命令拦截率 | 100% |
| 危险命令误伤率 | < 5% |
| 回滚正确率 | 100% |
| 可观测性 | LangSmith 全链路可追溯、审计日志无缺口 |

---

## 8. 进度

| 周 | 状态 | 说明 |
|---|---|---|
| W1 | ✅ 完成 | 脚手架、DeepSeek 客户端、WSL 沙箱、命令分级、`act ⇄ tools` 内核、CLI |
| W2 | ✅ 完成 | `planner` / `advance` / `respond` 节点，计划注入系统提示，CLI 计划渲染 |
| W2.5 | ✅ 完成 | 事件层重构：`AgentRuntime` + `events.py` + 工具 artifact，CLI 改为消费事件流 |
| W2.6 | ✅ 完成 | TUI 骨架：计划面板、工具时间线、流式输出、斜杠命令、架构约束测试 |
| W3 | ✅ 完成 | 文件工具（read/write/edit）、符号链接守卫、unified diff、写前备份、FileChanged 事件 |
| W4 | ✅ 完成 | 审计日志（JSONL）、AsyncSqliteSaver 持久化 checkpoint、事件 call_id 配对、`agent audit` / TUI `/audit` |
| W5 | ✅ 完成 | `approval_gate` + `interrupt()` 挂起/恢复、`SessionPolicy`、CLI 确认、TUI `ModalScreen` |
| W6 | ✅ 完成 | Git 工具（status/diff/log/add/commit）、依赖管理（uv/poetry/pnpm/yarn/npm/pip）、ulimit 资源上限与沙箱内超时 |
| W7 | ✅ 完成 | `SnapshotStore` 快照存储、`file_restore` 工具、`/undo` `/diff` `/snapshots`（CLI 子命令 + TUI 斜杠命令） |
| W8 | ✅ 完成 | `SessionIndex` 会话列表（从审计归纳）、`LongTermMemory` 项目记忆、`/sessions` `/switch` `/memory` `/remember` |
| W9 | ✅ 完成 | `verify` 节点、测试命令自动识别、输出结构化为「位置 + 消息」、`Verification` 事件与审计 |
| W10 | ✅ 完成 | `repair` 节点、失败驱动修复循环、硬性重试上限与上报、`RepairStarted` 事件 |
| W11 | ✅ 完成 | `search_code` / `find_files` 检索工具（rg 优先、grep 兜底）、上下文裁剪 |
| W12 | ✅ 完成 | 追踪元数据与 doctor 连通性检查、prompt 调优（去 markdown、压步骤小结、最终答复分界） |
| W13 | ✅ 完成 | 覆盖率 75%→89%（补 runtime 事件流、CLI 渲染与子命令、planner/respond）；顺带修出 3 个真实缺陷 |
| W14 | ✅ 完成 | Web 最小验证（SSE 流式 / 多会话 / 联通测试，零构建链）、TUI 打磨、`scripts/demo.py` 四场景演示 |
| W15 | ✅ 完成 | 文档拆分：`ARCHITECTURE.md`（分层/数据流/不变量/扩展点）、`USAGE.md`（工作流/配置/故障排查）、`DEMO.md`（讲稿） |
| W16 | ✅ 完成 | 冻结前审计（删死字段、清理过时注释）、需求对照表 `ACCEPTANCE.md`、代码冻结 |
| 冻结后 | 🚧 进行中 | 多轮缺陷收口：冻结审计的 P0/P1、[BUG_AUDIT_2](BUG_AUDIT_2.md) 的 C1–C8、2026-10-06 的 P0×3（见下）。阶段 A 计划见 [PHASE_A.md](PHASE_A.md) |

### 冻结后记录：缺陷收口（P0 / P1 + 阶段 A）

一次完整技术与安全审查后，按"会不会影响正确性/安全/答辩结论"排序修复了一遍。
计划与逐项状态见 [PHASE_A.md](PHASE_A.md)。

**P0（可静默越权/丢审计，优先修）**

1. shell 只读命令没有文件系统边界：`cat ~/.ssh/id_rsa`、`find / -name '*.key'` 曾被当 L0
   自动放行。改为对自动放行命令做越界升级（`$()`/变量、`~`/绝对路径/`..`、`find -exec`）→ L2 人工确认。
2. 审批恢复后审计丢失工具调用参数：配对表原先是 `_stream` 局部变量，挂起/恢复跨了两次 `_stream`。
   提升为 runtime 实例级、按 `thread_id` 分桶。
3. 悬空符号链接可写到工作区外：`-e` 对悬空链接为假，跳过了 realpath 校验。判定改为 `-e || -L`。

**P1（可靠性/可用性）**

4. `recursion_limit` 偏小（按每轮 2 个超步算，实际 3 个）→ 系数改 `3*rounds+3`。
5. 危险模式原始全文匹配误伤（`grep "rm -rf"` 判 L3）→ 引号感知：屏蔽字面量，保留 `$()` 与 `sh -c` 内容。
6. `budget_exhausted` 不参与路由，空转步骤被静默跳过 → 预算耗尽且无改动时停下如实上报。
7. 审批 `call_id` 空/重复时串号 → 整批 fail-closed。
8. `recursion_limit` 又偏小：第 4 条只算了一个执行周期，但 `repair` 会把 `tool_rounds` 清零、
   让每轮修复**重新吃满一个完整周期** —— 一步反复失败且每轮烧光预算时会以 `GraphRecursionError`
   收场，拿不到「试过但没修好」的收尾。改为 `(修复次数+1)×周期 + 修复次数 + advance`，
   并补图级回归用例（`test_repair.py::test_maxed_out_repair_loop_fits_the_estimated_limit`）。

**阶段 A（缺陷收口）**：A1 大文件读取上限在读前生效（100MB：6.68s→0.40s，并修出"空文件读不了"）；
A2 `history()` 解耦建图（无 API Key 可用）；A3 上下文裁剪支持 content blocks；
A4 shell 变更也置 `dirty`（触发验证）；A5 工程卫生（124 消歧、删空 `main.py`、
`Makefile` + CI、`RunStarted` 入契约、`.coverage` 出库）；A7 快照 id 提升到微秒+序号（同秒留底排序稳定）。

**阶段 B（安全纵深）**：

- **B3** 词法器边界测试矩阵（16 例）：`$'...'`、嵌套 `$()`、反引号、`sh -c` 包装都验证到位。
- **B2** `.agent` 在 shell / git 路径的防护：`git add -A`/`.`（整树暂存）与任何 `.agent` 引用 → L2。
- **B1** 可选内核级隔离 `AGENT_SHELL_SANDBOX=bwrap`：挂载命名空间里 `/home` `/root` `/mnt`
  不可见、仅工作区可写；配置了 bwrap 却不可用时**拒绝执行**（fail closed）。
  `agent doctor` 增探测项。
- **B4** 威胁模型归档 [THREAT_MODEL.md](THREAT_MODEL.md)：明确默认交付是"防误操作"（T1），
  逐层给出升到"防主动越狱"（T2）的前置条件与代价。

### 2026-10-06 记录：第三轮 P0 收口（从测试系统体检入手）

这一轮的入口不是读代码，而是**先量测试系统本身**。当时全量跑分是
`1043 passed / 5 failed / 1 error`，而文档写的是"992 passed，无失败" —— 追这 5 个失败
的过程中挖出一个一直存在、又被 CI 结构性地挡在视野外的功能缺陷。

**1. 写文件超过约 96 KB 必然失败**（功能，直指创新点 4「可回滚的修改流程」）

`limits.wrap_with_limits` 把整个脚本体作为**单个 argv** 传给
`timeout … bash -c '<body>'`，撞上内核 `MAX_ARG_STRLEN`（128 KiB）→
`Argument list too long`（exit 126）。而 `fs.write_text` 会把整份内容 base64 后内联进
脚本（膨胀 4/3），于是 96 KB 成了实际写入天花板 —— 与此同时 `max_file_read_bytes`
宣称 2 MB、`MAX_OPERATIONAL_BYTES` 宣称 10 MB，快照回滚同样写不回去。
改为 `bash -s` + heredoc、脚本体经 **stdin** 传递。实测 96 KB / 256 KB / 2 MB / 5 MB
写入与 1.5 MB 快照回滚全部通过。

**2. 命令分级可被 `$'…'` / `~user` 绕过**（安全，直指创新点 2）

`_HIDDEN_EXPANSION_RE` 不认 ANSI-C 与本地化引号，`_mask_quoted` 又把它们整段当字面量
屏蔽，shlex 词元退化成 `$/etc/passwd`（不以 `/` 开头）→ `cat $'/etc/passwd'`、
`cat $'\x2fetc\x2fpasswd'`、`cat ~root/.ssh/id_rsa` 全被判 **L0 自动放行**。

修复**刻意没有采用"出现 `$'` 就升级"的一刀切**：那会让 `echo $'rm -rf /'` 这种完全
惰性的字面量也进人工确认，把防线变成噪声（既有测试正是钉住这一点的）。判据改为
**展开后的值是否像工作区外路径**（`policy.py::_ansi_quoted_path`）；转义写法无法便宜
还原，按隐藏处理。`~user` 一并收进 `_is_external_path_token`。

**3. 自动 verify 的命令可被模型决定**（安全，绕过整条审批链）

`testrun._detect` 读工作区里的 `Makefile` / `package.json` 来决定自动 verify 跑什么，
而这两个文件模型用 `file_write` 就写得到（`--write` 下 L1 自动放行）——
「写个恶意 manifest → 下一次脏写自动执行」是一条**不过审批的任意 shell 执行路径**。
`verify.py` 与 `ARCHITECTURE.md` 里"模型影响不了跑什么"的论断由此被证伪（两处已改正）。

改为自动 verify 传 `allow_manifest=False`，只接受**命令文本由宿主写死**的
`cargo test` / `go test` / `pytest`；`make test` / `npm test` 留给走审批的 `run_tests`
—— 能力没丢，只是回到审批后面。探测缓存键带上该标志：两种口径答案不同，共用一个键
会让审批口径的结果漏进无人审批的 verify。

**顺带修掉的测试基建元缺陷**（第 1 条能藏这么久的直接原因）

`tests/unit/test_snapshots.py` 与 `tests/integration/test_snapshots.py` 同名，而 tests/
下没有 `__init__.py`，pytest 报 `import file mismatch`，**unit 那 7 条用例从未被执行**
（该文件当时还没进 git）。加 `tests/{unit,integration}/__init__.py` 让模块名带上目录前缀。
教训：**"跑绿"与"跑过"是两件事** —— 同名文件冲突的失败形态是静默跳过，不是变红，
而 CI 只跑 `-m "not wsl and not llm"`，沙箱层的问题它结构性看不见。

收口后：`pytest -m "not llm"` = **1070 passed / 0 failed**、覆盖率 **90.9%**。

### 2026-10-06 记录：评测尺子修复 + review 节点

需求侧的六阶段对照（理解需求与规划 → 代码生成与编辑 → **自动化代码审查** →
自动化验证 → 迭代修复 → 交付与合并）里，**「自动化代码审查」是唯一完全缺失的一环**：
`verify` 只跑测试命令，`ruff`/`mypy`/`tsc` 类问题零检出（IMPROVEMENT_PLAN P1-4），
而「测试通过 ≠ 代码正确」的第二道关也不存在（P1-6）。这一轮补上它，同时先修了
度量它的那把尺子。

**一、评测尺子（先修，否则改了什么都看不出来）**

三处缺陷，都不是"数字不够高"而是"数字说不清"：

1. **没有机制指标**。只有通过率一个维度，于是不知道 verify / repair / replan
   到底有没有被触发过。README 里记着一次真实误读：`--ab` 跑出「开 3/3 对 关 2/3」
   看着像重规划有效，实际 `replans=0`（节点一次都没执行），那 33 个百分点是噪声。
   现补 `mechanism` 块：`verifications`（**status → 次数**，`not_configured` 与 `ok`
   的差别正是"验证脊柱有没有生效"的判据）、`repairs`、`replans`、token。
2. **token 口径有系统性偏差**。`input/output_tokens` 原本是 `_stream` 的局部变量，
   挂起、中断、`RunFailed` 路径都会丢 —— 而失败的那一轮往往花费最多，按审计汇总
   会让成本只由成功任务贡献。改为累计进 `progress`（跨挂起-恢复不丢）并由
   `RunFinished`/`RunFailed` 带出，评测读事件而非审计。
3. **脏数字能冒充基线**。`baseline.json` 当时是 `git_dirty=true`；`--only debug`
   会把 10 个任务的子集覆盖写到全量 baseline 上。现加可比性守卫：子集 / 脏工作区 /
   沙箱无 pytest 时**在开跑之前**拒绝（不是烧完十分钟额度之后），需 `--force` 或
   `--baseline <路径>` 绕过，且写入 `comparable: false` + 原因。

另加 `--repeat k` 量化噪声地板：29 个任务翻一个是 3.4 个百分点，比它小的"提升"
在单次运行里不可分辨。报告观测到的逐次波动与**翻转过的任务清单**（两次运行通过率
可以相同而成员不同，只看通过率会得出"没有变化"）。措辞上刻意不写置信区间 ——
重复 2~3 次没有那种统计效力。

**二、`review` 节点（自动化代码审查）**

位置在 `verify` **通过之后**、`advance` 之前 —— 两道关串联，任一关没过都进
`repair` 重试。沙箱里**没有** ruff/mypy/node/tsc（实测），所以主干是零依赖的确定性
检查，外部 linter 探到才用、探不到就如实说明，不假装审过。

- **改动前从哪来**：直接复用 `SnapshotStore` 的留底（不变量 8），不另建基线。
  只审**新增行**。
- **水位线**（`review_watermark`）：只看 `snapshot_id > watermark` 的留底，
  **跨步骤保留、只由 planner 归零**。这是最容易写错的地方 —— 留底记的是写前内容，
  所以改过的文件永远与自己的留底不同，水位线一旦逐步清零，每一步都会重审前面
  所有步骤的改动，同一个早已处理过的问题反复告警到把修复预算耗光。
- **阻断 vs 告警**：语法坏、测试被改弱（新增跳过标记/`assert True`）、新增
  `breakpoint()`/`pdb.set_trace()`、写进 `.agent/` → 阻断；新增 `print(`/`TODO`、
  linter 输出 → 只告警。刻意**不做**「公共符号被删」规则：`rename_function` 这类
  任务本就要求旧名字彻底消失，该规则会与评测任务直接冲突。
- **共用修复预算**，不新开计数器：两个独立计数器会让 `estimate_recursion_limit`
  多一层乘积，而它历史上算歪过三次。收敛性由 replan 的硬上限保证。

过程中改出两个真缺陷（都由测试逮到）：

1. `route_after_verify` 若**也**看 review，会因为 `repair` 刻意不清 `review`
   （act 要靠它知道该改什么）而把"验证失败→修复→验证通过"这条路上**陈旧的阻断**
   直接打回 repair —— **审查再也不重跑**。改为每一跳只看自己那道关。
2. `replan` 只认 `verification` 失败，于是 review 的阻断会落进「步进微调」入口
   （那条路允许返回"不改"），控制流回到 act 空转到额度耗尽。改为 `blocking_failure`
   同时认两种阻断。

**三、结果**

`pytest -m "not llm"` = **1124 passed / 0 failed**（较本轮开工前 1070 增 54）。

> 未做：评测集的**动态范围扩容**（几十文件的仓库、几十步长程、模糊需求）。
> 当前 29 个任务最大也只有 9 文件 / 68 行，`tool_calls` 中位数约 7（预算 60），
> 「自主性提升了多少」仍然测不出来 —— 那是 IMPROVEMENT_PLAN §3.2 W2 的量级工程。
> 另：新基线必须在干净工作区上跑，并带上 `review_enabled` 戳（开关两态的机制
> 指标不可比）。

### W15 记录：文档拆分的取舍

README 之前是八周里逐段追加出来的，涨到 400 行什么都讲。这周按**读者意图**拆成三份：

| 文档 | 读者想问的问题 |
|---|---|
| `README.md` | 这是什么、怎么跑起来、能做什么 |
| `ARCHITECTURE.md` | 怎么设计的、为什么这么设计、我想加东西该改哪 |
| `USAGE.md` | 某个具体事怎么做、出错了怎么办 |
| `DEMO.md` | 答辩时说什么、先演示什么 |

**最有价值的一节是 `ARCHITECTURE.md` 的「不变量」表** ——
九条全是从真实故障里踩出来并固化成测试的（tool_call 配对、审批一次性、
fail closed、审计失败显式报出……）。之前它们散落在各周的工作记录里，
新人（包括几个月后的自己）改动相关代码时不会知道。每条都标了对应测试名。

另外补了「扩展点」：加工具 / 加节点 / 加前端各三步，以及**容易漏的那一步** ——
加工具必须在 `approve.tool_level` 登记等级，否则按 L3 处理（故意的 fail closed）。

写完做了一轮链接校验：所有内部链接与锚点都存在（`docs/` 下 5 份文档）。

### W14 记录：Web 与演示脚本

**Web 是"最小验证"，不是第二套产品界面。** 只做三件原计划里写明的事：
SSE 流式对话、多会话列表、API 联通测试。刻意**不引前端构建链** ——
一个自包含的 HTML + 原生 JS，有测试守着（断言页面里没有外链脚本）。

**审批固定拒绝**（`WEB_APPROVAL_MODE = "deny"`）。Web 端没有做审批交互，
与其让 L2/L3 命令悬在那里等一个永远不会来的答复，不如明确拒绝、并在页面上说清楚。
需要审批能力用 TUI。这条有测试锁着，避免以后有人顺手改成 `ask` 把网页卡住。

Web 端点本身很薄：`AgentRuntime` 产出的领域事件本来就能 JSON 化，
`event.model_dump_json()` 直接就是一个 SSE 数据帧 —— **事件层解耦在这里第二次兑现**
（第一次是 TUI 无头测试）。

**演示脚本** `scripts/demo.py` 按四个创新点各安排一个场景，顺序递进：
只读 → 被拦 → 修复 → 回滚。`--list` 只看说明，`--only N` 跑单个。

写它的时候踩到两个老坑，都是 `wsl.exe` 的：

- 输出编码随环境在 UTF-16LE / UTF-8 间摆动，直接按 UTF-8 解会留下 NUL，
  拿去当路径就是 `embedded null character` → 复用产品里的 `decode_wsl_output`
- WSL 自己的诊断（localhost 代理提示）走 stderr，混进 stdout 后
  「取最后一行当路径」会取到警告文案 → 只在失败时合并 stderr

顺带发现 `wslpath -w /tmp/...` 给出的是 UNC 路径，`-C` 认不了；
改成把演示工作区放在 Windows 临时目录下，用 `win_to_wsl` 正推，不碰 `wslpath`。

### W13 记录：测试补全查出来的问题

覆盖率是量出来的，不是猜的：先跑 `--cov` 定位缺口，再补。
`runtime.py`（中枢，43%）与 `cli/app.py`（主界面，**0%**）占了未覆盖行的 71%。

用**注入假图**的方式测 `AgentRuntime._stream`：喂给它与真实图同构的
`(mode, data)` 序列，就能不碰 LLM、不碰沙箱地断言事件翻译与审计。
CLI 则用记录型 Console + CliRunner。

**补测试过程中发现并修掉的真实缺陷：**

1. **`run_start` 的审计写入在 `try` 之外** —— 审计失败会直接抛给调用方，
   而 README 承诺的是"写入失败会转成 `RunFailed`"。也就是说**这条承诺只对循环内的写入成立**。
   顺带把 except 里的审计改成尽力而为：它自己再失败一次会把真正的错误盖掉。
2. **`[ui]` 被 rich 当成样式标记吞掉** —— 缺依赖时的提示
   `pip install -e ".[ui]"` 显示成 `pip install -e "."`，用户照着敲会失败。
3. **回滚目标解析失败时没有写审计** —— 用户以为回滚了但没回滚，审计里一片空白。
   与"审计日志无缺口"的验收指标直接冲突。

### 测试执行速度

初测：**全套 599 秒**（10 分钟），单个集成用例 10–100 秒。三个原因，都已修：

| 问题 | 处理 | 效果 |
|---|---|---|
| 全是串行的 `wsl.exe` 进程启动 | `pytest-xdist`，默认 `-n auto` | 599s → 180s |
| **读一个文件要 3 次进程启动**（`resolve`+`stat`+`base64` 各一趟） | 合成一次调用；`resolve`+`stat` 合并为 `probe`；写入的 realpath 校验移进 shell 脚本 | 180s → 113s |
| 用例在真的 `pip install` / `npm install`（走网络）；langsmith 重试退避把一条用例拖到 50 秒 | 只验证"选了哪个包管理器"而不执行安装；让 Client 直接抛异常 | 病态用例消失 |

读路径的 3→1 不只是测试收益：**每次 `file_read` / `file_edit` 都少两次进程启动**，
这是产品层面的性能改善。

两种跑法：

```bash
pytest -m "not wsl"   # 快反馈：单元 + UI，约 46 秒
pytest                # 全量（含真实 WSL 沙箱），约 2 分钟
```

### W12 记录：追踪与调优

**LangSmith**：运行时给图挂 `run_name` / `tags` / `metadata`（thread_id、工作区、模型、
权限、审批模式），trace 因此可按会话与工作区筛选。元数据**只放标识与开关，
不放提示词或文件内容** —— 追踪数据会离开本机，有测试守着这条边界。

**一次真实的排查：403 其实是配置被静默覆盖**

现象：开启追踪后 ingestion 一直 403，`/sessions` 直连也是 403。一度判断为凭据失效。

重新排查时的关键判据是 **401 与 403 的区别**：

| 请求 | 结果 |
|---|---|
| 空 key | **401** `Invalid token` |
| 格式合法但不存在的 key | **403** `Forbidden` |
| 本机 key | **403** `Forbidden` |

403 是「token 解析不出租户」，即**钥匙不存在/已吊销**。但真正的原因不在钥匙本身：
`.env` 里已经写入了新的 key，而**系统环境变量里还留着旧的**
（前 12 位与长度都没变）。配置优先级是 `环境变量 > .env`，所以旧的把新的盖住了。

这个覆盖**完全静默**，用户改完 `.env` 发现不生效，无从定位。

**进一步排查发现删不掉**：这些变量不在 Windows 注册表的任何作用域
（HKCU 只有 7 项、HKLM 27 项，无一匹配），也不在 `.claude/settings.json`
或任何 shell 配置里。沿进程链往上追，源头是 **`Claude Code Haha.exe`**
在启动时注入进程环境 —— 用户既看不到来源也删不掉。

因此做了两个修改：

1. **优先级改为 `.env` > 环境变量**（偏离通用约定，`settings_customise_sources`
   重排）。项目目录里的 `.env` 是显式写下、看得见、可编辑的意图，让它优先。
   构造函数参数仍最高优先（测试与嵌入式用法依赖），`_env_file=None` 这类显式
   覆盖继续生效 —— 自定义 source 是**委托**给原 source 加一层空值过滤，
   而不是重建，否则会把这个覆盖无视掉。
   `.env` 里的空占位（`KEY=`）不算配置，不会盖掉环境变量。
2. **差异仍然上报**：`shadowed_env_keys()` 比对两侧，`agent doctor` 用 WARN 列出
   「环境变量里也有、但当前以 `.env` 为准」的键，避免反向困惑。

修正后实测追踪链路完全正常：

```
run: 'agent:c04ed4f1'  status=success  type=chain
metadata: {"thread_id": "c04ed4f1", "workspace": "/mnt/c/.../agent-w11",
           "model": "deepseek-chat", "allow_write": false, "approval_mode": "ask"}
```

`agent doctor` 同时保留连通性检查：开启追踪但凭据确实不可用时**提前报 FAIL**，
而不是让 langsmith 在每次调用后往 stderr 打一串 ingestion 失败让用户去猜。

**prompt 调优**（依据观察到的真实输出，不是凭感觉改）：

1. **去掉 Markdown**：终端不渲染，`**加粗**` 与 `# 标题` 只显示成多余符号。
2. **压步骤小结**：原先 act 每步收尾都写成完整报告，与 respond 的最终答复大量重叠，
   短任务的输出长度几乎翻倍。现在明确要求步骤小结只交代本步做了什么。
3. **最终答复分界**：act 与 respond 内容重叠是架构固有的（每步小结 + 最终汇总），
   靠提示词消除不掉。改为在界面上打 `── 最终答复 ──` 分界 ——
   读者因此知道那是汇总结论，而不是重复输出。

### UI 排期（已确认：TUI 为主，Web 最小化，答辩演示级）

| 时点 | 内容 |
|---|---|
| W3 之前 | ✅ 事件层与 artifact 重构（已完成） |
| W3 之前 | ✅ TUI 骨架（已完成，含架构约束测试） |
| W4 | 审计日志复用 artifact，TUI 加审计视图 |
| W5 | 审批 `ModalScreen`（与 `interrupt` 同步落地） |
| W7 | `/undo` `/diff` 可视化 |
| W13–W14 | TUI 打磨（会话管理、演示脚本、视觉细节）+ Web 最小版 |

### 遗留项

**转 W4 策略完善**

- `xargs` 未在白名单，导致 `find … | xargs grep …` 这类只读管道被误判为 L2。
  需按 `xargs` 的目标命令判定，否则影响「误伤率 < 5%」指标。
- `python3` 同样不在白名单 → 被判定 L2。这一条判定本身合理（执行任意代码），
  但 W9 的 verify 节点需要跑测试，届时应以专用工具放行，而不是放宽 shell 白名单。

**转 W7 回滚流程**

- 备份已经在写，位于 `<工作区>/.agent/backups/<snapshot_id>/`，
  每次编辑独立快照（已实测两次连续编辑形成两条快照链）。
  还缺的是 `/undo` 与 `/diff` 的恢复入口。

## 9. 快速开始

```bash
conda activate agent              # 项目当前使用的 conda 环境（也可用 venv 替代）
pip install -e ".[dev]"
cp .env.example .env             # 填入 DEEPSEEK_API_KEY
agent doctor                     # 环境自检
agent chat                       # 流式对话（API 联通测试）
agent run "看看当前目录有哪些文件"  # 走 LangGraph 图执行
pytest -q
```
