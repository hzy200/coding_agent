# 使用指南

命令清单见 [README](../README.md#命令一览)。这份文档讲**怎么用**与**出问题怎么办**。

## 安装

```bash
python -m venv .venv
.venv/Scripts/activate            # macOS/Linux 用 source .venv/bin/activate
pip install -e ".[dev,ui,web]"    # ui=TUI，web=Web 最小版，dev=测试
cp .env.example .env              # 填入 DEEPSEEK_API_KEY
agent doctor                      # 环境自检（不需要 API Key）
agent sandbox-init                # 创建沙箱工作区
```

`doctor` 会检查：Python、依赖、API Key、WSL 发行版、沙箱用户是否非 root、
工作区是否存在、检索后端是 ripgrep 还是 grep。

## 权限模型

默认**只读**。要改文件必须显式开：

| 开关 | 放开什么 |
|---|---|
| （默认） | 只有 L0 只读命令与 `file_read` |
| `--write` | L1：`mkdir`/`touch`、`file_write`/`file_edit`/`file_restore`、`git_add` |
| `-y` / `--yes` | L2/L3 不再逐条询问，自动批准 |

L2/L3 默认会**停下来问**。不带 `-y` 时：

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
```

**回滚本身也可回滚**：恢复前会先给当前内容留底，再执行一次 `undo` 就滚回来了。

TUI 里对应 `/snapshots` `/diff` `/undo`。

### 接着上次继续

```bash
agent sessions                        # 列出历史会话
agent run --thread-id <id> "继续"
```

会话存在 SQLite 里，跨进程可恢复。TUI 里 `/sessions` `/switch <id>`。

### 记住项目约定

```bash
agent memory --add "这个仓库用 uv 管理依赖，不要用 pip"
```

写进 `<工作区>/.agent/memory.md`，每次运行注入系统提示。也可以直接编辑那个文件。
TUI 里 `/remember` `/memory` `/forget`。

### 查审计

```bash
agent audit                   # 最近 20 条
agent audit -n 100
agent audit --thread-id abc123
```

按天一个 JSONL 文件，默认在 `<当前目录>/.agent/audit/`。

记录类型：`run_start` / `plan` / `tool_call` / `file_change` / `verify` / `repair` /
`rollback` / `run_end` / `run_error`。每条复用工具的结构化产物，不解析工具文本输出；
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
详见 [PLAN.md](PLAN.md#6-风险与应对)。

### 测试太慢

```bash
pytest -m "not wsl"   # 跳过真实沙箱，约 45 秒
```

集成测试的开销几乎全在 `wsl.exe` 进程启动上。完整说明见
[PLAN.md](PLAN.md#测试执行速度)。

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

**路径安全**：词法归一化 + 沙箱内 `realpath` 双重校验。工作区里的符号链接
若指向外部，读写都会被拒。

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
