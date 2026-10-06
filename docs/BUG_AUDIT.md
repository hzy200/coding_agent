# 功能缺陷与 BUG 审计

> 审计对象：`src/coding_agent/` 全部 53 个源文件（含新增的 `replan` 节点与评测 harness）。
> 审计方式：逐文件精读 + 关键路径交叉验证 + 运行 `pytest -m "not wsl and not llm"`（762 用例全绿）。
> **测试全绿不等于没有缺陷** —— 下面列出的多数问题位于「单测正确、但系统级交互未覆盖」的地带。
>
> **修复进度**：B1、B2 已实现并补上回归测试（见文末「六、已修复」）。其余待办。

严重度口径：

| 级别 | 含义 |
|---|---|
| **P0** | 会产生**错误的任务结果**，且用户/模型难以察觉 |
| **P1** | 状态机或事件流**不一致**，影响可观测性与可回滚性 |
| **P2** | 资源浪费、健壮性缺口、误导性反馈 |

---

## 一、P0：会产生错误结果

### B1. planner 静默降级 —— 代码自己记载过的坑，至今未修

**位置**：`graph/nodes/planner.py:56-60`

```python
except Exception:  # 规划失败不能拖垮整张图
    steps = []
if not steps:
    steps = [request or "完成用户请求"]   # ← 整条请求当成一步
```

**证据**：`llm/deepseek.py:20-37` 的注释白纸黑字记着这个故障模式：

> 「planner 把异常静默吞掉、降级成『整条请求当作一步』—— 于是一个**完全失效的规划，
> 看起来和正常工作一模一样**，直到有人去翻审计才发现每次的计划都是请求原文。」

也就是说：**作者已经识别出这个失效模式，但降级逻辑原封不动留在 planner 里**。
结构化输出一旦失败（`with_retry(stop_after_attempt=3)` 也救不回来时），
整个多步工作流退化成单步，而 `StepStarted(total=1)` 让前端显示完全正常。

**为何测试没抓到**：`test_replan.py` / `test_nodes.py` 里的 planner 用例都用 FakeLLM 返回合法 JSON，
没有覆盖「模型返回非法 JSON」与「返回合法但 steps 为空」这两条降级路径。

**修复方向**：降级时不要伪装成正常计划 ——
显式标记 `plan_degraded=True` 进 state，让 `PlanCreated` 事件带上该标记；
前端显示「规划未能解析，已按单步执行」，审计记为 `PLAN_DEGRADED`。
至少让失效**可见**，而不是静默。

---

### B2. replan 额度被成功路径抢占 —— 一步失败会连带放弃后面所有步骤

**位置**：`graph/nodes/replan.py:128-133`，`config.py:126`（`max_replans=2`）

```python
if failed and used >= max_replans:
    return {"plan": plan[:step_idx], "replan_count": used}   # 砍掉剩余全部步骤
if used >= max_replans:
    return {}                                               # 步进入口：沿用原计划
```

`replan_count` 是**任务级**额度（只有 planner 开局清零，见 `state.py:26-28`），
但两种语义完全不同的入口共用它：

- **步进入口**（每步做完后的"微调"）：额度用完只是不改计划，无害。
- **失败入口**（修不动了换做法）：额度用完 = **砍掉剩余步骤、整个任务收尾**。

**后果**：若前两步各做了一次无害的"计划微调"（`replan_count` 已是 2），
第 3 步一旦验证失败修不好，`replan` 不再问模型，直接把剩余步骤全部砍掉。
用户看到的是「任务在第 3 步失败，后面 2 步没做」，而实际上后面那 2 步与失败毫无关系。

这把「重规划」这个刚加上的自主性能力，在最需要它的场景下关掉了。

**修复方向**：拆成两个计数器 —— `replan_count`（失败入口专用，上限 `max_replans`）
与 `tweak_count`（步进入口，可给更大的上限或共用但失败入口优先）。
或者更简单：**失败入口不消费额度时也要有退路**，改为「额度耗尽时先尝试一次不带 LLM 的保守策略」。

---

### B3. 新建文件不留快照 —— 「回滚」承诺存在空洞

**位置**：`tools/files.py:191-206`

```python
original: str | None = None
if info.exists:
    ...
    original = fs.read_text(target, max_bytes=max_bytes)
...
snapshot_id = None
if original is not None:              # ← 新建文件时 original 为 None
    snapshot_id = snapshots.save(target, original)
```

`action="create"` 分支**不产生快照**。之后 `file_restore` 对该文件
`latest_for()` 返回 `None`，回灌「没有任何留底，无法回滚」。

问题在于**契约不一致**：`RESTORE_DESCRIPTION`（`files.py:58-66`）写的是
「把一个文件回滚到之前某次修改前的状态；不传参数则回滚最近一次文件改动」——
模型读到的是「刚做的改动都能撤销」，而新建恰好覆盖不到。
模型按承诺去回滚时才发现做不到，白白多一轮。

**修复方向**：二选一 ——
（a）新建时存一份空内容快照，回滚即清空/删除，语义完整；
（b）在 `RESTORE_DESCRIPTION` 与回灌文本里**明确写出「新建的文件无法回滚」**。
（a）更符合项目"可回滚"的立身之本，但要注意回滚成空文件与删除的语义差别。

---

### B4. >2MB 的文件完全无法修改，且错误提示误导模型

**位置**：`tools/files.py:196-198`、`sandbox/fs.py:194-197`

`file_write` / `file_edit` 都先 `fs.read_text(target, max_bytes=settings.max_file_read_bytes)`
（默认 2MB），超限即抛 `SandboxFsError`，错误文本是：

```
文件过大：{size} 字节（上限 {max}）。请用 offset/limit 分段读取。
```

两个问题：

1. **编辑被彻底阻断**：对 `file_edit` 而言，`offset/limit` 根本不存在这个参数
   （`EditInput` 只有 path/old/new/reason/replace_all），提示让模型去用一它没有的能力。
2. 结果是模型只能改用 `shell`（`sed -i`），而系统提示词明确禁止，
   且经 shell 的改动**没有快照**（`tools.py:70-74` 自己也提示了）。
   于是一个 3MB 的文件只能在不留底的前提下修改 —— 与「可回滚」承诺冲突。

**修复方向**：编辑场景改用「按行定位 + 局部读写」而非整文件读入；
至少把错误提示改成可执行建议（提高 `AGENT_MAX_FILE_READ_BYTES` 或改用 `file_read` 定位后分段重写）。

---

## 二、P1：状态机与事件流不一致

### B5. StepStarted 可能永远等不到 StepFinished

**位置**：`runtime.py:474 / 483`（发出）vs `runtime.py:526`（唯一的结束点）

`StepStarted` 在两个地方发出：`planner` 之后（index=0）、`advance` 之后（index=step_idx）。
`StepFinished` **只在 `act` 分支、且本次没有 tool_calls 时**发出。

`advance → replan` 这条新边上，`replan` 可以把剩余步骤清空
（"剩下的工作其实已经做完了"）。此时 `route_after_replan` 直接送 `respond`，
`act` 再也不跑 —— 于是刚发出的 `StepStarted` 永远没有对应的 `StepFinished`。

**后果**：TUI/CLI 的步骤进度条停在这一步（"进行中"），直到任务结束；
`StepFinished.budget_exhausted` 这类状态也无从上报。属于事件契约的配对性漏洞。

**修复方向**：`replan` 清空剩余步骤时，由 runtime 补发一个 `StepFinished(index=step_idx, text="")`，
或在 `run()` 收尾前统一对未闭合的步骤补发。

---

### B6. call_id 缺失/重复时，审计会丢掉工具参数

**位置**：`graph/nodes/approve.py:85-87` + `runtime.py:523 / 535`

```python
call_ids = [str(call.get("id", "")) for call in calls]
if len(call_ids) != len(set(call_ids)) or "" in call_ids:
    return {"approvals": {call_id: DENIED for call_id in call_ids}}
```

这条 fail-closed 判定本身是对的。但下游的配对会塌掉：

- `runtime.py:523` 以 `pending[started.call_id] = started` 登记，重复/空 id 会**互相覆盖**（后者胜）；
- `tools` 节点回灌 N 条 ToolMessage，`pending.pop(call_id)` 只命中一次，
  其余 N-1 条拿到 `started=None`（`runtime.py:535`）；
- `_audit_tool_call(None, ...)` 落下的记录 `args={}`、`level=""`。

安全上无害（调用全部被拒），但**审计出现空洞** ——
而 `audit/logger.py:5` 的立身原则是「一条悄悄丢掉的记录比一次失败的运行更糟」。

**修复方向**：`pending` 的键改用 `(call_id, 序号)` 或 `id(call)`，保证一一对应；
重复 id 场景在审计里显式记 `args_unavailable=true` 而不是留空。

---

### B7. 验证结果的「ok」口径在审计与事件之间不一致

**位置**：`runtime.py:555` vs `runtime.py:868`

```python
# 审计（:555）
ok=raw.get("status") == "ok"
# 事件（:868）
ok=status in ("ok", "skipped", "not_configured")
```

`VerifyResult.ok`（`tools/testrun.py:84-86`）的口径是后者。
于是 `status="skipped"` / `"not_configured"` 时：**事件告诉前端"通过"，审计记成"未通过"**。
事后对账时两者对不上，而审计本该是可信来源。

**修复方向**：两处统一调用 `VerifyResult.ok` 的同源判定（例如 `raw.get("status") in _OK_STATUSES`），
常量定义在一处。

---

### B8. 「改了工作区却不验证」的漏判

**位置**：`graph/nodes/tools.py:190`

```python
if artifact.get("ok") and _mutates_workspace(name, args):
    dirty = True
```

`ok` 对 shell 来说等价于 `exit_code == 0`。但**改了文件却非零退出**是很常见的
（例如 `pytest` 失败但已生成 `.pyc`、`npm install` 装了一半后报错、
`git add` 部分成功）。此时 `dirty` 保持 `False`，`verify` 直接跳过（`:34`），
这一步就被当成"没改动、无需验证"放行。

**修复方向**：对 shell 而言"是否改动"与"是否成功"是两个维度 ——
`dirty` 应只由 `_mutates_workspace` 决定，不看 `ok`；
`ok` 只在决定是否发送 `FileChanged` 时起作用。

---

## 三、P2：资源与健壮性

| # | 位置 | 问题 | 影响 |
|---|---|---|---|
| **B9** ✅ | `wsl_exec.py:276` `resolve_workspace` | **9 处调用、结果从不缓存**（`home()` 只有 `_bwrap_ok` 做了缓存，说明作者知道这个模式）。调用点：`cli/app.py` ×2、`verify.py:31`、`runtime.py:170`、`files/git/deps/search/shell/testrun` 各 1 | 冷启动约 **9 × 0.3s ≈ 2.7s**；Web 端每个 `/api/run` 新建 `AgentRuntime`（`web/app.py:109`），每请求重付 |
| **B10** | `runtime.py:145-151` | `_pending_calls` / `_progress` / `_resume_counts` 按 thread_id **无界增长**，任务结束不清空 | 长会话 TUI 内存持续累积；`_pending_calls` 还可能跨任务串号 |
| **B11** | `snapshots.py:147,165` | 硬编码 `max_bytes=10_000_000`，与 `settings.max_file_read_bytes`（2MB）口径不一致且无注释 | 快照实际永远 ≤2MB（受 B4 限制），10MB 是死代码 |
| **B12** | `verify.py:31` | `root` 在建图时算出并闭包捕获，而 `state["cwd"]` 就是工作区 | 多一次进程启动；配置与状态可能漂移 |
| **B13** ✅ | `testrun.py:254` | `run_verification` 每次都跑 `detect_test_command`（一次 wsl.exe），项目清单在运行内不会变 | 最坏 5 步 × 4 次验证 × 2 次启动 ≈ **40 次 ≈ 12s** |
| **B14** | `act.py:75` / `respond.py:70` | 异步图里用同步 `llm.invoke`，靠 LangGraph 线程池执行 | **无法取消**；LLM 调用期间占满线程；Ctrl-C 不生效 |

---

## 四、附：仍存在的已知小瑕疵

- `tools.py:88-95`：`_POLICY_DENIED_TEXT` / `_NOT_APPROVED_TEXT` 插在 `_mutates_workspace`
  函数之后，与 `:65-74` 的常量块割裂（ruff 未开 `ARG`/`E402` 相关规则所以不报）。
- `files.py:105` 与 `snapshots.py:70`：`_relpath` 两份完全相同的实现，应移入 `pathguard.py`。
- `wsl_exec.py:252`：超时后 `proc.kill()` 再 `communicate()` **无 timeout 参数**，理论上可永久挂住。
- `config.py:137`：`context_budget` 返回 `Any`，丢类型信息（应改 `TYPE_CHECKING` + 直接标注）。
- `artifacts.py:124`：`unpack` 靠键集合严格匹配判封装，工具若返回恰好含
  `{text, artifact}` 两键的合法 JSON 会被误拆（概率低，但契约脆弱）。

---

## 五、修复优先级建议

| 顺序 | 编号 | 理由 |
|---|---|---|
| 1 | **B1** | 改动最小（约 10 行）、收益最大：让"规划失效"从静默变可见。且是代码注释自证的老坑 |
| 2 | **B2** | 直接关系新加的 replan 能否真正发挥作用，改法清晰（拆计数器） |
| 3 | **B5 / B7** | 事件契约与口径一致性，各约 5 行 |
| 4 | **B3 / B4** | 涉及"可回滚"承诺的边界，需要决定语义（建议先做 B4 的错误提示，成本近乎为零） |
| 5 | **B6 / B8** | 审计完整性与验证漏判，属于正确性但触发路径较窄 |
| 6 | **B9 / B13** | 性能，约 30 行改动省 8~9 秒，与功能缺陷正交，可并行做 |

B1 + B2 + B5 + B7 + B3 + B4 + B9 + B13 已全部修复（见第六节），累计新增 34 条回归测试。
剩余：B6 / B8（审计完整性与验证漏判，触发路径较窄）、B10 / B12 / B14（资源与健壮性）。

---

## 六、已修复

### ✅ B1 — planner 降级可见化

| 文件 | 改动 |
|---|---|
| `graph/state.py` | 新增 `plan_degraded: bool` |
| `graph/nodes/planner.py` | 抛异常与「steps 为空」两条降级路径都写 `plan_degraded=True`；**正常时也写 False**（只写 True 会让上一轮的标记串到本轮） |
| `events.py` | `PlanCreated` 增加 `degraded: bool = False` |
| `runtime.py` | 事件带上 `degraded`，审计 `PLAN` 记录的 detail 写入「规划未解析，已退化为单步执行」 |
| `cli/app.py` / `tui/app.py` | 渲染时显式提示，不再静默 |

新增回归测试（`tests/unit/test_nodes_llm.py`）：
- `test_planner_marks_the_degraded_plan`（经 `test_planner_falls_back_*` 两条断言 `plan_degraded is True`）
- `test_planner_does_not_mark_a_real_plan_as_degraded` —— 守「正常规划必须写 False」

### ✅ B2 — 两个 replan 入口独立计额度

| 文件 | 改动 |
|---|---|
| `graph/state.py` | 新增 `tweak_count`；`replan_count` 改为**仅失败入口**使用 |
| `graph/nodes/replan.py` | `counter = "replan_count" if failed else "tweak_count"`，四个出口统一走 `counter` |
| `graph/nodes/planner.py` | 开局两个额度一起归零 |
| `graph/build.py` | `estimate_recursion_limit` 同步更新：步进微调是独立额度，每步再留 2 个超步 |

新增回归测试（`tests/unit/test_replan.py`）：
- `test_step_tweaks_do_not_consume_the_failure_budget` —— **核心用例**：`tweak_count=2` 已到上限时，失败入口仍须问模型而不是直接砍掉剩余步骤
- `test_failure_revisions_do_not_consume_the_tweak_budget`
- `test_the_two_counters_are_independent`

同步调整：`test_cap_stops_asking_the_model` → `test_step_cap_stops_asking_the_model`（步进入口改看 `tweak_count`），
`test_recursion_limit.py` 的两个推导断言跟进新公式。

### ✅ B5 — 步骤事件严格配对

| 文件 | 改动 |
|---|---|
| `events.py` | `StepFinished` 新增 `cancelled: bool`。**被放弃**与**工具预算耗尽**是两回事：前者根本没试过，后者是试过但轮次用完，混为一谈会让用户误以为模型已经尝试过 |
| `runtime.py` | `_stream` 增加 `step_open` 配对状态（存在 `progress` 里，跨 resume 保留）与两个内部生成器 `_open_step` / `_close_step`；`replan` 砍掉当前这步时就地收口；`RunFinished` / `RunFailed` 之前兜底收口 |
| `cli/app.py` / `tui/app.py` | `cancelled` 渲染为「本步骤未执行完，已放弃」，不再静默 |

三个出口覆盖全部漏点：

1. **`replan` 取消当前步骤**（真正的触发点）：`plan[:step_idx]` 让路由直送 `respond`，`act` 再也不跑 → 在 replan 分支里按 `step_idx >= len(plan)` 判定并收口
2. **异常路径**：图抛错时开着的步骤也要收口，否则前端显示"仍在运行"
3. **收尾兜底**：任何让 `act` 没能跑的路径，都在 `RunFinished` 之前补发

`step_open` 放在 `progress`（按 thread 留存）而不是局部变量 —— 挂起与恢复是两次 `_stream`，存局部会丢掉「上一段还开着一步」这件事。

新增回归测试（`tests/unit/test_runtime_stream.py`）：
- `test_replan_that_cancels_the_current_step_closes_it` —— **核心用例**：完整复现「修复用尽 → replan 放弃 → 收尾」的脚本，断言事件序列严格配对
- `test_open_step_is_closed_before_run_finished` / `..._before_run_failed` —— 两个兜底出口
- `test_a_completed_run_emits_no_extra_step_finished` —— 守"正常路径不能因为加了兜底就多冒一个事件"

同步调整：`test_full_run_event_sequence` 的预期序列补上末尾的 `step_finished` ——
它此前编码的正是"步骤开了头却无人收口"的缺陷行为。

### ✅ B7 — 验证「ok」口径统一

| 文件 | 改动 |
|---|---|
| `runtime.py` | 新增模块级 `_VERIFICATION_BLOCKING_STATUS = "failed"` 与 `_verification_passed(raw)`；审计记录与 `Verification` 事件**都**调用它 |

口径取「是否放行」而非「是否跑过」：`skipped` / `not_configured` 表示**没验证**，
不是**验证失败**，记成失败会让审计虚报问题。这也与路由同源 ——
`route_after_verify` 只在 `status == "failed"` 时拦下任务，两边不一致迟早再漂。

新增回归测试：`test_verification_ok_agrees_with_the_audit_record`（4 种 status 参数化，
断言事件与审计记录结论相同）+ `test_not_running_verification_is_not_a_failure`。

### ✅ B4 — 大文件可以改了，提示也不再误导

根因是**读限的口径错配**：`settings.max_file_read_bytes`（默认 2MB）约束的是
「喂进模型上下文的内容量」，却被用在了 `file_edit` / `file_write` 的**内部读取**
上——那些内容模型根本看不到（它只拿到 diff）。于是 2MB 以上的文件彻底改不了，
而错误又让模型去用 `file_edit` 并不存在的 `offset/limit`，只能退回 `shell`；
经 shell 的改动不留快照，正好绕开「可回滚」这条底线。

| 文件 | 改动 |
|---|---|
| `sandbox/snapshots.py` | 新增 `MAX_OPERATIONAL_BYTES = 10_000_000`，命名并说明它与读限是两个口径（原先是两处硬编码的 10_000_000） |
| `tools/files.py` | `_edit` 与 `_write` 的备份读取改用 `MAX_OPERATIONAL_BYTES`；`_read` 保留读限 |
| `sandbox/fs.py` | 超限提示**只说事实、不说办法**（调用方才知道自己有什么退路） |
| `tools/files.py` | `_read` 的退路写「请用 offset/limit 分段读取」；`_edit` 改写成「file_edit 需要整份读出再写回，改不了这么大的文件。可退回 shell 修改，但经 shell 的改动不留快照、无法回滚」 |

### ✅ B3 — 新建的文件也可回滚

语义决定：**回滚一次新建 = 删掉这个文件**，而不是还原成空文件。
还原成空文件不算回到原状，只会在工作区里留下一堆删不掉的垃圾文件。

| 文件 | 改动 |
|---|---|
| `sandbox/snapshots.py` | `save(..., existed=False)` 额外落一个 `.absent` 标记；`list()` 按后缀过滤掉标记；新增 `was_absent()`；`restore()` 遇标记走删除分支 |
| `sandbox/fs.py` | 新增 `remove()`：只删**普通文件**，目录与符号链接一律拒绝（符号链接可能是逃逸通道），删除前同样做 realpath 校验 |
| `tools/files.py` | create 分支也留底；`RESTORE_DESCRIPTION` 写明新建的回滚结果是删除 |

为什么需要标记：光存一份空内容**无法区分「原来就是空文件」和「原来根本没有这个文件」**，
而两者的回滚结果不同。删除前照旧给当前内容留底，所以这次删除本身也能再回滚。

新增回归测试：
- `tests/unit/test_snapshots.py`（7 条，内存版 fs + 假沙箱，不需要 WSL）—— 核心判别用例
  `test_an_empty_file_is_restored_as_empty_not_deleted`：空文件还原成空文件、新建则删除
- `tests/integration/test_snapshots.py`：`test_restore_tool_deletes_a_created_file`、
  `test_deleting_rollback_can_be_undone`

同步调整（编码了旧行为的既有测试）：
- `test_newly_created_file_has_no_snapshot` → `test_newly_created_file_has_a_snapshot`
- `test_write_creates_file_with_exact_bytes` 的 `snapshot_id is None  # 新建无需备份`
  → 断言有快照

### ✅ B9 — 工作区解析只探一次

| 文件 | 改动 |
|---|---|
| `sandbox/wsl_exec.py` | 新增实例级探测缓存 `_probe_cache` 与 `cached_probe(key, producer)` / `forget_probe(key)`；`home()` 改为走缓存（真正探测的部分抽成 `_probe_home`） |

缓存挂在**实例**上而不是模块级：每个沙箱（也就是每次运行、每个测试）各记各的，
不会跨运行串味。`producer` 抛异常时什么都不记 —— 失败不能缓存成「这个人没有 $HOME」。

冷启动的 `wsl.exe` 次数从 **9 次降到 4 次**（≈2.7s → ≈1.2s）：`resolve_workspace`
在建图时被 6 个工具构造器 + `verify` + `runtime.workspace` 各调一次，默认配置下每次
都要走 `home()` 启动一次进程。

> **更正**：此处原记为「9 → 2 次（≈0.6s）」，写大了。缓存挂在**实例**上，而一次运行
> 里有**三份** `WslSandbox`（`runtime` / `build_tools` / `verify` 各一份），各探一次
> `$HOME`；再加上 `search` 的 `command -v rg`，真实是 4 次。合并沙箱可再降到 2 次，
> 见 [BUG_AUDIT_2.md](BUG_AUDIT_2.md) 的 C1。

Web 端收益最直接：`web/app.py:109` 每个 `/api/run` 新建 `AgentRuntime`，原本每请求
都要重付这 2.7s。

### ✅ B13 — 测试命令只探一次

| 文件 | 改动 |
|---|---|
| `tools/testrun.py` | 探测主体抽成 `_detect()`；`detect_test_command` 按 `test_command:<root>` 走 `cached_probe` |

**只缓存「探到了」，不缓存「没探到」** —— 任务很可能先建目录、后写 `pyproject.toml`
才第一次出现可识别的测试命令；把空结果也记住，验证会一直停在 `not_configured`，
而真实原因只是第一次探测发生在配置出现之前。所以探到空串时调 `forget_probe` 丢掉。

一个运行里验证会跑很多遍（每步一遍、`repair` 之后再来一遍），最坏 5 步 × 4 次 =
20 次验证；现在只有第一次带探测，**省掉约 20 次进程启动（≈6s）**。

新增回归测试：`tests/unit/test_sandbox_probe_cache.py`（7 条，桩掉 `run`，不启动 wsl.exe）
- `test_home_is_probed_once_across_repeated_workspace_resolution` —— 连续 4 次解析只探 1 次
- `test_a_failed_home_probe_is_not_cached` / `test_two_sandboxes_do_not_share_probe_results`
- `test_test_command_is_probed_once_per_workspace` —— 换 root 要重新探（缓存键带 root）
- `test_an_empty_probe_result_is_not_cached` —— **核心用例**，守上面的语义
- `test_an_override_bypasses_the_probe_entirely`
- `test_repeated_verification_probes_only_once` —— 端到端：两遍验证 = 3 次 `run`

同步调整：`tests/unit/test_verify_node.py` 的 `_FakeSandbox` 补 `cached_probe` /
`forget_probe`（桩件刻意不做缓存 —— 它要数的就是 `run` 的调用次数）。

### 验证结果

```
791 passed, 1 skipped, 3 deselected
```

（`test_doctor_reports_environment` 标着 `wsl_env`，需真实 WSL，本机沙箱屏蔽了 `wsl.exe`，
与本次改动无关；用 `-m "not wsl_env"` 排除后全绿。）
