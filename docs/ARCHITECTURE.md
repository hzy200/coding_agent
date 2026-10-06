# 架构

面向实现：分层、数据流、不变量与取舍理由。开发方案与排期见 [PLAN.md](PLAN.md)。

## 1. 一条原则

> **宿主内置工具，模型只输出意图。**

LLM 拿不到操作系统句柄，只能产出结构化调用 `{tool, args}`；宿主校验后在 WSL2 沙箱里执行。
文件修改走专用工具（精确替换 + diff + 写前备份），不经 shell。

由此推出四件事：

| 后果 | 实现 |
|---|---|
| 模型说什么不算数，宿主说了算 | 命令分级在 `sandbox/policy.py`，不看模型自报 |
| 危险操作过不去 | L2/L3 先挂起等审批；不批就不执行 |
| 改动可回滚 | 写盘前快照，恢复前再留底 |
| 一切可追溯 | 审计由 runtime 写，前端绕不过 |

## 2. 分层与依赖方向

```
┌──────────────────────────────────────────────────────────┐
│ 前端  cli/   tui/   web/                                  │
│   只消费领域事件，**不得导入 tools/ 或 sandbox/**           │
└───────────────────────┬──────────────────────────────────┘
                        │ AsyncIterator[Event]
┌───────────────────────▼──────────────────────────────────┐
│ runtime.py   AgentRuntime                                 │
│   唯一编排入口：持有图、沙箱、安全策略、审计、快照存储        │
│   把 LangGraph 的原始流翻译成领域事件                       │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ graph/  state.py routing.py build.py nodes/               │
│   planner → act ⇄ (approval_gate → tools) → verify        │
│   → review → advance → replan → …                         │
│   两道质量关：verify 管行为对不对，review 管代码干不干净      │
│   一步没有产生任何改动 → nudge 打回重做一次（上限一次）        │
│   步进后经 replan：计划可在执行中被修正（修的是"后面"）        │
│   节点只读状态、返回状态增量；不碰 I/O 以外的宿主资源         │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ tools/   shell files git deps search testrun              │
│   能力层：执行 + 路径守卫 + 产出结构化 artifact             │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ sandbox/ wsl_exec fs snapshots policy pathguard limits    │
│   WSL2 进程、路径校验、命令分级、资源上限                   │
└──────────────────────────────────────────────────────────┘
        events.py  config.py  messages.py  diffing.py  audit/  memory/
                          （横向：各层都可依赖）
```

**依赖方向是单向的**，`tests/unit/test_architecture.py` 用 AST 扫描守住最关键的一条：
`tui/` 与 `web/` 若导入 `coding_agent.tools` 或 `coding_agent.sandbox` 直接失败。

为什么这条最重要：一旦前端能直接调工具，就会出现**绕过审批的第二条执行路径** ——
Web 页面上一个按钮就能跳过命令分级。这条约束是安全属性，不是代码风格。

> `cli/` 是唯一的例外：`doctor` 与 `sandbox-init` 需要直接探测沙箱。
> 它们是诊断命令，不执行模型意图。

## 3. 目录职责

| 路径 | 职责 | 不该做的事 |
|---|---|---|
| `runtime.py` | 编排、事件翻译、审计、生命周期 | 不含业务规则（分级在 policy） |
| `events.py` | 前端契约（9 个事件） | 不含逻辑 |
| `graph/state.py` | 跨节点传递的状态 | 不放能推导出来的派生值 |
| `graph/nodes/` | 单个节点的纯函数 | 不直接 yield 事件（节点只返回状态增量） |
| `graph/routing.py` | 条件边 | 不做副作用 |
| `tools/` | 能力：执行 + 回报 | 不做安全判定（判定在 approval_gate） |
| `sandbox/policy.py` | 命令分级、会话策略 | 不执行命令 |
| `sandbox/fs.py` | 沙箱内文件读写 + 路径双重校验 | 不做业务判断 |
| `audit/` | JSONL 审计 | 不解析工具文本输出 |
| `memory/` | checkpoint / 会话索引 / 长期记忆 | 不持有业务状态 |

## 4. 一次完整运行

```
前端                  Runtime                     Graph                  沙箱
 │                      │                          │
 │─ run(prompt, tid) ──▶│                          │
 │                      │ 组装 inputs（含 memories 快照）
 │                      │ 组装 config（trace metadata）
 │                      │ 审计 run_start
 │                      │─ astream(payload) ──────▶│
 │                      │                          │─ planner ─┐
 │                      │◀─ updates{planner} ──────│           │ 分解子任务
 │◀─ PlanCreated ───────│                          │           │
 │◀─ StepStarted ───────│                          │◀──────────┘
 │                      │                          │
 │                      │◀─ messages{tokens} ──────│─ act ─────┐ 调模型
 │◀─ AssistantToken ────│                          │           │
 │                      │◀─ updates{act} ──────────│           │
 │◀─ ToolCallStarted ───│                          │◀──────────┘
 │                      │                          │
 │                      │                          │─ approval_gate ─┐
 │◀─ ApprovalRequested ─│◀─ __interrupt__ ─────────│                 │ 需要人工？
 │                      │                          │◀────────────────┘
 │  （前端收集答复后 resume）
 │                      │                          │─ tools ───▶│─ 校验 + 执行 ─┐
 │                      │◀─ updates{tools} ────────│◀───────────│◀─────────────┘
 │◀─ ToolCallFinished ──│  写审计 tool_call        │
 │◀─ FileChanged ───────│  写审计 file_change       │
 │                      │                          │
 │                      │◀─ updates{verify} ───────│─ verify ──▶│ 跑测试/构建
 │◀─ Verification ──────│  写审计 verify            │◀───────────│
 │                      │                          │
 │                      │                          │ 通过 → review
 │◀─ ReviewFinished ────│◀─ updates{review} ───────│─ review ──▶│ 确定性静态审查
 │                      │  写审计 review            │◀───────────│ （+ 探测到的 linter）
 │                      │                          │
 │                      │                          │ 任一关没过且还有预算 → repair → act
 │◀─ RepairStarted ─────│◀─ updates{repair} ───────│
 │                      │  写审计 repair            │
 │                      │                          │
 │                      │                          │─ advance → replan / respond
 │◀─ StepStarted ───────│                          │
 │                      │                          │  改了剩余步骤 → 写审计 replan
 │◀─ PlanRevised ───────│◀─ updates{replan} ───────│
 │◀─ AssistantToken ────│  （respond 的 token）      │
 │◀─ RunFinished ───────│  写审计 run_end            │
```

要点：

- **节点不产生事件**。节点只返回状态增量；事件由 runtime 从 `updates` 里翻译出来。
  这样节点的测试不需要关心事件，前端的测试不需要关心图。
- **审计与事件同源**。两者都从同一份 `updates` 产生，不存在"界面显示了但没记账"。
- **挂起时以 `ApprovalRequested` 收尾**，不发 `RunFinished` —— 前端据此判断该 resume 还是收工。

## 5. 图状态与生命周期

| 字段 | 含义 | 谁重置 |
|---|---|---|
| `messages` | 对话与工具轨迹 | `add_messages` 追加 |
| `cwd` | 沙箱内工作目录 | 每次 run 由 runtime 注入 |
| `memories` | 项目长期事实 | 每次 run 从文件重读 |
| `plan` / `step_idx` | 子任务与进度 | planner 初始化，advance 递增，replan 改写剩余部分 |
| `replan_count` | 已重建计划的次数 | planner 清零、replan 递增；**整个任务**的额度，advance 不动 |
| `tool_rounds` | 当前步骤已用轮次 | planner / advance / repair 清零 |
| `budget_exhausted` | 本步因预算耗尽收尾 | 同上 |
| `dirty` | 本步是否改过东西 | 由 tools 置位，advance/planner 清零，repair 置位 |
| `verification` | 最近一次验证结果 | planner / advance 清空 |
| `review` | 最近一次代码审查结果 | 同 `verification`：planner / advance 清空，**repair 刻意不清**（act 要靠它知道该改什么） |
| `review_watermark` | 已审过的最大快照 id | **只由 planner 归零**；advance / repair 都不许碰（见下方不变量 19） |
| `retry` | 当前步骤的修复次数 | planner / advance 清零，repair 递增。**verify 与 review 共用这一份预算** |
| `approvals` | call_id → 审批结果 | approval_gate 写入，tools **消费后清空** |

`approvals` 的一次性语义是安全属性：不清空的话，下一轮的调用会复用上一轮的批准。
有测试守着（`test_approvals_do_not_leak_into_next_round`）。

## 6. 不变量

这些是几周里踩出来并逐一固化成测试的。改动相关代码前请先读它们。

| # | 不变量 | 为什么 | 测试 |
|---|---|---|---|
| 1 | **act 只在还有预算时产出 `tool_calls`** | 路由因此可无条件信任"有 tool_calls 就送去执行"，不会留下没人应答的 tool_calls（那会让下一轮 API 直接 400） | `test_act_stops_without_tool_calls_when_budget_exhausted` |
| 2 | **审批结果一次性** | 否则跨轮次误放行 | `test_approvals_do_not_leak_into_next_round` |
| 3 | **上下文裁剪只截断内容，绝不丢弃消息** | 丢消息会破坏 `tool_calls` ↔ `tool_call_id` 配对 | `test_preserves_tool_call_pairing` |
| 4 | **工具产物是结构化 artifact，不解析文本** | 文本是给模型看的，格式随时会变 | `test_parse_artifact_dispatches_on_kind` |
| 5 | **前端不得导入 tools / sandbox** | 否则出现绕过审批的第二条执行路径 | `test_ui_does_not_import_capability_or_sandbox` |
| 6 | **fail closed** | 审批记录缺失 / 答复无法解析 / 非交互环境，一律拒绝 | `test_missing_approval_fails_closed` |
| 7 | **审计失败显式报出** | 审计有缺口是这个项目不能接受的失败模式 | `test_audit_write_failure_surfaces_as_run_failed` |
| 8 | **所有写操作先留底** | 回滚本身也要可回滚 | `test_restore_is_itself_undoable` |
| 9 | **路径校验做两次**（词法 + realpath） | 词法挡不住"先建符号链接再穿透" | `test_symlink_escape_is_rejected` |
| 10 | **审批结果跨挂起/恢复仍带调用参数** | 挂起与恢复是两次 `_stream`；配对表若只活在局部，恢复后审计会丢掉参数——偏偏是最高风险的 L2/L3 调用 | `test_approved_tool_call_keeps_args_across_resume` |
| 11 | **悬空符号链接也按目标解析**（`-e` 或 `-L` 都触发） | 悬空链接的 `-e` 为假，只判存在性会顺着链接写到工作区外 | `test_dangling_symlink_escape_is_rejected` |
| 12 | **只读命令的参数越界升级到人工确认** | 命令名只读 ≠ 参数安全（`cat ~/.ssh/id_rsa`） | `test_external_access_is_escalated_to_confirmation` |
| 13 | **引号内的字面量不当命令** | 危险模式匹配全文会把 `grep "rm -rf"` 误判；但 `$()` / `sh -c` 里的内容仍要扫 | `test_quoted_literals_are_not_dangerous` |
| 14 | **读取的大小上限在读取之前生效** | 读完再拒等于先把整份文件物化进内存，上限形同虚设 | `test_fs_oversized_file_is_not_read_in_shell` |
| 15 | **改了就要验证**：`dirty` 覆盖 shell 变更 | 只按工具名判定会漏掉经 shell 的修改（`sed -i`），verify 被跳过 | `test_shell_mutation_marks_dirty` |
| 16 | **重规划只改写剩余步骤** | 已完成的是既成事实；删掉它们会让 `step_idx` 与计划的对应关系断掉，后续步进全错位 | `test_revision_replaces_only_the_remaining_steps` |
| 17 | **重规划有硬性次数上限，且到顶后不再问模型** | 边做边改没有收敛点；每次重规划都要多一次模型调用 | `test_cap_stops_asking_the_model` |
| 18 | **重规划失败沿用原计划** | 模型给不出结构化结果不能拖垮整张图 —— 计划不完美也比任务中断强 | `test_structured_failure_keeps_the_old_plan` |
| 19 | **审查水位线跨步骤保留，只由 planner 归零** | 留底记的是**写前内容**，所以改过的文件永远与自己的留底不同。水位线一旦被逐步清零，每一步都会重审前面所有步骤的改动，同一个早已处理过的问题反复告警到把修复预算耗光 | `test_watermark_stops_the_same_findings_from_repeating`、`test_advance_increments_step_and_resets_per_step_state` |
| 20 | **路由每一跳只看自己那道关** | `repair` 刻意不清 `review`，若 `route_after_verify` 也去看 review，"验证失败→修复→验证通过"这条路上陈旧的阻断会把刚通过的验证直接打回 repair，**审查再也不重跑** | `test_verify_does_not_reroute_on_a_stale_review_block`、`test_review_blocking_loop_converges_and_reviews_every_round` |
| 21 | **审查阻断与验证失败共用修复预算，额度耗尽一律去 replan** | 两个独立计数器会让 `estimate_recursion_limit` 多一层乘积，而它算歪过三次。共用的收敛性由 replan 的硬上限保证 | `test_review_blocking_loop_converges_and_reviews_every_round`、`test_review_blocking_loop_terminates_with_a_zero_budget` |
| 22 | **审查没跑起来 ≠ 审查通过** | 与审计、验证同源的一条：脚本崩了要留一条可见的告警，而不是静默当成"审过了没问题" | `test_a_crashed_review_is_not_reported_as_clean` |
| 23 | **「这一步只读了文件」不等于「这一步做完了」** | `verify` 与 `review` 都只在 `dirty` 时执行，所以一个只读步骤会**一路短路到 advance** —— 实测里模型因此把整个计划"读"完就算完成（长程任务 10 次运行里 5 次一次写都没尝试过）。改为一律经 `nudge` 打回重做一次，并在提示里说明原因 | `test_a_step_with_no_change_is_sent_back_to_act`、`test_a_step_that_changes_nothing_is_retried_once_then_accepted` |
| 25 | **「没验证」不等于「通过」** | 识别不出测试命令时原先是 `not_configured` → 路由判为非 failed → **审计记成 `ok=True`**，于是"没做"与"做对了"在账上分不开（P0-2）。现在：① 有源码就做**语法解析级降级验证**（`kind=syntax`），② 事件与审计的 `ok` 都是**三态**（True/False/**None=没验证**） | `test_a_python_project_without_tests_falls_back_to_a_syntax_check`、`test_the_audit_records_not_configured_as_unverified_not_passed`、`test_verification_ok_is_three_state` |
| 24 | **空步骤的重做有界，且必须给退路** | 只读步骤是合法的（计划第一步常常就是探索）。无限要求"必须有改动"会逼出无意义的改动，所以只给一次机会，且提示里明确写了"这一步确实只需只读就说明理由停下" | `test_the_nudge_happens_only_once_per_step`、`test_nudge_tells_the_model_why_it_is_back` |

## 7. 关键取舍

### 为什么不直接用 `ToolNode`

`response_format="content_and_artifact"` 在 `tool.invoke()` 下**会丢弃 artifact**
（实测；该语义只在 `ToolNode` 内生效，而 `ToolNode` 在 langgraph 1.2.x 下无法脱离图调用）。
这条契约是事件层与审计层的地基，不适合押在语义不明的框架行为上，
改成工具返回 `artifacts.pack(text, artifact)` 自描述封装，由宿主显式拆开。

### 为什么审批在节点，不在工具里

审批结果是**逐次调用**的图状态，而工具是无状态的。把结果塞进模型可见的参数会被模型伪造；
塞进隐藏参数又要跟框架的 schema 校验搏斗。所以：

- `approval_gate` **只做决定**（放行 / 询问 / 拒绝），保持纯函数
  （`interrupt()` 恢复时节点会从头重跑）
- `tools` 节点**负责强制** —— 它是所有工具执行的唯一咽喉，没拿到 `auto`/`approved` 的调用一律不执行

### 为什么自动 review 也不走审批

与 verify 同一条理由，再加一条：审查**只读**工作区、不执行工作区里的代码。
它跑的是宿主写死的检查脚本（`tools/review.py`）加探测到的 linter，命令文本
完全由宿主决定 —— 与自动 verify 受同一条约束（不读模型可写的 manifest）。

### 为什么 review 的「改动前」用快照，不另建基线

文件工具每次落盘前都留了底（不变量 8），所以「这次改了什么」本来就有权威记录，
不需要再加一套影子存储。代价说清楚：**经 shell 改的文件（`sed -i`）没有留底，
会完全逃过审查** —— 这条质量关不是安全边界，「禁止用 shell 改源码」只是系统
提示里的软约束。

### 为什么自动 verify 不走审批

区别在**谁决定执行什么**：verify 只能跑**命令文本由宿主写死**的命令
（`cargo test` / `go test` / `pytest`），模型影响不了跑什么；
模型主动调 `run_tests` 才走 L2 审批。

**这条线划过一次位置。** 早先的写法是「跑的是宿主从项目清单推导出的固定命令」——
但 `make test` 跑什么写在 Makefile 里、`npm test` 写在 package.json 里，而这两个
文件模型用 `file_write` 就写得到（`--write` 下是自动放行的 L1）。于是
「写个恶意 Makefile → 下一次脏写自动执行」就成了一条**不过审批的执行路径**，
把命令分级整个绕开。现在自动 verify 传 `allow_manifest=False`，manifest 派生的
命令只留给走审批的 `run_tests`（见 `tools/testrun.py::_detect`）。

### 为什么结构化工具而不是让模型拼 shell

Git、依赖、检索都有专用工具。换来三件事：参数无法逃逸（逐参数 `shlex.quote`、
包名白名单正则）、等级可判定（不再对命令字符串猜正则）、审计可读。

### 为什么 `.env` 优先于环境变量

通用约定是反过来的，这里刻意偏离。原因是踩过真实的坑：某些工具链会在启动时
往进程环境注入值，用户既看不到来源也删不掉，导致改 `.env` 完全不生效。
项目目录里的 `.env` 是显式写下、看得见、可编辑的意图。详见 [USAGE.md](USAGE.md#配置来源)。

## 8. 扩展点

### 加一个工具

1. 在 `tools/` 写 `build_xxx_tools(settings, sandbox)`，返回 `BaseTool` 列表
2. 返回值用 `artifacts.pack(text, artifact)` —— 文本给模型，artifact 给事件层与审计层
3. 在 `tools/registry.py::build_tools` 注册
4. 在 `graph/nodes/approve.py::tool_level` **登记风险等级**
   （未登记的按最危险的 L3 处理，这是故意的 fail closed）

路径参数记得走 `ensure_inside`；命令参数逐项 `shlex.quote`。

### 加一个节点

1. 在 `graph/nodes/` 写节点函数：签名 `(state, config) -> dict`，返回**状态增量**
2. 在 `graph/build.py` 接线
3. 在 `graph/routing.py` 加条件边
4. 若产生用户可见的东西，在 `runtime._stream` 里把对应的 `updates` 翻译成事件
5. **回来同步 `estimate_recursion_limit`** —— 那个公式算歪过三次，每次都是漏算了
   某个会「重新开始」的机制
6. 在 `graph/state.py` 声明新字段，并想清楚**谁写入、谁重置**（漏重置会让状态跨步串味；
   重置过头会把跨步需要的东西清掉 —— `review_watermark` 就是后者）
7. 前端（`cli/app.py`、`tui/app.py`）补事件映射

节点里不要 yield 事件、不要写审计 —— 那些是 runtime 的职责。

第 4 与第 7 步都有守卫：`test_graph_event_coverage.py` 用 AST 断言「每个图节点要么
有事件翻译、要么在 `SILENT_NODES` 里显式登记」，漏翻译会当场变红；前端漏映射则
**不报错，只是时间线上少一行**，所以那一步得自己盯着。

### 加一个前端

1. 只依赖 `runtime.AgentRuntime` 与 `events`
2. 消费 `AsyncIterator[Event]`，不认识 LangGraph
3. **不要导入 `tools/` 或 `sandbox/`**（架构测试会拦）

参考 `tui/app.py` 的事件映射表，或 `web/app.py` 里 `model_dump_json()` 直接当 SSE 帧的写法。
