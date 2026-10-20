# 端到端能力评测（tests/eval）

回答的问题是：**给定一个任务，这个智能体能不能把它做完。**

与另外两层测试的分工：

| 层 | 测什么 | 需要什么 | 进 CI |
|---|---|---|---|
| `tests/unit` | **机制**：判定逻辑、路由收敛、事件翻译 | 无 | 是 |
| `tests/integration` | **机制**：真实 WSL 沙箱行为 | WSL | 否 |
| `tests/eval` | **能力**：任务完成率 | WSL + API Key | 否 |

它是**尺子**，不是回归。回归失败要修代码；评测通过率下降只说明能力变了，
需要人来判断这是不是可接受的。

## 上下文/记忆专项标尺

`context_memory_bench.py` 不调用模型、不依赖 WSL，可直接进 CI：

```bash
python tests/eval/context_memory_bench.py          # 人读
python tests/eval/context_memory_bench.py --json   # 机器读 / A/B
```

- **长会话压缩**：30 轮工具历史按字符 + token 预算裁剪，检查
  「目标 / 约束 / 最近结论 / 最近改过的文件」是否仍可见，并报告压缩率；
- **记忆检索**：30 条事实 + 自然语言查询，报告 recall@K、无关查询回退、
  冲突替换与过期过滤。

门槛由 `tests/unit/test_context_memory_bench.py` 钉住，防止标尺本身退化。

## LangSmith 全功能评测

`langsmith_suite.py` 把能力任务和机制探针放进同一份数据集：

```bash
# 创建/补齐数据集（139 个 capability + 11 个 mechanism = 150）
python tests/eval/langsmith_suite.py --upload --kinds all

# 只跑机制探针（安全、路由、持久化、代码索引、账单配置、常驻 shell…）
python tests/eval/langsmith_suite.py --run --kinds mechanism

# 跑覆盖主要回路的 6 个 capability 抽样
python tests/eval/langsmith_suite.py --run --smoke

# 只跑单个 case
python tests/eval/langsmith_suite.py --run --only single_file/off_by_one
```

- `capability`：复用 `run_task`，真实 WSL + 模型，评估任务完成率；
  其中新增 `full` 套件 **104 个任务**（字符串/数值/集合/解析/校验/状态/
  跨文件/排障/spec/长程/并发/重构），种子与参考解均通过 `--verify-seeds`；
- `mechanism`：不调用模型，断言具体机制（secret 脱敏、写策略、allow rules、
  steering 持久化、provider cost/capability 路由、import 图、账单配置、
  shell 分级、LSP 分帧、常驻 shell、跨进程 usage）；
- 子集运行会创建派生 dataset，不会误跑全量。

最近一次本机实跑（2026-10-20，`coding-agent-full-v1`）：

- `mechanism`：**11/11** 通过（安全、steering、路由、代码索引、账单配置、常驻 shell、成本对账）；
- `smoke`：**6/6** 通过（direct、跨文件、排障、spec 隐藏测试、并发读、并发写/合并）；
- `full` 套件：**104/104** 种子未解决且参考解通过（确定性自检）；新任务抽查
  `full/string/slugify`、`full/cross/plugin_registry`、`full/long/discount_pipeline`
  均通过 LangSmith 实验；
- **139 capability 全量真实模型运行：138/139 = 99.3%**；失败任务
  `long/discount_spec` 隔离复跑通过。该轮默认 `orchestrator_write_enabled=false`、
  `explore_enabled=false`，implement 与 subagent 路径由生产试跑单独验证；
- 量化明细见 [docs/LANGSMITH_REPORT.md](../docs/LANGSMITH_REPORT.md)；
- 另用真实 CLI 跑通 direct 修复与 4 个 implement worker 的并发写编排。

## 用法

```bash
# 看任务清单
python tests/eval/runner.py --list

# 种子自检：确认 29 个任务在智能体未介入时都"未解决"（不需要 API Key）
python tests/eval/runner.py --verify-seeds

# 104 个 full 套件任务的双向自检（种子失败 + 参考解通过）
python tests/eval/runner.py --suite full --verify-seeds

# 跑一个类别 / 一个任务（注意：不会写入 canonical baseline，见下）
python tests/eval/runner.py --only debug
python tests/eval/runner.py --only debug/output_order

# 全量，结果写入 baseline.json
python tests/eval/runner.py

# 量化噪声：整批跑 3 次，报告逐次波动与翻转过的任务
python tests/eval/runner.py --repeat 3

# 对比两份 baseline：差值落在两臂 spread 内就明确拒绝下结论
python tests/eval/compare.py tests/eval/baseline.json tests/eval/phase4_baseline.json

# 干预延迟基准（不调用模型、不需要 WSL）
python tests/eval/steering_bench.py --repeat 3 --delay-ms 50
```

全量会真实调用模型、耗时以十分钟计，并把 `baseline.json` 覆盖掉。

评测工作区建在 `<沙箱工作区>/.agent/eval/<task_id>/`。放 `.agent/` 下是有意的：
那里已经被系统提示、`git_add` 过滤与检索排除保护着，日常会话不会在 `ls` 里
看到几十个评测目录；而跑某个任务时，智能体的沙箱根就是那个任务目录，看不到别的任务。

### 可比性守卫：不可比的数字不许冒充基线

三种情况会让通过率**说不清含义**，runner 会在**开跑之前**（而不是烧完十分钟
额度之后）拒绝，退出码 2：

| 情况 | 为什么 |
|---|---|
| `--only` 只跑了子集 | 覆盖写全量 baseline 会让统计口径悄悄变掉 |
| 工作区有未提交改动 | 涨了 10 个点，是改了能力还是跑的时候躺着别的改动？ |
| `sandbox_capabilities.pytest` 为 false | 测的是「没有修复循环」的智能体 |

绕过的办法：`--force`（跑并写入，标记 `comparable: false` 并写明原因）、
`--baseline <路径>`（写到别处），或者把工作区弄干净（最推荐）。

### 噪声地板：单次运行分辨不出小改进

`--repeat k` 会跑 k 遍，报告 `pass_rate_per_run`、观测到的 `spread`、
以及**结果翻转过的任务清单**（`flipped`）。

**`one_task_flip` 不是这把尺子的分辨率。** 它只是算术粒度：29 个任务时翻一个是
3.4 个百分点。真正的分辨率由 `spread`（观测到的运行间波动）决定 —— 实测下来
后者比前者大一个量级，见下。

`flipped` 往往比通过率本身更有信息量：两次运行通过率可以完全相同，而成员不同
（A 翻成过、B 翻成不过，恰好相抵）—— 只看通过率会得出"没有任何变化"。

措辞上刻意**不写**「置信区间 / 显著性」：重复 2~3 次没有那种统计效力，
把它写成区间等于给噪声套上科学的壳。报告的是**观测到的逐次波动**。

#### 实测：波动比想象的大得多

同一份代码（`811636e`）、同一模型、`--repeat 2`：

| | 值 |
|---|---|
| 第 1 轮 | 15/29 = **52%** |
| 第 2 轮 | 24/29 = **83%** |
| 波动 `spread` | **31 个百分点** |
| 翻转的任务 | **11 个 / 29**（38%） |

翻转的既有难题也有简单题：`single_file/mutable_default`、`debug/empty_edge_case`
这种基础档也在翻。

**后来重测**（`nudge` 修复 + 工作区加 git 之后，`cd62014`）：同一份代码两遍是
44.8% / 37.9%，`spread` 只有 **6.9pp** —— 比上面那组小得多，但**仍有 12/29 任务
翻转**（方向相抵）。所以结论要分成两句：

> **聚合稳、成员不稳。** `spread` 小不代表单任务可比 —— 必须同时看 `flipped`。
> 上面那组 52%/83% 是这条的反面教材：波动大到能把整个改善空间吃掉。

`long` 套件（**当时 5 个任务**，现已扩到 6 个）波动更大：三个 `--repeat 2` 样本对的逐轮通过率分别是
[60,40]、[60,40]、[40,20]，合起来 **20%~60%（40pp）**。任务少、每个都长，
方差自然大 —— 目前这一档只能分辨很大的差异。

**这条推翻了一个更早的结论。** 上一份基线（单次运行）是 18/29 = 62%，当时读作
"已落进 30–70% 区间、尺子有区分度"。现在看，62% 完全落在 52%~83% 的噪声带里 ——
**那一次读数说明不了任何事**。同一把尺子，两次读数的差就能吃掉整个"改善空间"。

所以使用规则是：

- **单次运行不要拿来下结论。** 它只能回答"这条回路是不是彻底坏了"。
- 想比较两个版本，至少 `--repeat 2`，且**差异必须大于 `spread`** 才值得讨论。
- 真正要测提升，得先把 `spread` 压下来（提高任务数、降低单任务方差），
  或者把量级扩上去让差异远大于波动 —— 这正是 §「还缺什么」说的那件事。

## 两个套件

任务按**成本量级**分成两个套件，各跑各的、各记各的基线：

| 套件 | 任务 | 基线 | 跑法 |
|---|---|---|---|
| `default` | 29 个（20 basic + 9 deep） | `baseline.json` | `python tests/eval/runner.py` |
| `long` | 6 个（5 个 38 文件长程任务 + 1 个多写 worker 演示） | `baseline_long.json` | `python tests/eval/runner.py --suite long` |

分套件不是分类癖：长程档的单任务成本是前两档的若干倍，混进全量会让整轮变得
不可承受；它的波动量级也不同（任务更少、每个更长），混着报会掩盖差异。
分成套件之后，「是不是子集」变成**相对本套件**而言 —— 所以 `--suite long` 跑全集
仍然是 `comparable: true`，不需要 `--force`。

## 任务构成（default 套件）

29 个任务，分**两档**。

| 档位 | 数量 | 定位 |
|---|---|---|
| `basic` | 20 | 单点缺陷，规模 1–3 个文件，失败测试通常直接指出位置 |
| `deep` | 9 | 为**区分度**设计：症状与根因相隔多个模块、需求未被可见测试覆盖、长程多文件贯通、计划需要中途修正 |

分档不是装饰。首轮 baseline 两档都是 **100%**（当时沙箱没有 pytest，自动验证
与修复循环整场未生效），平均只用了 17% 的工具预算。一把人人满分的尺子量不出
差别 —— 所以报告通过率时**必须分档报**（见 `by_tier`）。

> 后来沙箱装上 pytest，历史基线为 12/29 = 41.4%；当前 default 已饱和为
> 29/29 ×2（见 Phase 4），后续调优需要更难的任务。

类别：

| 类别 | 数量 | 考察什么 |
|---|---|---|
| `single_file` | 7 | 读懂一个函数并改对（差一错误、可变默认参数、输入校验…） |
| `cross_file` | 8 | 理解模块间关系并一致地改（抽公共函数、搬类、改签名、新字段端到端贯通…） |
| `debug` | 11 | 从症状定位到根因（循环导入、全局状态泄漏、写未落盘、默认值被就地改写、3 个失败共用一个根因…） |
| `spec` | 3 | **可见测试全绿、需求未满足**：会不会只盯着绿化测试 |

`debug` 里有 3 个（`many_symptoms_one_cause`、`stale_todo_list`、`misdirected_request`）
来自 `tasks_replan.py`，测的是**计划本身要不要改**：请求明确列出 N 件事，规划器会
忠实产出 N 步，而探索后发现其实只需 1 步、或根因在另一个文件。它们靠 `replans`
指标与 `steps` 一起看效果 —— 重规划的主要收益是**省**，只看通过率量不出来。

### spec 类任务与隐藏测试

`spec` 任务的种子里，可见测试是**通过**的（虽然行为不符合需求）。所以"让测试变绿"
这个策略在这里完全无效，它逼智能体去读需求、去检查行为而不是检查测试结果。

判定靠 `EvalTask.hidden_tests` —— 这些文件**从不写进工作区**，只在判分时加入，
和可见测试一起跑。因此 `spec` 任务必须有隐藏测试（有测试守着这条），
否则它就会退化成一个永远"已通过"的假任务。

## 判定为什么用 stdlib 的 unittest，不用 pytest

**判定必须零依赖、到哪都能跑。** 任务里的测试一律写成 stdlib `unittest` 的
`TestCase`，判定命令固定是 `python3 -m unittest discover -v`。**请不要改成 pytest。**

这条规矩来自一次真实事故：早先判定命令是 `python3 -m pytest`，而当时沙箱里
没有 pytest，于是**所有任务都判失败**，而种子自检看到的是「20/20 未解决」——
一个看起来干净、实际什么都没测出来的结果。所以现在有两道检查：`preflight()`
先在一个已知正确的项目上确认「判定能通过」，`--verify-seeds` 再确认
「种子确实不通过」。

### 沙箱里到底有没有 pytest

**有 pytest ≠ 判定用 pytest**，这两件事要分开看：

- **判定**永远走 stdlib unittest，与沙箱装了什么无关。
- **智能体自己的自动验证**（`detect_test_command`）要求 `python3 -c 'import pytest'`
  成功。沙箱里**有没有 pytest 是会变的**（Ubuntu 默认不带，装过
  `python3-pytest` 才有），所以 runner 每次都探测一遍，结果记进
  `baseline.json` 的 `sandbox_capabilities.pytest`。

**如果它是 false，那么 Python 项目的自动验证与修复循环整场都不会生效**，
测到的是「没有修复循环的智能体」。同一份通过率，含义完全不同 —— 所以
`--force` 以外的情况下**拒绝**把这种数字写成基线（见下文「可比性守卫」）。

要测「带修复循环」的智能体：确保沙箱里有 `python3-pytest`。任务种子里已经
统一带了 `pytest.ini`（`harness.py::materialize`），探测能命中。

## 评测工作区是一个 git 仓库

`materialize` 写完种子后跑一次 `git init` + 初始提交（时间戳固定，所以同一份种子
每次得到同一个提交）。

**为什么**：实测里 `git_status` / `git_diff` 在长程任务中全部失败 —— 工作区压根
不是仓库。于是「交付与合并」那一环（`git_add` / `git_commit`）在长程档**根本测
不到**，而它是六阶段需求里的最后一段。真实项目几乎都是 git 仓库。

顺带多了一个宿主之外的独立视角：`git status --porcelain` 能看出智能体到底改了
什么，与快照、`writes` 指标互为交叉验证。

## 两条判定原则

**一、只看外部可观察结果。**
判定是在沙箱里跑项目自带的测试，不看智能体用了哪些工具、改了几个文件、
回答是否漂亮。内部实现随便换，只要结果对。

**二、判定不可被篡改。**
每个任务的测试文件在判定前会被 `harness.py` 里的**纯净副本**覆盖回去。
所以 `EvalTask.tests` 里的内容会被写两次 —— 一次作为种子（让智能体能自己跑
测试），一次在判定前恢复。这不是冗余，是判定可信性的来源：智能体改测试
文件没有任何收益，必须真的改源码。

## 历史基线（已归档）

早期数字保留在 `baseline.json` / `ab_*.json` 中，只用于说明口径，**不再作为
当前基线**：

- 2026-10-05 首轮 100% 是“沙箱无 pytest、验证/修复循环未生效”的假象；
- 2026-10-06 `--repeat 2`：default 12/29 = 41.4%（spread 6.9pp，12 个任务翻转），
  long 2/5 = 30%（spread 20pp）；
- 同一份代码曾跑出 52% 与 83%（spread 31pp）——单次运行不能当结论。

机制指标、可比性守卫与 `flipped`/`spread` 的方法论仍然有效；当前数字见下。

## Phase 4：成本、并发与噪声感知对比

### 成本与并发指标

| 指标 | 口径 |
|---|---|
| `input_tokens` / `output_tokens` | **主 agent** 的 run_total；委派子智能体的 token 已由 runtime 累加进来 |
| `worker_input_tokens` / `worker_output_tokens` | **编排 worker** 的用量（dispatch 不并入 run_end，必须单独累加） |
| `total_input_tokens` / `total_output_tokens` | 主 agent + 编排 worker |
| `worker_seconds` | Σ `WorkerFinished.duration_ms` |
| `max_worker_batch` | **同一时刻活跃 worker 数**的峰值（按 `WorkerStarted`/`WorkerFinished` 配对；实时事件会交错，不能再用连续 started 的段长） |
| `worker_parallelism` | `worker_seconds / Σ agent_seconds`；>1 才说明并行真的发生 |
| `worker_roles` | explore/test/review/implement 次数 |
| `worker_merge_attempts` / `worker_conflicts` | implement 合并尝试 / `merge_error` 非空 |
| `merge_success_rate` / `conflict_rate` | 无合并尝试时为 `None`，不是 0% |
| `tasks_reporting_worker_tokens` | 有多少任务真的回报了 worker 用量 |

`--repeat k` 时这些字段按轮求和；`worker_parallelism` 由合并后的秒数重算，
不是把每轮的比率再平均。

`RunFinished` / `RunFailed` 也直接携带 `worker_input_tokens` /
`worker_output_tokens`（主 agent token 仍单独记），TUI 会显示这一行。

### 噪声感知对比（`compare.py`）

```bash
python tests/eval/compare.py a.json b.json [--json]
```

- 通过率 Δ 与两臂 `spread` 取大者比较；**差值在带内时输出“不能下结论”**；
- 重复次数 < 2 或任务集合/套件不同 → `insufficient_repeats` / `incomparable`；
- 两臂间翻转的任务若在任一臂内部也翻过（`noise.flipped`），标为
  `noise_suspect`，不能归因于改动；
- 同时对比墙钟、主/worker/total token、并行度、合并/冲突率。

### 干预延迟（`steering_bench.py`）

```bash
python tests/eval/steering_bench.py --repeat 3 --delay-ms 50
```

合成图 `work(sleep) → steering_step → respond`，由真实 `AgentRuntime` 驱动，
不调用模型、不依赖 WSL。实测（delay=50ms，repeat=3）：

| 场景 | wait（提交→边界） | apply（边界→事件） | total |
|---|---|---|---|
| pause | 55.9ms | **9.4ms** | 64.4ms |
| resume | — | — | **20.2ms** |
| cancel | 59.9ms | **8.6ms** | 73.7ms |

`wait` 由当前节点决定（真实运行里就是模型/工具耗时）；`apply` 才是 steering
的处理开销。不要把 `total` 当成“干预有多快”——它主要反映你等当前这步等了多久。

#### default `--repeat 2` 实测（2026-10-18）

`tests/eval/phase4_baseline.json`，工作区有未提交改动（`comparable=false`）：

| 项 | 值 |
|---|---|
| 两轮通过率 | **29/29、29/29 = 100%** |
| `spread` / 翻转 | **0.0pp** / 无 |
| token（两轮合计） | 主 agent in **4,333,650** / out **97,419**；worker 0（本次为 direct 模式） |
| 墙钟（两轮合计） | **1,528s** |
| 验证 / 修复 / 重规划 | 72 次（failed 9 / ok 63）、6 次、12 次 |

**结论：default 套件已饱和。** 两轮都 100%、零翻转，说明这把尺子对当前模型
没有区分度，继续用它做 A/B 只会得到 0pp。与旧 `baseline.json`（12/29）对比
会报 Δ=+58.6pp、超出 6.9pp 噪声带，但那是**跨版本/跨时间**的差异，并且右侧
`comparable=false`，不能读成“优化带来的提升”。下一步应扩展更难的 default
任务或改用 `long` 套件。

## 编排与写 Worker 验证记录（已归档）

以下为 Phase 3A–3C 的机制验证，**单轮、不可比**，只证明链路走通：

| 场景 | 关键结果 |
|---|---|
| Explore A/B | 机制发生（17 次子智能体、最大同批 5）；通过率 1/5 vs 1/5，成本 input +42.6% / output +190%；单轮不能下结论 |
| Phase 3A 只读编排 | `parallel_readonly`，5 任务 / 4 worker，最大同批 4，通过 1/1 |
| Phase 3B 重试与阻塞 | test worker 失败后重试 1 次，依赖 partial 的任务 blocked，通过 1/1 |
| Phase 3C 单写 worker | `orchestrated_implement`，1 worker，33 次工具调用，1 patch 合并，通过 |
| Phase 3C 多写并发 | `long/parallel_modules`：2 个 implement 同批（最大同批 2），2 patch 合并，通过 |

多写并发这轮还修掉一个真实缺陷：worktree 与 patch 都落在主工作区 `.agent/` 下，
第二个 worker 的脏检查会误判为用户改动。`_git_ready` 现在排除 `.agent/` 与
`__pycache__/`，由 `test_concurrent_implement_workers_ignore_agent_state` 锁住。

## long 套件：几十个文件的仓库

前两档的工作区是 1–9 个文件、几十行 —— 实测单任务 15–19s、7 次工具调用，
60 次的工具预算只用了 10–25%。那种规模量不出"自主性"：不需要探索、不需要规划，
一眼就能定位。这一档按 `IMPROVEMENT_PLAN §3.2 W2` 的要求提一个量级。

| 项 | 值 |
|---|---|
| 仓库规模 | **38 个文件 / 约 970 行**（分层：模型 / 仓储 / 校验 / 服务 / 报表 / 12 个 handler / 9 个 util / 3 个 adapter）；`parallel_modules` 是 32 个文件的独立 fixture，同样跨过编排的 30 文件阈值 |
| 任务数 | 6 个，**同一个仓库、五种形态 + 一个多写并发演示**（补样本时刻意不复制任务） |
| 改动跨度 | 3–8 个文件 |

| 任务 | 种子形态 | 改动 | 考的是什么 |
|---|---|---|---|
| `long/parallel_modules` | 两个独立模块都是 stub | 2 文件（互不依赖） | Phase 3C 多写 worker 并发：写范围不重叠、worktree 隔离、patch 顺序合并 |
| `long/discount_pipeline` | 功能全缺 | 6 文件 | 从零贯通：模型 → 配置 → 服务 → 报表 → 两个 handler |
| `long/discount_partial` | 折扣**只做了模型层** | 4 文件 | **诊断既有代码**：找出还缺哪几层，而不是照需求重建 |
| `long/audit_trail` | `EventLog` 是**死代码** | 3 文件 | 发现既有但没人用的设施，并把它接对 |
| `long/discount_and_audit` | 两条功能线都缺 | **8 文件** | 最长的那个：两条线各自的接缝都要处理 |
| `long/discount_spec` | 功能全缺，但**可见测试全绿** | 6 文件 | 会不会读需求 —— 判据在隐藏测试，prompt 不点名文件也不点名测试 |

> **为什么五种形态而不是五个同类任务**：探路实测暴露了更要紧的问题 ——
> **两次运行之间的方差比任何调参的效应都大**（同一个任务，一次改了文件、12 次验证
> 全失败；另一次连一次写都没尝试过）。2 个任务的样本下，任何"抬预算 / 改提示"的
> 实验都无法评估。补样本时若只复制同类任务，量的还是同一件事，所以按**形态**补。

### 仓库从哪来：确定性生成器（**刻意偏离**计划里的风险对策）

`docs/IMPROVEMENT_PLAN.md` 第四部分的风险对策写的是「任务源自真实仓库而非自造」。
这里**没有照做**，理由要摆明：

| 动机 | 说明 |
|---|---|
| 可复现 | 同一份规格永远生成同一份仓库；进库的是几百行生成器，不是几十个文件 |
| 零依赖 | 判定不能被第三方的 requirements 绑架（沙箱里没有 pip） |
| **问题位置可控** | 要测的是「在几十个文件里定位并贯通修改」，问题的位置必须是我们设计的 |
| 仓库体积 | 38 个文件进库会让 diff 淹没真正的改动 |

那条对策真正要防的是「任务太简单」与「任务与实现耦合」。前者由**规模**挡
（38 个文件、近千行，定位本身就要十几次工具调用），后者由既有的两条判定原则挡
（只看外部可观察结果 + 判分前用纯净副本覆盖测试）。

生成器只产"健康"仓库；每个任务用 `patch()` 注入功能缺口。**`patch()` 要求锚点
恰好出现一次** —— 模板改了而功能线没跟着改时，补丁会静默失效（功能没接上），
而"种子必挂 / 参考解必过"会随之静默走样。这条断言是整个生成方案的结构性保障，
`test_eval_harness.py` 里有对应用例。

### 判定用显式模块名，不用 `discover`

```
python3 -m unittest -v tests.test_core tests.test_discount
```

`unittest discover` 在多层包的大仓库上会踩一串问题：同名 basename 冲突、子目录
可导入性、收集顺序，以及最要命的 —— **智能体写在 `tests/` 里的临时测试会被一并
收进来**（`prepare_for_judging` 只删根级 `test_*.py`）。收窄到指定模块之后，
这一整类风险**结构性地消失**：智能体再多写几个测试文件也影响不到判定。

判定前仍会覆盖 `judge_files`（可见测试 + 隐藏测试），所以「判定不可被篡改」这条
不变量继续成立。

### 成本

长程任务单任务 token 与耗时都是前两档的若干倍（仓库大、探索多、每步还要跑测试）。
**先探路再全量**：

```bash
python tests/eval/runner.py --suite long --verify-seeds   # 不调模型
python tests/eval/runner.py --suite long --repeat 1       # 探路，看单任务成本
python tests/eval/runner.py --suite long --repeat 2       # 拿到噪声后再决定要不要跑
```

`EvalTask.agent_timeout` 是单任务的墙钟护栏：超时后**仍然照常判定**（那正是
"有没有做完"的答案），只是把原因写进 detail —— 否则一个挂死的长程任务会把串行的
整轮评测钉住。

### 当前缺口

- **default 已饱和**：两轮 29/29、spread 0，无法再区分改动优劣。任务规模仍偏小
  （最大 9 个文件），需要更难的 default 任务才能恢复区分度。
- **long 仍不够稳**：6 个任务、单任务成本高，历史基线 2/5 = 30%、spread 20pp；
  继续调优至少 `--repeat 2`，且结论必须超出 spread。
- **编排/并发收益尚未量化**：需要让两臂只差 `AGENT_ORCHESTRATOR_ENABLED`，
  用 `compare.py` 检查 Δ 是否超过噪声带。
- 具体后续项见 [HARNESS.md §14](../../docs/HARNESS.md) 与
  [ACCEPTANCE.md 已知限制](../../docs/ACCEPTANCE.md)。

### 重规划族的 A/B：一次被指标拦下的误读

`--only replan --ab` 跑出来是 `开 3/3` 对 `关 2/3`。**这不是重规划有效** ——
同一份数据里 `replans = 0`、`steps = 1`：重规划节点**一次都没有执行过**
（它挂在 `advance` 之后，而所有任务的计划都只有 1 步）。

两个开关只差一个 `max_replans`，既然该节点从未执行，两臂的行为就应当完全一致 ——
那 33 个百分点是**运行间的噪声**。没有 `steps` / `replans` 这两个指标，
这次实验会得出一个完全错误、而且看起来很漂亮的结论。

根因仍是环境：**沙箱里没有 pytest，`verification` 永远是 `not_configured`、
从不为 `failed`**，所以 `repair` 与「修不动就改计划」这条 replan 入口都够不着。
要真正量出这两个机制，必须先让沙箱能跑测试（装 `python3-pytest`），
或者把任务设计成能让 planner 产出多步计划。

## 双向自检：两类相反的错误

评测集是尺子，尺子本身错了，后面每一句"提升了 X 个百分点"都是假的。而它会以
**两种相反**的方式出错，两种都必须在跑基线之前拦下：

| 错误 | 症状 | 怎么拦 |
|---|---|---|
| **假阳性**：任务在种子状态就通过 | 什么都没测，通过率却很高 | 种子必须**不通过** |
| **假阴性**：任务根本无解 | 做对的智能体被判成失败 | 种子 + **参考解**必须**通过** |

第二种更隐蔽 —— 从结果看只是"智能体没做出来"。实际踩过一次：
`deep/field_propagation` 的可见测试断言了旧输出格式，而新需求必然要改它，
两者互相矛盾，任务无论怎么做都过不了，智能体做对了却被判失败。

所以：

```bash
python tests/eval/runner.py --verify-seeds    # 双向自检，不需要 API Key
```

它会先确认每个任务在种子状态下不通过，再确认每个提供了参考解的种子 + 参考解能通过。
**进阶档必须提供参考解**（有测试守着这条），因为多文件改动最容易写出自相矛盾的任务。

## 加任务

基础档加在 `tasks.py`，进阶档加在 `tasks_deep.py`（分开只是为了各自可读）。
然后跑：

```bash
pytest tests/unit/test_eval_harness.py -q     # 结构 + 语法 + 种子必须失败
python tests/eval/runner.py --verify-seeds    # 在真实沙箱里再确认一遍
```

`tests/unit/test_eval_harness.py` 会强制两条规矩：

1. **种子必须失败。** 判在种子状态就通过的任务是假阳性 —— 它会把基线抬高，
   却让人误以为"智能体会做"。这是评测集最容易出、也最隐蔽的错。
2. **种子必须是合法 Python。** 否则"收集期失败"就有两种含义（模块还没建
   vs 种子本身是坏的），而它们需要完全相反的处理。

写判定时注意：断言**导入面与函数输出**，不要断言内部实现。唯一的例外是任务
本身要求的结构变化（例如「旧名字必须彻底消失」），那属于外部可观察契约。

**可见测试不能与新需求矛盾。** 它只能断言那些不随需求改变的部分。例如要求
"渲染格式改成 `名称(优先级)`"时，可见测试就绝不能断言旧格式的完整输出 ——
那样任务无论怎么做都过不了。参考解就是用来发现这种情况的。

## baseline.json

```json
{
  "passed": 18, "total": 29, "pass_rate": 0.6207,
  "by_tier": {"basic": {"passed": 13, "total": 20}, "deep": {"passed": 5, "total": 9}},
  "by_category": {"single_file": {"passed": 5, "total": 7}, ...},
  "mechanism": {
    "runs": 2,
    "verifications": {"ok": 45, "failed": 74},
    "tasks_with_verification": 46, "repairs": 56, "tasks_with_repair": 7, "replans": 24,
    "input_tokens": 3752519, "output_tokens": 95042, "tasks_reporting_tokens": 58
  },
  "tasks": [{"id": "...", "tier": "deep", "verdict": "passed|failed|error|flaky",
             "tool_calls": 7, "steps": 2, "replans": 0, "repairs": 0,
             "verifications": {"ok": 1}, "input_tokens": 18321, ...}],
  "aggregation": "single_run | mean_over_repeats",
  "noise": {"repeats": 2, "pass_rate_per_run": [0.5172, 0.8276], "spread": 0.3104,
            "flipped": ["...11 个..."], "one_task_flip": 0.0345},
  "comparable": true, "comparable_reason": "",
  "recorded_at": "2026-10-05T...", "model": "deepseek-chat",
  "git_commit": "a88c822", "git_dirty": false, "max_replans": 2,
  "review_enabled": true,
  "sandbox_capabilities": {"python3": true, "pytest": true, ...}
}
```

几个字段是刻意加的，都是为了让数字**可解释**：

- `by_tier` —— 分档报通过率。基础档 100% 而进阶档 30%，与"总共 65%"是完全不同的
  两回事：后者让人以为还有很大提升空间，前者才说明尺子有区分度。
- **`mechanism`** —— 回答「那条回路真的被走过吗」。只看通过率会得出反向结论：
  README 里记着一次真实误读，`--ab` 跑出「开 3/3 对 关 2/3」看着像重规划有效，
  实际 `replans=0`（节点一次都没执行），那 33 个百分点纯是噪声。
  `verifications` 是 **status → 次数**不是单个计数：`not_configured`
  （识别不出测试命令）与 `ok` 的差别正是「验证脊柱有没有生效」的判据。
  口径：**只统计真正执行过的验证** —— `dirty=false` 时 verify 短路返回、不发事件，
  所以"每步都过 verify"不等于这里计数 ≥ 步数。
  **k>1 时这些是各轮之和**，`runs` 字段写明是几轮 —— 口径必须能一眼看出来。
  （踩过一次：聚合时漏了合并 mechanism，于是通过率是两轮均值、机制却只是第 1 轮的。）
- **`input_tokens` / `output_tokens`** —— 取 `RunFinished`/`RunFailed` 事件上的总量
  （跨挂起-恢复累加）。失败路径也计入：失败那一轮往往花费最多，把它排除会
  让成本口径系统性偏低。缺失记 `null`，**不当 0**，并用 `tasks_reporting_tokens`
  把"有多少任务真报了"暴露出来。
- `git_commit` / `git_dirty` / `comparable` / `comparable_reason` —— 只在干净工作区
  上跑出来的数字才可比较；不可比的会被显式标记（见「可比性守卫」）。
- `max_replans` / `review_enabled` —— 配置戳。它们会**改变机制指标本身**
  （review 阻断复用 repair 边，会产生新的 `repairs` / `replans`），
  开关两态的通过率不可直接对照。
- `sandbox_capabilities` —— **如果 `pytest` 是 false，智能体的自动验证对 Python
  项目从未生效过**，这时测到的是"没有修复循环的智能体"。同一份通过率，含义完全不同。
- `verdict` 四态 —— `error` 表示**判定没跑起来**（沙箱不可用、编排层崩溃），
  它既不算通过也不算失败；`flaky`（仅在 `--repeat k>1` 时出现）表示**这个任务的
  结论取决于运气**，混进 passed 或 failed 都会掩盖这件事。
