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
│ planner→retrieve→act→gate→execute→verify→repair │
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
  docs/PLAN.md
  src/coding_agent/
    runtime.py                                   # AgentRuntime：唯一编排入口
    events.py                                    # 领域事件（前端契约）
    cli/            app.py  render.py  slash.py
    tui/            app.py  widgets/             # W3 后接入
    graph/
      state.py                                     # AgentState
      build.py                                     # 编译 StateGraph
      routing.py                                   # 条件边
      nodes/  act.py  tools.py  planner.py
              retrieve.py  verify.py  repair.py
    tools/    shell.py files.py search.py git.py
              deps.py testrun.py registry.py
    tools/    shell.py files.py artifacts.py registry.py
              search.py git.py deps.py testrun.py
    sandbox/  wsl_exec.py policy.py pathguard.py fs.py limits.py
    memory/   checkpointer.py longterm.py
    audit/    logger.py models.py
    llm/      deepseek.py prompts.py
    config.py
  tests/  unit/  integration/  smoke/
  scripts/  demo_*.py
  web/      app.py                                 # 最小验证 UI
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

### 4.3 文件工具与回滚

- 编辑采用 `old_string → new_string` 精确替换或 unified diff，不走 shell
- `pathguard`：词法归一化后必须落在 workspace root 内，拒绝 `..` 逃逸与越界绝对路径
- 写盘前 snapshot 到 `.agent/backups/<snapshot_id>/`（或影子 git 提交）
- `/undo` 按 snapshot_id 还原，`/diff` 输出 unified diff

### 4.4 事件层与前端解耦

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

### 4.5 多会话与审计

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
| WSL 命令行转义踩坑 | 命令经 stdin 传给 `bash -l -s`，不走 argv |

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
| W5–W16 | ⏳ 待开始 | 见第 5 节 |

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
python -m venv .venv
.venv/Scripts/activate           # Windows
pip install -e ".[dev]"
cp .env.example .env             # 填入 DEEPSEEK_API_KEY
agent doctor                     # 环境自检
agent chat                       # 流式对话（API 联通测试）
agent run "看看当前目录有哪些文件"  # 走 LangGraph 图执行
pytest -q
```
