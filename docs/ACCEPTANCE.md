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

## 3. 自检清单

代码冻结前跑一遍，全部应当通过。

```bash
# 1. 静态检查
ruff check .

# 2. 全量测试（含真实 WSL 沙箱；LLM 用例需 API Key）
pytest                       # 期望 749 passed（不含 LLM 标记的用例）

# 3. 环境自检
agent doctor                 # 期望「全部通过」

# 4. 端到端冒烟（无需 API Key 的部分）
agent sandbox-init
pytest -m wsl -q             # 沙箱链路

# 5. 场景演示（需要 API Key，会真实调用模型）
python scripts/demo.py --list
python scripts/demo.py --only 1
```

当前状态（2026-10-04，缺陷收口后）：

| 项 | 结果 |
|---|---|
| 测试 | **749 passed**（不含 LLM 用例），WSL 集成 172，无失败 |
| 覆盖率 | **89%**（`pytest -m "not llm" --cov`，含真实 WSL） |
| lint | 干净（`ruff check src tests`） |
| 快反馈 | `pytest -m "not wsl and not llm"` = **577 passed**，约 40 秒 |
| CI | `.github/workflows/ci.yml`：`pip install -e ".[dev]"` → ruff → `pytest -m "not wsl and not llm"` |

## 4. 已知限制

按"会不会影响答辩结论"排序。前三条是**有意的取舍**，后三条是**待办**。

### 有意为之

**Web 端固定拒绝变更类命令。**
没有做审批交互。与其让 L2/L3 命令悬着等一个永远不会来的答复，不如明确拒绝
并在页面上说清楚。`web/app.py::WEB_APPROVAL_MODE` 有测试锁着，
防止以后被改成 `ask` 把页面卡死。

**长期记忆只支持显式增删，不做 LLM 自动提炼。**
自动记忆容易积累噪声，也难以回答"这条结论是哪来的"。
在一个主打可审计的系统里，这个交换不划算。

**内存上限默认关闭。**
`ulimit -v` 默认 0（不限）。JVM / Node / 编译器会索取远超实际使用的
虚拟地址空间，贸然开启会把正常构建打死。CPU 时间、文件大小、进程数都有限制。

**经 shell 的修改不产生快照。**
文件修改的正道是 `file_*` 工具（精确替换 + diff + 写前备份）。模型若用 shell
改文件（`sed -i` 等，属 L2、需确认），会置 `dirty` 触发验证，但**不生成快照、
不发 `FileChanged`** —— shell 输出无法结构化解析。「可回滚的修改流程」只覆盖文件工具路径。

### 待办

**思考模式的 `reasoning_content` 未修。**
`deepseek-v4-flash` 偶发 400。W12 查清了根因（langchain-openai 不提取该字段，
且序列化时不透传任意 `additional_kwargs`），但**捕获侧无法稳定复现**，
所以没有把无法验证的改动放进请求路径。规避方式是 `DEEPSEEK_MODEL=deepseek-chat`。
详见 [PLAN.md §6.1](PLAN.md)。

**检索默认后端是 grep。**
沙箱里没装 ripgrep，自动退到 `grep`/`find`。两个后端输出格式一致（grep 分支显式 `-E`），
但大仓库上 grep 会更慢。`agent doctor` 会显示当前用的是哪个。

**上下文裁剪按字符数，不是 token。**
够用且不引入 tokenizer 依赖，但不如按 token 精确。

### 未验证

**TUI 在真实交互终端下的视觉效果。**
自动化测试用 Textual 无头驱动（44 个用例），覆盖了事件映射、计划面板、
审批弹窗、斜杠命令，但**配色、边框、滚动行为需要人工看一眼**。

**沙箱隔离：默认到 WSL 发行版级，可选用 bwrap 做内核级隔离。**
默认（`AGENT_SHELL_SANDBOX=off`）是非 root 用户 + 工作区限定 + 资源上限 +
**命令参数的词法越界升级**（只读命令引用 `~`/工作区外绝对路径/`..`、或含 `$()`/变量替换时
升级到人工确认）。若要**内核级**隔离，设 `AGENT_SHELL_SANDBOX=bwrap`：命令在挂载命名空间里执行，
`/home` `/root` `/mnt`（Windows 盘）不可见、仅工作区可写；配置了 bwrap 但沙箱内不可用时会
**拒绝执行**（fail closed），不静默降级。默认威胁模型仍是"防误操作与静默越权"；
完整的资产/假设/边界与"从 T1 升到 T2 还差什么"见 [THREAT_MODEL.md](THREAT_MODEL.md)。

## 5. 冻结

代码冻结后不再改动功能，只接受缺陷修复。任何改动都应：

1. 跑 `pytest`（687 个用例全过）
2. 跑 `ruff check .`
3. 如果是安全相关的改动，对照本文件第 2 节确认对应测试仍在
