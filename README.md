# CodingAgent

基于 LangGraph 的终端原生编程智能体。架构核心是**宿主内置工具、模型只输出意图**：
LLM 不直接接触操作系统，只产出结构化工具调用，由宿主校验后在 WSL2 沙箱里执行。

完整开发方案与四个月计划见 [docs/PLAN.md](docs/PLAN.md)。

## 当前进度

- **M1-W1** 项目骨架：配置层、DeepSeek 客户端、WSL2 沙箱、命令分级策略、`act ⇄ tools` 执行内核、流式 CLI
- **M1-W2** 任务规划：`planner` 拆分计划、`advance` 步进、`respond` 收尾，计划上下文注入系统提示
- **M1-W2.5** 事件层：`AgentRuntime` 统一编排、`events.py` 领域事件、工具 artifact，CLI 改为消费事件流
- **M1-W2.6** TUI 骨架：Textual 界面（计划面板 + 工具时间线 + 流式输出 + 斜杠命令）
- **M1-W3** 文件工具：`file_read` / `file_write` / `file_edit`，符号链接守卫、unified diff、写前备份
- **M1-W4** 审计与持久化：JSONL 审计日志、SQLite checkpoint 跨进程会话恢复、`agent audit` / TUI `/audit`

## 架构：前端只消费事件

```
AgentRuntime（图 + 沙箱 + 安全策略）
        │  AsyncIterator[Event]
        ├──→ cli/   Rich 渲染
        ├──→ tui/   Textual（计划中）
        └──→ web/   SSE（Event 是 pydantic 模型，model_dump 即数据帧）
```

前端拿不到工具与沙箱，因此**无法绕过命令分级审批**。工具除文本外还产出结构化 artifact，
事件层与审计层据此渲染与记账，不解析工具的文本输出。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env            # 填入 DEEPSEEK_API_KEY

agent doctor                    # 环境自检（不需要 API Key）
agent sandbox-init              # 创建沙箱工作区目录
agent chat                      # 流式对话，验证 API 联通

agent run "看看当前工作区有哪些文件"
agent run -C "D:\proj" "这个项目的安全策略分几级？"   # 指向任意仓库（只读）
agent run --write "在 src 下建一个 utils 目录"

agent run --thread-id mytask "接着上次继续"   # 复用会话（SQLite checkpoint）

agent audit                     # 查看审计日志
agent audit --thread-id mytask -n 50

agent tui                       # 终端界面（需 pip install -e ".[ui]"）
agent tui -C "D:\proj"

pytest -q
```

TUI 快捷键：`Ctrl+Q` 退出 · `Ctrl+N` 新会话 · `Ctrl+L` 清屏。
斜杠命令：`/help` `/new` `/clear` `/workspace` `/quit`。

## 目录

```
src/coding_agent/
  cli/        终端入口（doctor / chat / run）
  graph/      LangGraph 状态与节点
  tools/      模型可调用的工具
  sandbox/    WSL 执行、命令分级、路径守卫
  llm/        DeepSeek 客户端与提示词
```

## 安全模型

| 级别 | 行为 |
|---|---|
| L0 只读 | 自动执行 |
| L1 低风险写 | 自动执行 + 审计 |
| L2 变更性 | 需用户确认（W5 接入） |
| L3 危险 | 拒绝 |

判定完全在宿主侧完成，不依赖模型自我申报；复合命令按段取最高级别，无法解析一律降级为 L2。

`cp` / `ln` / `mv` 刻意不自动放行：它们能覆盖或替换文件，会绕过文件工具的精确替换、
diff 与写前备份。

## 文件修改

代码修改只走 `file_read` / `file_write` / `file_edit`（默认关闭写入，用 `--write` 打开）：

- **精确替换**：`file_edit` 要求 `old_string` 在文件中唯一。出现 0 次或多次都拒绝执行并
  说明原因，让模型补充上下文，而不是猜。
- **路径双重校验**：词法归一化 + 沙箱内 `realpath`。工作区里的符号链接若指向外部，
  读写都会被拒绝。
- **写前备份**：覆盖或编辑前把原文件存入 `<工作区>/.agent/backups/<snapshot_id>/`，
  为回滚留下依据（`/undo` 入口在 W7）。

内容经 base64 在沙箱内落盘，不经过命令行解释，不受引号与换行影响。

## 审计与持久化

- **审计日志**由 `AgentRuntime` 写入（不是前端），因此 CLI / TUI / Web 都绕不过。
  按天一个 JSONL 文件，默认在 `<cwd>/.agent/audit/`。每条记录复用工具 artifact，
  不解析工具文本输出；`args` 落库前统一截断，不会把整个文件内容抄进日志。
  写入失败会转成 `RunFailed` —— 宁可显式失败也不静默丢记录。
- **checkpoint** 用 SQLite 落盘，`--thread-id` 复用会话即可跨进程恢复上下文。
