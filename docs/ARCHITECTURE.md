# 架构

面向实现：分层、数据流、不变量与取舍理由。Harness 阶段方案见
[HARNESS.md](HARNESS.md)，当前状态与已知限制见 [ACCEPTANCE.md](ACCEPTANCE.md)。

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
│   → review → advance → steering_step → replan → …         │
│   编排路径：plan_tasks → steering_plan → scheduler        │
│             ⇄ steering_dispatch → fan-in              │
│   所有收尾路径经 steering_final → respond                  │
│   两道质量关：verify 管行为对不对，review 管代码干不干净      │
│   一步没有产生任何改动 → nudge 打回重做一次（上限一次）        │
│   步进后经 replan：计划可在执行中被修正（修的是"后面"）        │
│   节点只读状态、返回状态增量；不碰 I/O 以外的宿主资源         │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│ agents/  explore.py（只读 Explore 子智能体）                │
│   独立 messages；只绑定 EXPLORE_TOOL_NAMES 白名单；         │
│   由 tools 节点在宿主侧执行，不产生第二条写路径；            │
│   同一消息的多个 delegate 由 Semaphore+gather 并发（fan-out）│
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
| `events.py` | 前端契约（领域事件） | 不含逻辑 |
| `graph/state.py` | 跨节点传递的状态 | 不放能推导出来的派生值 |
| `graph/nodes/` | 单个节点的纯函数 | 不直接 yield 事件（例外：`scheduler` 用 custom stream 发 worker 实时事件） |
| `graph/routing.py` | 条件边 | 不做副作用 |
| `agents/` | 子智能体循环（独立上下文） | 不直接写主 state、不产生写路径 |
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

- **节点不直接调用前端**。绝大多数事件由 runtime 从 `updates` 里翻译；
  `scheduler` 是唯一例外：它通过 LangGraph `get_stream_writer()` 推送 worker
  开始/结束的 custom 事件（实时进度），runtime 用 `worker_id:attempt` 去重后
  翻译成领域事件。
  这样节点的测试不需要关心事件，前端的测试不需要关心图。
- **审计与事件同源**。绝大多数事件来自同一份 `updates`；`scheduler` 的 worker
  事件来自 custom stream，runtime 同样在翻译时写审计，不存在“界面显示了但没记账”。
- **挂起时以 `ApprovalRequested` 收尾**，不发 `RunFinished` —— 前端据此判断该 resume 还是收工。

## 5. 图状态与生命周期

| 字段 | 含义 | 谁重置 |
|---|---|---|
| `messages` | 对话与工具轨迹 | `add_messages` 追加 |
| `cwd` | 沙箱内工作目录 | 每次 run 由 runtime 注入 |
| `memories` | 项目长期事实（按请求检索的 top-K） | 每次 run 从文件重读 |
| `project_instructions` | 分层 `AGENTS.md` / `CLAUDE.md` 合并文本 | 每次 run 加载 |
| `stale_paths` | 本轮已成功修改的文件 | runtime 每轮置空；tools 追加 |
| `context_trim` | 最近一次 act/respond 的裁剪报告 | 节点每次覆盖 |
| `plan` / `plan_kinds` / `step_idx` | 子任务、类型与进度 | planner 初始化，advance 递增，replan 改写剩余部分（文本与 kind 同步） |
| `replan_count` | 已重建计划的次数 | planner 清零、replan 递增；**整个任务**的额度，advance 不动 |
| `tool_rounds` | 当前步骤已用轮次 | planner / advance / repair 清零 |
| `budget_exhausted` | 本步因预算耗尽收尾 | 同上 |
| `dirty` | 本步是否改过东西 | 由 tools 置位，advance/planner 清零，repair 置位 |
| `verification` | 最近一次验证结果 | planner / advance 清空 |
| `review` | 最近一次代码审查结果 | 同 `verification`：planner / advance 清空，**repair 刻意不清**（act 要靠它知道该改什么） |
| `review_watermark` | 已审过的最大快照 id | **只由 planner 归零**；advance / repair 都不许碰（见下方不变量 19） |
| `retry` | 当前步骤的修复次数 | planner / advance 清零，repair 递增。**verify 与 review 共用这一份预算** |
| `same_failure_count` / `last_failure_signature` | 连续同一失败签名的次数与签名 | verify 写入；repair 与 replan 都刻意不清；只有 planner / advance（换步骤）归零 |
| `repo_profile` / `delegation_allowed` / `delegation_budget` | 仓库画像与本次运行的委派预算 | runtime 在 `run()` 时注入；planner / advance 不动 |
| `session_brief` | 从 checkpoint 抽取的有界会话约束摘要 | runtime 每次 `run()` 重新生成；不写回 `messages` |
| `transient_retries` | 节点内瞬时错误重试次数 | runtime 每轮归零；节点写入，runtime 累加后写进 `run_end` 审计 |
| `run_status` | `running` / `finished` | runtime 每轮置 `running`；`respond` 收尾置 `finished` |
| `recovery_notice` | 上一轮异常中断时给模型的恢复提示 | runtime 每次 `run()` 根据 prior checkpoint 生成 |
| `orchestration_handoff` | fan-in 产出的任务级结构化交接（含 artifact 路径） | fan-in 每次覆盖；runtime 每轮清空 |
| `approvals` | call_id → 审批结果 | approval_gate 写入，tools **消费后清空** |
| `steering_cancelled` / `steering_replan` / `steering_replans` | 用户干预结果与次数 | runtime 每轮归零；steering 节点写入；plan_tasks 消费 replan |
| `steering_applied` / `workflow_revised_reason` | runtime 翻译事件用 | steering / plan_tasks 每次写入 |
| `tasks` / `task_status` / `task_attempts` / `worker_results` | 编排任务图与每轮结果 | plan_tasks 初始化；scheduler 增量写入 |
| `task_extensions_seen` / `task_extensions_applied` | worker 提议只读任务的扫描位置与本轮新增 | extend_tasks 写入；runtime 每轮归零 |

`approvals` 的一次性语义是安全属性：不清空的话，下一轮的调用会复用上一轮的批准。
有测试守着（`test_approvals_do_not_leak_into_next_round`）。

steering 的**指令队列不在 state 里**：它是外部输入，worker 需要一边运行一边
非破坏性轮询 `cancel_requested`，而 state 更新与图执行是串行的。暂停状态由
checkpoint 保存；未消费指令与 pause 标记由 `SteeringController` 原子落盘到
`.agent/steering/`，进程重启后可通过 `resume` 恢复。

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
| 26 | **语法错在落盘前就拒** | 原先「先写下去 → review 事后查 → 不合格再 repair」，白烧一轮工具预算与一次模型往返。语法错是这里最便宜就能确定的错误（纯解析、零 I/O），没理由等到事后。**判据是「有没有变坏」而不是「是不是好的」** —— 原本就坏的文件允许继续改，否则会堵死模型自己的分步修复 | `test_write_rejects_broken_python`、`test_edit_rejects_a_change_that_breaks_syntax`、`test_an_already_broken_file_can_still_be_edited` |
| 25 | **「没验证」不等于「通过」** | 识别不出测试命令时原先是 `not_configured` → 路由判为非 failed → **审计记成 `ok=True`**，于是"没做"与"做对了"在账上分不开（P0-2）。现在：① 有源码就做**语法解析级降级验证**（`kind=syntax`），② 事件与审计的 `ok` 都是**三态**（True/False/**None=没验证**） | `test_a_python_project_without_tests_falls_back_to_a_syntax_check`、`test_the_audit_records_not_configured_as_unverified_not_passed`、`test_verification_ok_is_three_state` |
| 24 | **空步骤的重做有界，且必须给退路** | 只读步骤是合法的（计划第一步常常就是探索）。无限要求"必须有改动"会逼出无意义的改动，所以只给一次机会，且提示里明确写了"这一步确实只需只读就说明理由停下" | `test_the_nudge_happens_only_once_per_step`、`test_nudge_tells_the_model_why_it_is_back` |
| 27 | **`delegate_explore` 子智能体只能绑定只读白名单工具；implement worker 只能绑定文件/检索白名单** | 子智能体输入包含仓库内容，不能让它获得写/执行能力。构造时校验工具集合，运行时再按名字二次校验；委托调用本身仍走 `approval_gate → tools` 唯一咽喉。编排 implement worker 是另一条受控路径，构造时断言不含 shell/git/deps/test（见 32） | `test_runner_refuses_non_readonly_tools_at_construction`、`test_non_whitelisted_tool_call_is_refused` |
| 28 | **主 agent 的并发只发生在只读子智能体之间；编排并发由 scheduler 统一管理** | 多个 delegate 同批并发；主 agent 的普通工具（含写）仍串行。编排路径由 scheduler 的 ready 堆与信号量控制并发，任务完成即解锁依赖，失败立即短路。写操作与探索混在同一条消息时，提示词要求模型拆开，避免读到改动前的状态 | `test_multiple_delegate_calls_run_concurrently`、`test_concurrent_batch_respects_the_task_budget`、`test_dependent_starts_as_soon_as_its_predecessor_finishes` |
| 29 | **`implement` 步骤必须有实际改动才能推进** | 委派探索可以把「读」做完，但不能因此把实现步当成完成。`plan_kinds` 与 `plan` 逐项对齐：explore/verify 允许零改动直接步进；implement 先 nudge 一次，仍无 `dirty` 就收尾，不再静默 advance | `test_an_implement_step_nudged_without_changes_responds`、`test_an_explore_step_without_changes_advances_without_nudge` |
| 30 | **同一失败签名连续出现就改走 replan，且跨 replan 继续累积** | 24 次相同失败验证说明 repair 只是重复同一类修改。失败签名由失败测试 id + 归一化错误消息组成；连续达到 `max_same_failures` 后不再 repair。replan 会清空 `verification`，所以签名另存 `last_failure_signature`：新做法若仍撞同一个失败，计数继续增长，不会「清零→再修两轮→再 replan」循环。repair 时只重跑上次失败的 pytest 测试，子集通过后自动补跑全量。 | `test_the_same_failure_signature_goes_to_replan`、`test_stagnation_survives_a_replan_that_clears_the_verification`、`test_selected_pass_is_confirmed_by_a_full_run` |
| 31 | **委派只在 explore 步、且受仓库规模预算约束** | 38 文件的任务实测会拆 3–5 个子智能体，成本 +43%。默认：<30 文件不委派；30–99 最多 2 次；100–299 最多 4 次；≥300 用配置上限。实现/验证步一律拒绝委派 | `test_small_repo_disables_delegation`、`test_delegate_is_rejected_outside_an_explore_step`、`test_delegate_budget_comes_from_the_cost_policy` |
| 32 | **编排写 worker 只能在隔离 worktree 中并发，且写范围在 I/O 前就被校验** | 多个 implement 同时改主工作区会互相覆盖。每个 worker 一个 worktree；scheduler 串行执行 `git apply --check` → 留快照 → `git apply`；文件工具在写入前拒绝 `write_scope` 外路径，事后 diff 校验保留为第二道防线 | `test_concurrent_implement_workers_ignore_agent_state`、`test_merged_patch_takes_snapshots_so_undo_can_restore`、`test_dispatch_marks_a_conflicting_patch_failed`、`test_write_outside_scope_is_rejected_before_io` |
| 33 | **执行环境异常不等于代码失败** | WSL 掉线/命令无法启动时，`verify` 记 `error` 并直接收尾，`review` 记 `warned` 且不推进水位线；把这类错误送进 repair 只会让模型徒劳地改代码，或让整轮无谓失败 | `test_environment_failure_is_reported_not_fatal`、`test_an_environment_failure_is_warned_and_does_not_advance_watermark`、`test_verification_environment_error_goes_straight_to_respond` |
| 34 | **崩溃后必须修复悬空的 tool_calls** | 崩溃可能发生在 act 已提交、tools 未提交之间；同一 thread 重跑时若把新消息追加在未应答的 tool_calls 后，下一轮 API 会因配对不完整直接 400。runtime 为缺失的调用补占位 `ToolMessage`，并明确标注“结果未知” | `test_recover_adds_a_placeholder_for_each_unanswered_call`、`test_dangling_tool_calls_are_repaired_before_the_new_prompt` |
| 35 | **只重试瞬时/环境错误，且总等待有上限** | 对语义错误重试同一请求没有意义，对致命配置错误重试只是烧钱；无限退避会让一次运行永远挂住。`recovery.classify_error` 把异常压成四类，`retry_async` 只对前两类做指数退避 + 抖动 | `test_retry_async_does_not_retry_a_fatal_error`、`test_retry_async_stops_at_the_total_wait_budget`、`test_act_retries_a_transient_llm_error` |
| 36 | **写文件必须原子替换，并对瞬时占用做短重试** | 直接 `> file` 在进程被杀/磁盘满时会留下半截文件；`EBUSY/ETXTBSY` 往往只是编辑器/构建工具短暂持有文件，立刻报错会让模型做无意义的“修代码”。同目录临时文件 + `rename` 保证读者看到完整内容 | `test_write_uses_a_temp_file_and_atomic_replace`、`test_write_retries_on_busy_file`、`test_write_does_not_retry_a_semantic_failure` |
| 37 | **模型读到写之间文件被外部改过时不覆盖** | 否则 `file_write` 会静默丢掉编辑器/另一个进程的改动。只拿模型自己的读取缓存做乐观比较；没有读取记录不猜 | `test_changed_since_read_detects_external_modification`、`test_overwrite_is_rejected_when_file_changed_since_read` |
| 38 | **checkpoint 要能承受并发，且中断必须可识别** | 多前端共享 SQLite 时，默认 journal 模式容易 `database is locked`；WAL + busy_timeout 降低冲突。`run_status` 留在 checkpoint 里，下一次同 thread 运行才能判断上一轮是否异常中断，并把“可能有未验证改动”交给模型 | `test_sqlite_uses_wal_and_busy_timeout`、`test_interrupted_run_produces_a_recovery_notice`、`test_finished_run_does_not_produce_a_recovery_notice` |
| 39 | **有副作用的工具必须可检测“结果未知”，不能盲目重放** | 工具已执行、checkpoint 未记录时重放会造成第二次副作用（重复 commit / shell 写入）。日志按 `(thread_id, call_id)` 记录 started/finished；只有 started 时明确回灌“结果未知”并强制验证 | `test_a_side_effect_with_only_started_is_not_replayed`、`test_a_finished_side_effect_is_replayed_without_executing`、`test_a_side_effect_execution_is_recorded` |
| 40 | **运行与验证各有独立时限** | `recursion_limit` 只管步数，管不住模型长时间卡住；验证命令通常比普通 shell 长，不能共用 60s。总时限跨 resume 累计，验证超时单独配置 | `test_run_deadline_stops_a_stuck_graph`、`test_verification_uses_the_dedicated_timeout` |
| 41 | **WSL 传输故障要熔断，但不能把命令失败算进去** | WSL 掉线时每个工具调用都拉起一个注定失败的 `wsl.exe`，会把一次运行拖成几分钟；连续传输失败后应 fail fast。命令退出码非零只说明用户命令失败，必须重置/保持闭合。冷却后只放一个半开探测，避免恢复瞬间又打满 | `test_transport_failures_open_the_breaker`、`test_normal_command_failures_do_not_open_the_breaker`、`test_half_open_probe_closes_the_breaker` |
| 42 | **worker / 工具 / WSL / LLM 并发由同一套资源池治理** | 各层独立限流会让高层在低层饱和时继续准入，优先级也传不下去。统一池让 scheduler、tools、sandbox、router 共享上限，获取槽位必须有超时，避免线程池被拖死 | `test_async_pool_limits_and_times_out`、`test_governor_limits_concurrent_llm_calls`、`test_sync_pool_times_out` |
| 43 | **调度要工作守恒：边界只管返回时机，预算不足不能全阻塞** | `checkpoint_every` 同时当启动上限会让 6 任务图先跑 4 个再空等；固定角色 token 预留会让大任务把小任务一起拖死。边界只按完成数返回；预算用 EWMA + next-fit 选择可容纳任务 | `test_checkpoint_boundary_does_not_cap_launches`、`test_observed_usage_lets_a_small_task_fit_where_static_reserve_would_not` |
| 44 | **低优先级任务要被 aging，探针要有保留槽** | 固定角色优先级会让 explore 在 implement 潮水下永久等待；WSL 长命令会把健康探针一起堵死。ready 队列按等待时间提升优先级；WSL 池保留 1 个槽只给 `reserved=True` 的探针 | `test_aging_raises_priority_but_does_not_beat_implement`、`test_reserved_slot_stays_available_for_probes` |
| 45 | **写批次要有合并背压，且只等待真正在跑的 implement** | 无上限地启动写 worker 会让补丁/快照堆积。在飞 + 待合并超过阈值时暂停新 implement；`has_active_implement` 只认 `in_flight`，否则待启动任务会让合并队列永远不 flush，收尾时还会整批丢弃 | `test_merge_backpressure_limits_concurrent_implement`、`test_merge_queue_only_waits_for_in_flight_implement_workers` |
| 46 | **同一工作区只能有一个运行中的 runtime** | 两个进程会同时拉起 WSL、同时改文件/合并补丁，快照与 review 水位线互相污染。用审计目录下的工作区哈希锁做进程内 + OS 级互斥 | `test_workspace_lock_blocks_a_second_runtime`、`test_same_workspace_is_exclusive` |
| 47 | **LLM 自适应并发要跨进程收敛** | 同一台机器上多个 runtime 各调各的，会在 429 恢复时一起打满。AIMD 结果写入现有共享限流文件，其他进程在下一次调用前同步并 clamp 本地池 | `test_concurrency_limit_is_shared_across_processes`、`test_aimd_shrinks_on_429_and_recovers_on_success` |
| 48 | **WSL 弹性并发只在饱和时依据 p95 延迟调整** | 非饱和时命令本身就慢（构建/测试），据此降并发会把正常负载误判成争用。只有 `in_flight >= limit` 时才按 p95 高/低于目标调整，且不抢占在飞命令 | `test_saturation_with_high_latency_lowers_the_limit`、`test_low_latency_recovers_toward_the_ceiling` |
| 49 | **常驻 shell 默认开启，但任何会话异常都必须退回一次性执行** | 一次性 `wsl.exe` 每次约 0.2s，是工具延迟的主要来源；常驻会话把读/检索降到约 20ms。会话破裂/超时/未返回结束标记时必须禁用它并回退，不能把稳定性押在一个长连接上 | `test_persistent_shell_reuses_one_process`、`test_persistent_shell_timeout_does_not_poison_the_next_command`、`test_non_wsl_test_cannot_execute_real_sandbox_command` |
| 50 | **文件写/编辑的读与写各只付一次沙箱往返** | 原先 stat + 读 + 写留底 + 写目标要 4 次进程启动。`read_for_update` 一次拿状态与内容，`write_with_snapshot` 一次写留底与目标；写盘仍是 temp+rename | `test_write_uses_a_temp_file_and_atomic_replace`、`test_overwrite_backs_up_previous_content` |
| 51 | **移动/复制/删除/建目录必须走受控文件工具，而不是 shell** | shell 的 `mv`/`rm` 是 L2 且不留快照、不过 review，直接破坏“可回滚”。文件工具版本带 write_scope、留底、diff 与审计；删除需要确认 | `test_move_renames_and_keeps_a_source_snapshot`、`test_delete_can_be_restored_from_the_snapshot`、`test_new_file_tool_levels` |
| 52 | **读缓存必须有字节上限，且只缓存模型可能复用的内容** | 长会话/大仓库里普通 dict 会无限增长。`ReadCache` 按字节做 LRU，超大单值直接不缓存；统计命中率供 run 指标 | `test_read_cache_evicts_least_recently_used_by_bytes`、`test_read_cache_skips_values_larger_than_the_limit` |
| 53 | **批量编辑必须“全量校验后一次落盘”** | 逐条编辑中途失败会留下半批修改，模型难以恢复。`file_edit_many` 先在内存中应用全部编辑、统一语法/陈旧/scope 校验，再用一次 `write_many` 落盘；任一失败整批不写 | `test_edit_many_applies_to_multiple_files_in_one_call`、`test_edit_many_rejects_the_whole_batch_on_a_bad_match` |
| 54 | **每个工具调用都要有统一耗时；重复探测要有 TTL** | 只靠 shell 的 duration 无法定位文件/检索工具慢在哪；AST 索引与包管理器探测每次重跑是纯浪费。tools 节点统一测 `duration_ms`，索引/探测按 TTL 缓存 | `test_selected_command_appends_a_safe_pytest_nodeid`、`test_read_cache_clear_and_stats` |
| 55 | **等待合并的 implement 必须离开 pending** | 一个 implement 完成、另一个还在跑时，前者会停在 merge 队列。若状态仍是 pending，`refresh_ready` 会把它重新排进 ready，导致同一任务被重复启动、同一补丁被重复加入批次，合并时第二次 apply 失败并整批回滚（实测 4 个 worker 全部失败） | `test_completed_implement_is_not_relaunched_while_waiting_for_merge` |
| 56 | **worktree 内的快照目录被 gitignore 时，`git add` 的 ignored-path 警告不是失败** | 快照写在 worktree 的 `.agent/`，它通常被 `.gitignore` 忽略；`git add -A` 会以 exit=1 报警告，但真实改动已暂存。直接返回“无改动”会让所有 implement worker 失败 | `test_implement_worker_creates_a_clean_patch`（setup 含 `.gitignore`） |

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

### 为什么 nudge 后的实现步强制一次工具动作

实测 deepseek-chat 在 implement 步偶尔会连续返回「无 tool_calls」的空动作：
文件已经读过了，但就是不落笔。`act` 在被 nudge 之后的**第一次**调用上使用
`tool_choice="required"`，至少强制出一个动作（通常是先读目标文件）；后续调用
恢复自动选择，留出编辑机会。provider 不支持强制 tool_choice 时回退到普通调用。

### 为什么多步任务只在最后验证

全量测试覆盖的是**整个功能**。计划第 2 步只做了模型/服务层时，报表与审批的
测试必然还是红的 —— 每步都跑全量，agent 会卡在第 2 步反复 repair，永远到不了
后面的实现步骤（实测 `long/discount_pipeline` 就是卡在这里）。

所以多步计划里，`kind=implement` 的非最终步骤不跑全量；只在 `kind=verify`
或最后一步验证。单步任务（也是最后一步）照常验证。
**`kind=verify` 的步骤即使 `dirty=False` 也会跑** —— 它的职责就是全量确认，
不应因为「这一步没有新改动」被短路。
`AGENT_VERIFY_ONLY_FINAL=false` 可以恢复旧行为。

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

## 8. Orchestrator（独立子图 + 事件驱动 DAG）

> 完整方案与阶段拆解见 [HARNESS.md](HARNESS.md)。

编排层是父图中的一个**独立子图**，默认启用；小任务仍由 runtime 选择
`direct` 快速路径：

```
START ─ route_entry ─┬─ direct → planner → act ...
                     └─ orchestrated
                          └─ orchestrator 子图
                               survey → select_and_plan → scheduler ⇄ steering_dispatch
                                                              ↓
                                                            fan_in → planner
```

- `orchestrator/state.py`：`OrchestratorState` 窄契约，只传 request/cwd/
  repo_profile 与编排结果，不复制父子图的 messages；
- `orchestrator/subgraph.py`：独立子图组装；父图用 `subgraphs=True` 消费内层
  更新与 worker custom 事件；
- `orchestrator/scheduler.py`：事件驱动 DAG（ready 堆 +
  `asyncio.wait(FIRST_COMPLETED)`），完成即解锁依赖、失败立即短路；
  `checkpoint_every` 到达边界时 drain 在飞任务再返回，交给
  `steering_dispatch` 消费 pause/add/replan/cancel；
- `orchestrator/merge.py`：implement 补丁串行合并（check → 解析路径 → 留快照 →
  apply），统一由 `merge.py` 实现；
- `orchestrator/artifacts.py`：fan-in 完整结果落 `.agent/artifacts/`；
  `worker_brief` 给 planner/act，`orchestration_handoff` 保留结构化字段与
  artifact 路径，主 agent 可按需回读；
- `orchestrator/scheduling.py` / `merge_queue.py`：限流退避、token 预算与
  补丁合并队列从 scheduler 主循环中拆出，便于单测；
- `concurrency.py`：worker / 工具 / WSL / LLM 共享的资源池；LLM 按
  provider + 全局双层限流，并根据成功/429 做 AIMD；
- `orchestrator/workflow.py`：规则优先；规则不确定时一次结构化调用同时返回
  workflow + 任务图，避免第二次规划往返。

**写路径**：`implement` worker 仍在独立 git worktree 中修改，导出 patch 后由
宿主串行合并。`write_scope` 现在有**写时校验**：文件工具在 I/O 前拒绝越界路径；
事后 diff 校验保留为第二道防线。写编排要求 `--write`、git 仓库根、干净工作区；
`.agent/` 与 `__pycache__/` 不算用户脏改动。

**Steering**：`runtime.steer()` → `SteeringController`（按 thread 隔离）。
`cancel` 在 worker 轮次/工具边界协作式轮询；`pause/add/replan` 只在 scheduler
drain 后的边界消费。pause 用 `interrupt()` + checkpoint，恢复时已完成任务
不会重跑。

**权限与稳定性**：worker 仍只能通过 `WslSandbox` 执行；implement worker 只绑定
文件/检索白名单，不绑定 shell/git/deps/test。`WslSandbox` 增加并发命令信号量与
探测缓存锁；所有 worker 工具调用、合并、超时、scope 违规、取消均写审计。

## 9. 扩展点

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
唯一例外是 `scheduler` 的 `get_stream_writer()` custom 事件（worker 实时进度），
它只推事件载荷，审计仍由 runtime 统一写。

第 4 与第 7 步都有守卫：`test_graph_event_coverage.py` 用 AST 断言「每个图节点要么
有事件翻译、要么在 `SILENT_NODES` 里显式登记」，漏翻译会当场变红；前端漏映射则
**不报错，只是时间线上少一行**，所以那一步得自己盯着。

### 加一个前端

1. 只依赖 `runtime.AgentRuntime` 与 `events`
2. 消费 `AsyncIterator[Event]`，不认识 LangGraph
3. **不要导入 `tools/` 或 `sandbox/`**（架构测试会拦）

参考 `tui/app.py` 的事件映射表，或 `web/app.py` 里 `model_dump_json()` 直接当 SSE 帧的写法。
