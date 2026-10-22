---
layer: L3
doc_id: usage
title: 使用指南
owner: 项目作者
status: active
last_reviewed: 2026-10-22
review_cadence: 命令或配置变动时
note: 场景化的「故障 → 处置步骤」版本见 docs/runbook.yaml。
---

# 使用指南

命令清单见 [README](../README.md#命令一览)。这份文档讲**怎么用**与**出问题怎么办**。

## 安装

### 环境

本项目当前使用 conda 环境 **`agent`**（Python 3.11）。环境已存在时激活即可：

```bash
conda activate agent
```

从零复现（新机器，或重建环境）：

```bash
conda create -n agent python=3.11 -y
conda activate agent
pip install -e ".[dev,ui,web]"     # 可编辑安装 + 各组依赖，见下表
```

`pip install` 的 extra 组按需取舍：

| extra | 装了它才能用 |
|---|---|
| （无） | `agent doctor` / `sandbox-init` / `chat` / `run` 等运行时命令 |
| `dev` | `pytest` / `ruff` / `pytest-xdist` / `pytest-cov` —— 跑测试与 lint |
| `ui` | `agent tui`（Textual） |
| `web` | `agent web`（FastAPI + uvicorn） |

> ⚠️ **必须先激活环境**：`agent` 命令装在环境自己的 `Scripts/` 下
> （conda 为 `D:\anaconda3\envs\agent\Scripts`），未激活时不在 PATH 上，
> 直接敲 `agent` 会「找不到命令」。

验证安装是否就绪：

```bash
python -c "import sys, sysconfig; print(sys.executable); print(sysconfig.get_path('scripts'))"
agent --help
```

> **用普通 venv 替代 conda 也可以**：`python -m venv .venv` 后激活
> （Windows `.venv/Scripts/activate`；macOS/Linux `source .venv/bin/activate`），
> 后续安装与运行命令完全一致。`scripts/demo.py` 会依次在当前环境 / PATH / `.venv`
> 中定位 `agent` 入口，两种布局都支持。

前置条件、**实测依赖版本快照**、复现/导出与常见问题见 [ENVIRONMENT.md](ENVIRONMENT.md)。

### 首次运行

```bash
cp .env.example .env              # 填入 DEEPSEEK_API_KEY
agent doctor                      # 环境自检（不需要 API Key）
agent sandbox-init                # 创建沙箱工作区
```

`doctor` 会检查：Python、依赖、API Key、WSL 发行版、沙箱用户是否非 root、
工作区是否存在、检索后端（ripgrep / grep）、shell 隔离后端（`off` / `bwrap`）。

## 权限模型

默认**只读**。要改文件必须显式开：

| 开关 | 放开什么 |
|---|---|
| （默认） | 只有 L0 只读命令与 `file_read` / `file_list` |
| `--write` | L1：`file_write`/`file_edit`/`file_edit_many`/`file_restore`/`file_move`/`file_copy`/`file_mkdir`、`git_add` |
| `-y` / `--yes` | L2 自动批准；**L3 危险命令仍逐条确认**（含 `file_delete` 的 L2 会被自动批准） |

L2/L3 默认会**停下来问**。`--yes` 只跳过 L2；L3（`rm -rf`、`git reset --hard`、
`sudo` 等）始终需要人工回答。不带 `-y` 时：

```
→ [L2 变更性] shell_exec: pip install requests

需要确认 (L2 变更性)
  pip install requests
  理由：安装依赖
  执行？ [y/N]
```

非交互环境（管道、CI）没有 stdin，一律**按拒绝处理**。这是有意的 fail closed。

## 常见工作流

### 只读分析一个仓库

```bash
agent run -C "D:\proj" "这个项目的命令安全策略分几级？每一级怎么判定？"
```

指向任意目录，只读，不改任何东西。

### 让它改代码

```bash
agent run --write -C "D:\proj" "把 config.py 里的超时从 30 改成 60，改完跑测试"
```

流程是：读文件 → 精确替换 → 自动跑验证 → 失败则带着结构化错误重试（上限 3 次）。
每次编辑都会留快照。

### 撤销它的改动

```bash
agent snapshots              # 看有哪些快照
agent diff                   # 最近一次改动变成了什么样
agent undo                   # 回滚最近一次
agent undo --path src/a.py   # 只回滚某个文件
agent undo --last-run --dry-run   # 先看最近一次运行改了哪些文件
agent undo --last-run             # 整体回滚最近一次运行
agent undo --since 2026-10-21T02:40:18+00:00   # 按时间点回滚
```

**回滚本身也可回滚**：恢复前会先给当前内容留底，再执行一次 `undo` 就滚回来了。

TUI 里对应 `/snapshots` `/diff` `/undo`。

### 接着上次继续

```bash
agent sessions                        # 列出历史会话
agent run --thread-id <id> "继续"
```

会话存在 SQLite 里，跨进程可恢复。TUI 里 `/sessions` `/switch <id>`。

需要试另一条路时，从历史 checkpoint 分叉，原会话不受影响：

```bash
agent checkpoints --thread-id <id>     # 看可选的 checkpoint
agent fork --thread-id <id>            # 从当前状态分叉（也可 -c <checkpoint>）
agent rewind --thread-id <id> -n 1     # 回退 1 个 checkpoint
```

回退是在旧 checkpoint 上新建分支，历史不会被删除，误操作还能找回。
TUI 里对应 `/fork` `/rewind [n]`。checkpoint 默认每个会话保留 200 个
（`AGENT_CHECKPOINT_KEEP`），超出后自动轮转。

### 记住项目约定

```bash
agent memory --add "这个仓库用 uv 管理依赖，不要用 pip"
```

写进 `<工作区>/.agent/memory.md`，每次运行按请求相关度注入 top-K 条。
也可以直接编辑那个文件；来源/时间/过期信息在 `.agent/memory.meta.json`。
写入会拒绝疑似密钥，相似的新事实会替换旧版本。模型在运行中可用
`memory_search` 检索，`--write` 会话可用 `memory_save` 写入带 TTL 的记忆。
TUI 里 `/remember` `/memory` `/forget`。

仓库里的 `AGENTS.md` / `CLAUDE.md` 会作为分层指令单独注入（最多 4 层），
与长期记忆分开维护。

### 查审计

```bash
agent audit                   # 最近 20 条
agent audit -n 100
agent audit --thread-id abc123
```

按天一个 JSONL 文件，默认在 `<当前目录>/.agent/audit/`。

记录类型：`run_start` / `plan` / `replan` / `tool_call` / `file_change` /
`verify` / `review` / `repair` / `nudge` / `subagent` / `workflow` /
`task_graph` / `worker` / `orchestration` / `steering` / `rollback` /
`run_end` / `run_error`。每条复用工具的结构化产物，不解析工具文本输出；
`args` 落库前统一截断，不会把整个文件内容抄进日志。

**审计由 runtime 写，不由前端写** —— 所以任何界面都绕不过。
写入失败会转成 `RunFailed`：宁可显式失败，也不静默丢记录。

### 追踪调用链

```bash
LANGSMITH_TRACING=true agent run "..."
```

trace 带 `run_name`（`agent:<会话id>`）、`tags`，以及
`thread_id` / `workspace` / `model` / `allow_write` / `approval_mode` 等 metadata，
因此可以按会话或工作区过滤。

**元数据只放标识与开关，不放提示词或文件内容** —— 追踪数据会离开本机。

实测落到 LangSmith 的样子：

```
run: 'agent:<会话id>'   status=success   type=chain
tags:     ["coding-agent", "mode:ask"]
metadata: {"thread_id": "…", "workspace": "/mnt/d/proj", "model": "deepseek-chat",
           "allow_write": false, "approval_mode": "ask"}
```

### 编排模式与实时干预

默认 `direct`。要启用动态工作流与并发 worker：

```bash
AGENT_ORCHESTRATOR_ENABLED=true agent tui
# 写 worker 还需要（工作区必须是干净 git 仓库根）：
AGENT_ORCHESTRATOR_ENABLED=true AGENT_ORCHESTRATOR_WRITE_ENABLED=true \
  agent tui --write
```

TUI 运行中可输入 `/pause` `/resume` `/cancel` `/add [角色] <目标>` `/replan`；
CLI 与 Web 只透传事件，不提供控制入口。任务图、worker 成本/并发/冲突与
干预延迟的评测方法见 [tests/eval/README.md](../tests/eval/README.md)。

## 配置来源

优先级：**命令行/构造函数参数 > `.env` > 环境变量 > 默认值**。

> ⚠️ 这里**刻意偏离**了「环境变量 > .env」的通用约定。

原因是一个真实的坑：某些工具链会在启动时往进程环境注入变量，用户既看不到来源
（不在注册表、也不在配置文件里）也删不掉 —— 结果改 `.env` 完全不生效，
表现为「明明更新了 API key 却一直 403」。

`agent doctor` 会把两边不一致的键列出来：

```
WARN  环境变量被 .env 覆盖 — LANGSMITH_API_KEY 在环境变量里也有，
      但优先级是「.env > 环境变量」，当前生效的是 .env 里那份
```

`.env` 里留空的占位（`KEY=`）不算配置，不会盖掉环境变量 ——
从 `.env.example` 复制来的空行不会误伤。

## 故障排查

### `DEEPSEEK_API_KEY` 已配好但仍报未配置

见上面的「配置来源」。跑 `agent doctor` 看有没有 WARN，
或者确认你改的是**当前目录**的 `.env`。

### LangSmith 一直 403

按顺序查：

1. `agent doctor` —— 追踪凭据可用性会提前报出来
2. 真实 key 与格式合法但不存在的 key 都返回 **403**，空 key 返回 **401**。
   403 说明服务端解析不出租户，即**钥匙不存在/已吊销**
3. 换了 key 仍 403？多半是旧值还在环境变量里盖着新的（见「配置来源」）

追踪开关在**进程启动时**读取并缓存（langsmith 内部用了 `lru_cache`），
运行中改 `LANGSMITH_*` 环境变量不生效，要重启进程。

### 追踪没有任何数据，也没报错

追踪可能是关闭的。`LANGSMITH_TRACING=true` 才会开启。
注意默认值是关闭 —— 追踪数据会离开本机，所以不做默认开启。

### 所有命令都被判「工作目录非法」

工作区根目录写错了。`agent doctor` 会打印实际解析出的工作区。
也可以显式指定：`agent run -C "D:\proj" "..."`。

注意 `wsl.exe` 会自动继承 Windows 的当前目录，所以工具始终显式 `cd` 到工作区。

### 检索很慢

`agent doctor` 会显示当前后端。没装 ripgrep 时退到 `grep`，
大仓库会慢一些（grep 不读 `.gitignore`，我们显式排除了 `.git`/`.venv`/`node_modules` 等）。

### `find … | xargs grep …` 被拦了

`xargs` 不在 L0 白名单，会降级为 L2。改用 `search_code` 工具，
它返回结构化的「文件:行号:内容」，也不会因为管道被拦。

### 模型报 `reasoning_content must be passed back`

`deepseek-v4-flash` 的思考模式偶发。langchain-openai 不提取该字段，传不回去。
**改用 `DEEPSEEK_MODEL=deepseek-chat`**（也是配置默认值）。
当前状态见 [ACCEPTANCE.md 已知限制](ACCEPTANCE.md#4-已知限制)。

### 测试太慢

```bash
pytest -m "not wsl and not llm"   # 跳过真实沙箱与真实模型，约 1 分钟
```

集成测试的开销几乎全在 `wsl.exe` 进程启动上。分层与守卫见
[WORKFLOW.md §3](WORKFLOW.md#3-开发工作流)。

## 沙箱

默认工作区是沙箱内的 `$HOME/agent-ws`（`agent sandbox-init` 会创建并打印实际路径）。
可以指向别处：

```bash
agent run -C "D:\proj" "..."      # Windows 路径写法
agent run -C /mnt/d/proj "..."    # WSL 路径写法
```

或写进 `.env` 的 `AGENT_WSL_WORKSPACE`。

**资源上限**（在沙箱内生效）：CPU 时间 600s、单文件 512MB、进程数 1024。
内存默认不限 —— JVM / Node 会索取远超实际使用的虚拟地址空间。
每次命令还有墙钟上限 `AGENT_SHELL_TIMEOUT`（默认 60s）。

**可选隔离** `AGENT_SHELL_SANDBOX`（默认 `off`）：设为 `bwrap` 时命令在挂载命名空间里执行，
`/home`、`/root`、`/mnt`（Windows 盘）不可见，仅工作区可写。需要沙箱内装好 bubblewrap
（`sudo apt install bubblewrap`）；**配了却不可用时会拒绝执行**（fail closed），不静默降级。
`agent doctor` 会报当前隔离后端是否可用。资产、假设与"从防误操作升到防越狱还差什么"见
[THREAT_MODEL.md](THREAT_MODEL.md)。

**路径安全**：词法归一化 + 沙箱内 `realpath` 双重校验。工作区里的符号链接
若指向外部（含**悬空链接**）读写都会被拒。

**环境隔离**：每条 WSL 命令都以 `env -i` 启动，宿主环境变量（CI token、云凭据、
`LANGSMITH_API_KEY` 等）不会进入 Linux 子进程；`HOME` 由脚本内 `getent passwd`
补回。需要构建工具变量（`JAVA_HOME`/`GOPATH`/`VIRTUAL_ENV` 等）时，用
`AGENT_SHELL_ENV_ALLOWLIST` 显式列出名称；含 KEY/TOKEN/SECRET/PASSWORD 等
名称即使写进去也会被忽略。`printenv` / `env` 被归为 L3，即使 `approve`
模式也必须人工确认。

**输出脱敏**：shell / search / file_read 的结果在回灌模型前会识别并替换常见
凭据（OpenAI/GitHub/AWS/JWT/Slack/Google token、私钥块、`password=...`、
数据库连接串）。这是模式匹配的 best-effort，不是完整的 DLP。

**秘密文件**：默认拒绝读取 `.env`、私钥/证书、`.npmrc`、`.netrc`、
`.aws/**`、`.kube/**`、`credentials*.json`、`*.tfstate` 等；可用
`AGENT_READ_DENY_GLOBS` / `AGENT_WRITE_DENY_GLOBS` 追加。shell 中
`cat .env*` 这类通配形状、以及检索密码关键词也会升级为 L2。

## 会话状态存放在哪

宿主侧（相对命令所在目录）与沙箱侧（相对工作区）是两处，别混淆：

| 位置 | 内容 |
|---|---|
| `<当前目录>/.agent/checkpoints.sqlite` | 会话 checkpoint |
| `<当前目录>/.agent/audit/*.jsonl` | 审计日志 |
| `<工作区>/.agent/backups/<快照id>/` | 文件快照 |
| `<工作区>/.agent/memory.md` | 项目长期记忆 |

`.agent/` 已在 `.gitignore` 里，而且 **git 工具会拒绝把它纳入版本控制**
（`git_add` 拒绝暂存、`git_commit` 提交前检查暂存区）——
否则模型一个 `git add -A` 就把 agent 自己的备份提交进你的仓库了。

## 安全 profile

`AGENT_SECURITY_PROFILE` 有三档：

| profile | 隔离 | 网络 | 审批 | 写入 |
|---|---|---|---|---|
| `local`（默认） | `AGENT_SHELL_SANDBOX`（默认 off） | 默认开 | `ask` | 由 `--write` 决定 |
| `untrusted` | 强制 bwrap | 强制关 | 强制 ask | 由 `--write` 决定 |
| `production` / `ops` | 强制 bwrap | 强制关 | 强制 `deny` | **强制关闭** |

`production/ops` 是只读审计档：`agent run --write`、`--yes` 以及 Web 的审批模式
都不会放宽它，L2/L3 一律拒绝。适合"只排查、不改动"的生产环境。

Web 会话固定只读：`/api/run` 不接受写权限，所有写操作请用本机 CLI/TUI。

## 公网 / 多用户部署

Web 默认按**不可信仓库**运行：`untrusted` profile（bwrap + 禁网 + 强制 ask +
不信任仓库指令/记忆），并且只监听 `127.0.0.1`。

1. 配置 Bearer Token（推荐用文件，权限 0600）：

```bash
AGENT_WEB_AUTH_TOKENS_FILE=/etc/coding-agent/tokens.json
# 文件内容：{"token-a": "alice", "token-b": "bob"}
# 也支持 "sha256:<hex>" 作为 key，避免保存明文 token
```

2. 预置每个用户的工作区（管理员操作）：

```bash
AGENT_WEB_WORKSPACES_ROOT=/srv/agent/workspaces   # WSL 路径
AGENT_WEB_DATA_ROOT=/var/lib/coding-agent/web     # 宿主路径
AGENT_WEB_MEMORY_ROOT=/srv/agent/memory           # WSL 路径
# 用户目录：/srv/agent/workspaces/alice、/srv/agent/memory/alice
```

Web **不接受用户指定任意主机路径**，用户只能在自己目录里工作。

3. 启动：

```bash
# 本机开发（loopback 匿名）
agent web

# 公网（TLS 由本进程终止）
agent web --host 0.0.0.0 --allow-remote \
  --ssl-certfile /etc/ssl/agent.crt --ssl-keyfile /etc/ssl/agent.key

# 公网（TLS 由反向代理终止）
agent web --host 127.0.0.1 --behind-proxy
```

非 loopback 且没有 token 会被拒绝；没有 TLS 且未声明 `--behind-proxy` /
`--allow-insecure-remote` 也会被拒绝。同一数据目录只允许一个 Web 进程
（单进程共享 governor）；不要使用 `uvicorn --workers > 1`。

4. 审计校验：

```bash
agent audit --verify            # 校验当天片段
agent audit --path <file> --verify
```

哈希链能发现本地日志被改动，但不能防拥有主机权限的人重写整条链；
高保证场景应外发到 WORM/远程日志。

5. 不可信仓库的已知边界：

- shell 的递归检索仍是词法 best-effort：检索密码/凭据关键词会升级为 L2，
  但变换写法仍可能绕过；请优先使用 `search_code`（会过滤秘密文件并脱敏输出）。
- 文件内容级 secrets 识别是模式匹配；自定义格式的凭据仍需要人工避免读取。
- `AGENTS.md`/`CLAUDE.md` 与仓库内记忆在 untrusted 下不注入。
- `approve` 模式仍然等于完全信任模型；不可信场景必须使用 `ask`。
- bwrap 不可用时 untrusted 模式拒绝执行；先 `agent doctor` 确认。
