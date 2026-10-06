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

## 用法

```bash
# 看任务清单
python tests/eval/runner.py --list

# 种子自检：确认 29 个任务在智能体未介入时都"未解决"（不需要 API Key）
python tests/eval/runner.py --verify-seeds

# 跑一个类别 / 一个任务（注意：不会写入 canonical baseline，见下）
python tests/eval/runner.py --only debug
python tests/eval/runner.py --only debug/output_order

# 全量，结果写入 baseline.json
python tests/eval/runner.py

# 量化噪声：整批跑 3 次，报告逐次波动与翻转过的任务
python tests/eval/runner.py --repeat 3
```

全量会真实调用模型、耗时以十分钟计，并把 `baseline.json` 覆盖掉。

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

29 个任务，**翻一个就是 3.4 个百分点**，而模型是随机的。所以：

- 单次运行只能分辨「≥1 个任务」的差异；
- `--repeat k` 会跑 k 遍，报告 `pass_rate_per_run`、观测到的 `spread`、
  以及**结果翻转过的任务清单**（`flipped`）。

`flipped` 往往比通过率本身更有信息量：两次运行通过率可以完全相同，而成员不同
（A 翻成过、B 翻成不过，恰好相抵）—— 只看通过率会得出"没有任何变化"。

措辞上刻意**不写**「置信区间 / 显著性」：重复 2~3 次没有那种统计效力，
把它写成区间等于给噪声套上科学的壳。报告的是**观测到的逐次波动**。

评测工作区建在 `<沙箱工作区>/.agent/eval/<task_id>/`。放 `.agent/` 下是有意的：
那里已经被系统提示、`git_add` 过滤与检索排除保护着，日常会话不会在 `ls` 里
看到 20 个评测目录；而跑某个任务时，智能体的沙箱根就是那个任务目录，看不到别的任务。

## 任务构成

29 个任务，分**两档**。

| 档位 | 数量 | 定位 |
|---|---|---|
| `basic` | 20 | 单点缺陷，规模 1–3 个文件，失败测试通常直接指出位置 |
| `deep` | 9 | 为**区分度**设计：症状与根因相隔多个模块、需求未被可见测试覆盖、长程多文件贯通、计划需要中途修正 |

分档不是装饰。首轮 baseline（`deepseek-v4-flash`）上两档都是 **100%**：每个任务
平均只用掉 17% 的工具预算，`search_code` 全场合计只用了 8 次。一把人人满分的
尺子量不出差别 —— 所以报告通过率时**必须分档报**（见 `by_tier`）。

> 那一轮 100% 之后情况变了：`tasks_replan.py` 补了 3 个专测「计划需要修正」的
> 进阶任务，沙箱里也装上了 pytest（见下文），自动验证与修复循环开始真正生效。
> 当前基线是 **18/29 = 62%**，见「实测结论」。

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

## 两条判定原则

**一、只看外部可观察结果。**
判定是在沙箱里跑项目自带的测试，不看智能体用了哪些工具、改了几个文件、
回答是否漂亮。内部实现随便换，只要结果对。

**二、判定不可被篡改。**
每个任务的测试文件在判定前会被 `harness.py` 里的**纯净副本**覆盖回去。
所以 `EvalTask.tests` 里的内容会被写两次 —— 一次作为种子（让智能体能自己跑
测试），一次在判定前恢复。这不是冗余，是判定可信性的来源：智能体改测试
文件没有任何收益，必须真的改源码。

## 实测结论

### 首轮：两档都是 100%（现在看，那是「没有修复循环」的智能体）

`deepseek-v4-flash`，2026-10-05 上午：

| 档位 | 结果 | 平均工具调用 |
|---|---|---|
| basic（20） | **20/20 = 100%** | ~9 |
| deep（6） | **6/6 = 100%** | ~10 |

预算是 5 步 × 12 轮 = 60 次工具调用，**两档都只用了约 17%**。押的三个假设全没成立：
模型能跨模块追根因（`config_precedence`）、能修跨模块循环依赖（`import_chain`）、
**也能照着需求做对而不是只盯着绿化测试**（3 个 spec 任务全过）。

> **那 100% 的含义要打折扣**：当时 `sandbox_capabilities.pytest` 是 false，
> 自动验证与修复循环**整场没有生效**。它测的是一个没有第二道质量关的智能体。
> 这正是可比性守卫要把 `pytest=false` 拦在基线之外的原因。

### 当前：18/29 = 62%（2026-10-05 下午，`deepseek-chat`）

| 档位 | 结果 |
|---|---|
| basic（20） | 13/20 |
| deep（9） | 5/9 |
| 合计 | **18/29 = 62%** |

by_category：`single_file` 5/7、`cross_file` 4/8、`debug` 8/11、**`spec` 1/3**。

这次沙箱里**有** pytest（`sandbox_capabilities.pytest = true`），所以自动验证与
修复循环是生效的 —— 与首轮那 100% 不可直接比较。`spec` 档从 3/3 掉到 1/3 尤其值得
注意：那正是「可见测试全绿、需求未满足」的一类，最能区分「照着需求做」与
「照着测试做」。

> 这份数字的 `git_dirty` 是 **true**，严格说不可比 —— 记在这里是为了留下坐标，
> 不作为对照基线。下一份基线应当在干净工作区上跑，并带上 `--repeat` 的噪声块。

### 还缺什么

62% 已经落在计划要求的 30–70% 区间，所以「没有区分度」这个诊断**不再准确**。
真正还缺的是**动态范围的上端**：29 个任务里最大的也只有 9 个文件、68 行，
`tool_calls` 中位数约 7（预算 60）。要测「自主性提升了多少」需要规模上的量级变化
—— 几十文件的仓库、几十步才能完成的任务、需要判断的模糊需求、跨会话的长程工作。
那是另一个量级的工程，见 [IMPROVEMENT_PLAN.md](../../docs/IMPROVEMENT_PLAN.md) §3.2 W2。

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
    "verifications": {"ok": 12, "not_configured": 3, "failed": 1},
    "tasks_with_verification": 9, "repairs": 4, "tasks_with_repair": 3, "replans": 2,
    "input_tokens": 512340, "output_tokens": 20418, "tasks_reporting_tokens": 27
  },
  "tasks": [{"id": "...", "tier": "deep", "verdict": "passed|failed|error|flaky",
             "tool_calls": 7, "steps": 2, "replans": 0, "repairs": 0,
             "verifications": {"ok": 1}, "input_tokens": 18321, ...}],
  "aggregation": "single_run | mean_over_repeats",
  "noise": {"repeats": 1, "pass_rate_per_run": [...], "spread": 0.0,
            "flipped": [], "resolution": 0.0345},
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
