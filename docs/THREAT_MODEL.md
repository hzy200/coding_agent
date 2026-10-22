---
layer: L2
doc_id: threat-model
title: 威胁模型
owner: 项目作者
status: active
last_reviewed: 2026-10-22
review_cadence: 安全相关代码变动时（sandbox/ · approve.py · 审批语义 · 路径守卫）
---

# 威胁模型

> 面向实现：说清**保护什么、假设对手是谁、现在挡得住什么、要挡住更强的对手还差什么**。
> 结论一句话：**默认交付是"防误操作"（T1），不是"防主动越狱"（T2）**；T2 需要额外一层内核级约束。

## 1. 资产（要保护什么）

| 资产 | 在哪 | 为什么值得保护 |
|---|---|---|
| 工作区外的用户文件 | WSL `$HOME`（`~/.ssh`、`~/.aws`、shell 历史…） | 私钥/凭据泄漏 |
| 宿主 Windows 文件 | `/mnt/c`、`/mnt/d` | 整台机器的数据 |
| 宿主进程环境 | 宿主的 `DEEPSEEK_API_KEY` 等 | 凭据泄漏、API 额度 |
| 工作区里的项目 | WSL 工作区 | 被误改/删 |
| 审计与快照 | 宿主 `.agent/audit`；工作区 `.agent/backups` | 可追溯性与可回滚 |

**已核实（2026-10-04）**：

- 宿主进程的 `DEEPSEEK_API_KEY` **不会**进入 WSL 登录 shell（`wsl.exe … bash -lc 'echo $DEEPSEEK_API_KEY'` → 空）。
- **审计与 checkpoint 落在宿主侧**（`<宿主 cwd>/.agent/`），不是 WSL 工作区 —— 沙箱内命令改不到。
- **快照 `.agent/backups` 在工作区内**（WSL），因此工作区可写时，agent 能改动/删除自己的留底。

## 2. 信任边界与攻击面

```
模型（不可信输出）
   │  {tool, args}            ← 唯一入口，宿主解析
   ▼
宿主策略（policy / approve / tools）  ← 判定权在这里，模型自报不算数
   │  经 stdin 传给 WSL
   ▼
WSL2 登录 bash → （可选）bwrap 挂载命名空间 → 命令
   │
   ▼
Linux 用户空间（非 root）
```

攻击面：shell 命令、文件工具路径、git/deps 参数、审批交互、前端 SSE/HTML、checkpoint/审计文件。

## 3. 两个威胁模型

### T1　防误操作（**默认交付目标**）

假设：模型**善意但会犯错**，或被**普通**（非对抗）提示误导；不主动设计逃逸。

这是默认配置（`AGENT_SHELL_SANDBOX=off`）的定位：把"手滑、越界、危险命令"挡住或交人工确认。

**验证入口**：[`tests/unit/test_policy.py`](../tests/unit/test_policy.py)、
[`tests/unit/test_approve.py`](../tests/unit/test_approve.py)。

### T2　防主动越狱（**对抗性，需要 bwrap**）

假设：模型输出（含被读取的文件内容）**主动尝试**越过工作区、读取凭据、外传数据。

默认 `AGENT_SHELL_SANDBOX=off` 不满足 T2；`untrusted` profile 强制 bwrap +
禁网后，能挡住大部分用户空间越界与网络外传，但**不防内核逃逸或恶意 WSL
发行版**，也不防拥有宿主权限的管理员。

**验证入口**：[`tests/integration/test_bwrap.py`](../tests/integration/test_bwrap.py)、
[`tests/unit/test_security_profile.py`](../tests/unit/test_security_profile.py)、
[`tests/unit/test_bwrap_args.py`](../tests/unit/test_bwrap_args.py)。

### T3　多用户 / 公网（本次新增）

假设：多个互不信任的用户通过公网访问同一 Web 服务，仓库来源不可信。

已实现的控制：

- Bearer Token 认证；未配置 token 只允许 loopback 匿名；
- per-user workspace / audit / checkpoint / steering / memory；
- registry 按 `(user, thread)` 隔离，跨用户访问返回 404；
- 非 loopback 必须有 token；远程无 TLS 默认拒绝；
- 请求体上限、按用户限流、runtime 数量上限、安全响应头；
- `untrusted` profile（bwrap + 禁网 + ask + 不信任仓库指令/记忆）；
- 秘密文件读保护 + 工具日志脱敏 + 审计哈希链；
- 单 Web 进程锁，避免多进程绕过配额。

残余风险：宿主管理员、WSL/内核漏洞、恶意 WSL 发行版、绕开关键词的递归检索、
以及审计链可被有主机权限者整体重写。

**验证入口**：[`tests/unit/test_tenancy.py`](../tests/unit/test_tenancy.py)、
[`tests/unit/test_web_auth.py`](../tests/unit/test_web_auth.py)、
[`tests/unit/test_web_app.py`](../tests/unit/test_web_app.py)。

## 4. 现有防线分级

| 防线 | 实现 | 归属 | 回归测试 |
|---|---|---|---|
| 命令四级分级 + 复合取最高 | `sandbox/policy.py` | T1 | [`test_policy.py`](../tests/unit/test_policy.py) |
| 危险模式拦截（`rm -rf`、`sh -c` 包装、管道执行等） | `policy.DANGER_PATTERNS` | T1（**已知模式的枚举**，非完备） | [`test_policy.py`](../tests/unit/test_policy.py) |
| 未知命令/解析失败 fail-closed 为需确认 | `policy` | T1 | [`test_policy.py`](../tests/unit/test_policy.py) |
| 只读命令**参数越界升级**（`~`、外部绝对路径、`..`、`$()`/变量） | `policy._escalate_auto_command` | T1（词法 best-effort） | [`test_policy.py`](../tests/unit/test_policy.py) |
| 引号感知（字面量不误判，`$()`/`sh -c` 照扫） | `policy._mask_quoted` | T1 | [`test_policy.py`](../tests/unit/test_policy.py) |
| `.agent` 在 shell/git 路径的防护 | `policy`（整树 `git add`、`.agent` 引用 → L2） | T1 | [`test_policy.py`](../tests/unit/test_policy.py) |
| 路径双重校验（词法 + realpath，含悬空链接） | `pathguard` + `fs` | **接近 T2**（对文件工具路径是硬约束） | [`test_pathguard.py`](../tests/unit/test_pathguard.py)、[`test_fs_guard.py`](../tests/unit/test_fs_guard.py)、[`test_file_tools.py`](../tests/integration/test_file_tools.py) |
| 审批在节点、执行在 `tools` 唯一咽喉 | `graph/nodes/{approve,tools}.py` | **接近 T2**（前端绕不过） | [`test_approve.py`](../tests/unit/test_approve.py)、[`test_nodes.py`](../tests/unit/test_nodes.py) |
| 资源上限（CPU/文件/进程，沙箱内 `ulimit`+`timeout`） | `sandbox/limits.py` | T1 | [`test_limits.py`](../tests/unit/test_limits.py)、[`test_sandbox_timeout_cleanup.py`](../tests/unit/test_sandbox_timeout_cleanup.py) |
| 审计（宿主侧、追加写、失败显式报出） | `audit/` | **接近 T2**（沙箱改不到） | [`test_audit.py`](../tests/unit/test_audit.py)、[`test_audit_chain.py`](../tests/unit/test_audit_chain.py) |
| 可选内核级隔离 `AGENT_SHELL_SANDBOX=bwrap` | `sandbox/wsl_exec.py` | **T2 的一层**（opt-in，默认 off） | [`test_bwrap_args.py`](../tests/unit/test_bwrap_args.py)、[`test_bwrap.py`](../tests/integration/test_bwrap.py) |
| 前端只消费事件、HTML 用 `textContent` | `tui/` `web/` | 接近 T2（无 XSS、无旁路执行） | [`test_web_app.py`](../tests/unit/test_web_app.py)、[`test_web_auth.py`](../tests/unit/test_web_auth.py)、[`test_cli_render.py`](../tests/unit/test_cli_render.py) |
| 编排写 worker：独立 worktree + `write_scope` + 合并前快照 | `orchestrator/implement.py`、`nodes.py` | T1（仍是 `--write` 预授权下的受控写路径） | [`test_write_scope.py`](../tests/unit/test_write_scope.py)、[`test_atomic_merge.py`](../tests/unit/test_atomic_merge.py)、[`test_implement_worker.py`](../tests/integration/test_implement_worker.py) |
| worker 提议任务只能只读，host 复核角色/去重/依赖/总量 | `orchestrator/graph.py::validate_proposals` | T1（不允许 worker 自由扩大写范围） | [`test_orchestrator.py`](../tests/unit/test_orchestrator.py) |

## 5. 从 T1 升到 T2 需要的前置条件

按"防线层"给出：现状 → 缺口 → 达标要做什么。

### 5.1 文件系统隔离（最关键）

- **现状**：默认 off，命令以用户身份运行，`/home` 可读；opt-in `bwrap` 已能隐藏 `/home` `/root` `/mnt`。
- **缺口**：默认不隔离；bwrap 依赖非特权 user namespace（多数 WSL 可用，但不保证）。
- **达标**：
  1. 默认改为 `bwrap`（或提供"无 bwrap 即拒绝启动"的部署档）；
  2. 只读根 + **白名单** bind（只挂工作区与必需系统目录），而非现在的固定集合；
  3. 明确"工作区是唯一可写点"。

### 5.2 网络隔离

- **现状**：bwrap 模式支持 `AGENT_SHELL_NETWORK=false`，会加 `--unshare-net` 完全断网；
  `untrusted` 与 `production/ops` profile 强制关闭网络。默认 local 仍为 `true`，
  因为 `deps_install`/构建通常需要网络。
- **缺口**：默认 local 不隔离网络；`off` 后端没有内核级网络限制。
- **达标**：按任务设置网络档（deny / allowlist proxy），默认对 implement/测试任务收紧。
  这是"安全 vs 可用"的直接取舍，必须显式配置而非默认放开。

### 5.3 进程与能力

- **现状**：`--unshare-pid --unshare-user --unshare-ipc --new-session
  --cap-drop ALL`；系统目录仍以只读方式挂载。
- **缺口**：未限制可执行集合（只读 `/usr` 仍可运行任意系统程序），也没有 seccomp。
- **达标**：限制可执行集合，并配合 5.4 的 syscall 过滤。

### 5.4 内核对接口（seccomp）

- **现状**：无 syscall 过滤。
- **达标**：`bwrap --seccomp` 加载策略，限制危险 syscall（如 `ptrace`、`mount`、原始 socket）。

### 5.5 命令解析

- **现状**：**词法守卫是 best-effort、可绕过**（编码、变量、间接执行、新变体无穷）。
- **结论**：**T2 不能把它当兜底** —— 必须由 5.1 的 OS 隔离兜底；词法守卫只作为"减少确认噪音 + 拦截明显危险"的 T1 手段。

### 5.6 资源隔离

- **现状**：`ulimit`；对子进程/新会话的约束不完全。
- **达标**：cgroup v2（memory/cpu/pids 硬上限），不依赖进程自我约束。

### 5.7 审计完整性

- **现状**：审计在**宿主侧**（好），但快照在**工作区内**（可被 agent 改）。
  编排合并前的快照同样落在工作区 `.agent/backups/`。
- **现状（新增）**：审计落盘前对常见 secret（`sk-*`、GitHub/GitLab token、
  AWS key、JWT、Slack/Google token、私钥块、`password=...`、数据库连接串）
  做模式脱敏；参数值递归处理。
- **现状（新增）**：shell / search / file_read 回灌模型前也做同一套脱敏，
  避免配置文件里的凭据直接进入模型上下文。
- **缺口**：脱敏是模式匹配，不保证覆盖所有凭据格式；审计与工具输出都不能
  替代真正的凭据管理；审计本身仍可被宿主用户改。
- **达标**：快照也移出工作区或加校验和；审计考虑远端/只追加存储，防"改完再抹痕"。

### 5.8 凭据与写入

- **现状（新增）**：WSL 命令通过 `env -i` 启动，宿主环境变量不再进入 Linux
  子进程；HOME 由脚本内 `getent passwd` 补回。`printenv` / `env` 升级为 L3。
- **现状（新增）**：读保护覆盖 `.env`、私钥/证书、`.npmrc`、`.netrc`、
  `.aws/**`、`.kube/**`、`credentials*.json`、`*.tfstate` 等；shell 中
  显式引用、`cat .env*` 这类通配形状、以及检索密码关键词都会升级为 L2。
- **现状（新增）**：`file_write`/`file_edit` 默认拒绝上述目标，
  `AGENT_WRITE_DENY_GLOBS` 可继续追加。
- **现状（新增）**：`AGENT_ALLOW_RULES` 支持 `tool:glob` 持久放行，但只覆盖
  原本会 ASK 的 L2，不会绕过 L3 或 `deny` 模式。
- **缺口**：shell 重定向仍可能写敏感路径（需 L2 审批；allow rule 也可能放宽）；
  读保护仍是文件名/模式匹配，无法识别任意自定义凭据。
- **达标（T2）**：shell 写路径接入同一套内容级策略；凭据改由 secret manager
  注入，而不是落在工作区文件里。

### 5.9 提示注入（真正的 T2 主战场）

- **现状**：读到的文件内容与用户指令**同等信任**，都进模型上下文。
- **untrusted 模式**：不注入仓库 `AGENTS.md`/`CLAUDE.md`，不读取仓库
  `.agent/memory.md`；记忆移到仓库外。文件内容本身仍可能诱导模型行动。
- **达标**：内容信任分级（工具结果标记为"数据、非指令"）、对"由文件内容触发的危险动作"强制人工确认。这是模型层问题，隔离层解决不了。

### 5.10 审批与前端

- **现状**：策略在宿主、前端绕不过；`--yes`/`approve` 只自动放行 L2，
  **L3 始终需要人工确认**。
- **Web**：Bearer Token 认证；未配置 token 只允许 loopback 匿名；非 loopback
  必须 token + TLS（本进程或可信反向代理），否则拒绝启动。
- **达标**：T2 场景仍应禁用 `approve` 模式（L2 仍会被自动放行）。

## 6. 结论

- **默认 local profile = T1**：防误操作 + 可审计 + 可回滚。
- **untrusted profile / 公网 Web = 接近 T2 的工程基线**：bwrap + 禁网 +
  强制 ask + 租户隔离 + 秘密读保护 + 工具输出脱敏 + 审计链；但仍不防内核逃逸、
  恶意 WSL 发行版与拥有宿主权限的管理员。
- **production/ops profile = 只读审计档**：bwrap + 禁网 + `approval_mode=deny`，
  运行时强制关闭 `allow_write`；L2/L3 一律拒绝。它不是"安全的生产变更通道"，
  而是"允许只读排查、不允许任何变更"的档位。
- **答辩建议**：把 T1/T2/T3 的边界讲清楚——"默认 T1；公网模式强制 T2 基线，
  生产只读用 production profile，并明确残余风险"，比含糊地说"很安全"更站得住。

## 7. 维护

本文档随防线变动更新。新增/修改安全相关代码时，对照 [ARCHITECTURE.md](ARCHITECTURE.md) 第 6 节不变量表与本表
确认：**属于 T1 的手段不要被当成 T2 的保证**。
