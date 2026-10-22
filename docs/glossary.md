---
layer: L2
doc_id: glossary
title: 统一语言词汇表
owner: 项目作者
status: active
last_reviewed: 2026-10-22
review_cadence: 引入新术语或发现新歧义时
criticality: 高
note: 术语歧义是 AI 出错的头号来源，本项目三套数字编号系统并存，动手前先查这里。
---

# 统一语言词汇表

**大型项目里同名不同义是 AI 出错的头号来源。** 本项目有三套数字编号系统并存、
三个词指同一物、三种不同的并发实体——动手前先查这里。

用法：写文档、写代码注释、给 AI 下指令时，都用这里的**规范名**。
别名列出来是为了"见到时能认出来"，不是为了继续用。

---

## 一、三套数字编号系统（最容易混）

| 编号 | 是什么 | 在哪 | 别混 |
|---|---|---|---|
| **L0 / L1 / L2 / L3** | **命令与工具的风险分级**（也有"文档四层"的同名用法，见下） | `sandbox/policy.py`、`docs/contracts/tools.yaml` | 不是开发阶段 |
| **Phase 3A / 3B / 3C / 3D / 4D** | harness 的**历史开发阶段**（均已实现） | `HARNESS.md §14` | 已全部落地，只作历史坐标，不代表当前状态 |
| **T1 / T2 / T3** | **威胁模型等级**：防误操作 / 防主动越狱 / 多用户公网 | `THREAT_MODEL.md §3` | 与命令分级无关 |

> ⚠️ 同名冲突：`docs/` 里"L0–L3"也用来指**文档资产四层**（可执行事实 / Agent 指令 /
> 决策与边界 / 叙事与上下文）。为避免混淆，文档四层一律写全称 **"文档 L 层"**，
> 命令分级一律写 **"命令 L0/L1/L2/L3"** 或 "四级分级"。

### 命令四级分级

| 级别 | 行为 | `-y`/`approve` 能放行吗 |
|---|---|---|
| L0 只读 | 自动执行 | — |
| L1 低风险写 | 需 `--write` 一次性授权，之后自动执行 + 审计 | — |
| L2 变更性 | 需用户确认 | 能 |
| L3 危险 | 需用户确认（标注危险） | **不能** |

---

## 二、三种并发实体（名字都很像）

| 规范名 | 谁派发的 | 深度 | 上下文 | 配置 |
|---|---|---|---|---|
| **编排 worker** | `scheduler` 按任务图依赖 | 固定 1 | 独立 messages，只注入 `session_brief` | `AGENT_ORCHESTRATOR_*` |
| **委派子智能体** | 主 agent 在 `act` 里调 `delegate_explore` | 1 | 只有系统提示 + 本次任务 | `AGENT_EXPLORE_ENABLED`（默认关） |
| **同消息并发只读工具** | 同一条 `act` 消息里的多个只读调用 | — | 共享主上下文 | `AGENT_MAX_CONCURRENT_TOOLS`（4） |

**不要**把 worker 叫成"子智能体"，也不要反过来。它们的工具白名单、预算、
取消语义都不一样。

---

## 三、三个词指同一物

| 规范名 | 别名 | 实际是什么 |
|---|---|---|
| **快照**（snapshot） | 留底、backup | 写盘前把原内容存到 `.agent/backups/<快照id>/` |

> 文档统一写"快照"。注意**目录名仍是 `backups/`**（历史原因，改名会破坏兼容），
> 不要因为目录名就把文档改回"backup"。

### 三个都叫"回退"但完全不同的东西

| 规范名 | 作用对象 | 命令 |
|---|---|---|
| **快照回滚**（undo） | **文件内容** | `agent undo` / `--last-run` / `--since` |
| **checkpoint 回退**（rewind） | **会话状态** | `agent rewind -n 1` |
| **checkpoint 分叉**（fork） | 从某个 checkpoint 开新分支，历史保留 | `agent fork` |

---

## 四、两道质量关

| 规范名 | 管什么 | 代码 |
|---|---|---|
| **verify** | 行为对不对（跑项目测试） | `tools/testrun.py`、`nodes/verify.py` |
| **review** | 代码干不干净（语法、被改弱的测试、调试残留） | `tools/review.py`、`nodes/review.py` |

两者都失败时**共用同一份修复预算**，都走 `repair` 出口。

## 五、执行路径

| 规范名 | 何时走 | 说明 |
|---|---|---|
| **direct** | 小仓库 / 单点任务 | 单 agent 快速路径：`planner → act ⇄ approval_gate → tools → verify → review → advance` |
| **orchestrated** | 文件数 ≥ `AGENT_ORCHESTRATOR_MIN_FILES`（30）且编排开启 | 独立 orchestrator 子图，并发 worker + fan-in，之后交回主 agent |

工作流的**模板名**另有一套：`direct` / `parallel_readonly` / `survey_then_implement` /
`review_loop` / `orchestrated_implement`。执行路径只有上面两种。

## 六、状态词

| 词 | 含义 | 易错点 |
|---|---|---|
| `dirty` | 本步是否改过东西 | 按命令等级判定，不只认工具名 |
| `partial` | worker 完成但验证未全通过 | 默认**不满足** `depends_mode=full` 的依赖；`any` 可消费 |
| `blocked` | 依赖 failed/blocked/cancelled，任务不会启动 | 任何 `depends_mode` 都不满足 |
| `merging` | implement 结果已进入合并队列 | **必须立刻离开 pending**，否则会被重新派发（出过 P0） |
| `degraded` | 任务图校验失败后整图降级 | 必须显式标记并发出事件，不能静默 |
| `kind`（步骤） | `explore` / `implement` / `verify` / `unknown` | 与事件的 `type` 字段、审计的 `kind` 字段**不是**一回事 |
| `ok`（验证） | **三态** True / False / None | `None` = 没跑起来，不要当 `False` |
| `skipped` | 工具能力不可用（如未配置 LSP） | 与 `ok=False` 区分开，前端显示"已跳过" |

## 七、目录与位置

| 词 | 在哪一侧 | 说明 |
|---|---|---|
| **宿主侧** | Windows | `<cwd>/.agent/`：checkpoint、audit、steering、rate_limits |
| **沙箱侧** / **工作区** | WSL2 | `<工作区>/.agent/`：backups（快照）、memory.md、artifacts |
| **工作区**（workspace） | 沙箱内 | 用户用 `-C` 指定的根目录；所有读写与命令限定在此 |

两侧都在 `.agent/` 下但**内容不同、被保护的方式不同**，别混。
审计在宿主侧（沙箱内命令改不到），快照在工作区内（工作区可写时 agent 能改自己的留底）。

## 八、编排术语

| 词 | 含义 |
|---|---|
| `write_scope` | implement 任务被允许修改的路径白名单，在文件工具 I/O 前强制校验 |
| `session_brief` | 从 checkpoint 抽取的有界会话摘要（默认 4000 字符），子图不复制 `messages`，只注入它 |
| `fan_in` | worker 结果汇总，产出 `worker_context`（给 planner）与 `orchestration_handoff`（结构化） |
| `worktree` | implement worker 的独立 git 工作树，改完导出 patch 回主工作区顺序合并 |
| `checkpoint_every` | 每完成多少个任务形成一次 checkpoint/干预边界；**不再限制每边界能启动几个** |
| `steering` | 运行中干预（pause/cancel/add/replan），只在节点边界消费 |

## 九、评测术语

| 词 | 含义 |
|---|---|
| `spread` | 同一份代码 `--repeat k` 观测到的通过率波动幅度。**差异必须大于它才值得讨论** |
| `flipped` | 两轮之间结论翻转过的任务清单。通过率相同也可能成员不同 |
| `comparable` | 基线可比性标记；工作区脏 / 只跑子集 / 沙箱无 pytest 都会置 false 并拒绝写入 |
| `verdict` | 四态：`passed` / `failed` / `error`（判定没跑起来）/ `flaky`（结论取决于运气） |
| `noise_suspect` | 两臂间翻转、但在任一臂内部也翻过的任务。**不能归因于改动** |
| 隐藏测试 | 判定前才加入、从不写进工作区的测试文件；`spec` 类任务必须有 |

> 措辞上刻意**不写**「置信区间 / 显著性」：重复 2~3 次没有那种统计效力，
> 把它写成区间等于给噪声套上科学的壳。
