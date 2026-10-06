# 第二轮缺陷审计：功能 / 性能

> 审计方式：在**第一轮修复之后**的代码上重新逐文件精读（含本轮新改的
> `wsl_exec.py` / `testrun.py` / `files.py` / `snapshots.py`），并交叉验证调用链。
> 基线：`pytest -m "not wsl and not llm and not wsl_env"` → **791 passed, 0 failed**。
>
> 与上一轮一样：**测试全绿不等于没有缺陷**。下面 8 项里没有一项会让现有测试变红 ——
> 它们要么在"单测覆盖不到的系统级接缝"上，要么是纯粹的浪费（不影响正确性，只影响等待）。
>
> **C1–C8 全部已修**（见第五、六节）：**823 passed, 0 failed, 1 skipped**。
> 较本轮审计时的基线 791 **+32**；较第一轮修复前的 762 **+61**。

---

## 零、先更正上一轮的一处记述

上一轮 `docs/BUG_AUDIT.md` 记 B9 的收益是「冷启动 `wsl.exe` **9 次 → 2 次**（≈2.7s → ≈0.6s）」。
**这个数字写大了。** 真实是 **9 → 4（≈2.7s → ≈1.2s）**，原因见 C1：进程里有**三份**
`WslSandbox` 实例，而缓存挂在实例上、不跨实例。已同步修正上一轮文档的记述。

教训值得记一笔：**缓存的粒度决定收益的边界**。加缓存时只数了"调用点"，没数"持有者"。

---

## 一、缺陷清单

| # | 级别 | 位置 | 问题 | 影响 |
|---|---|---|---|---|
| **C1** ✅ | **P1 性能** | `registry.py:34`、`build.py:131`、`runtime.py:146` | 一次运行里存在 **3 份 `WslSandbox`**，探测缓存不跨实例 | ~~冷启动 4 次 `wsl.exe` ≈ 1.2s（而非 1 次）~~ → 已修，见第六节 |
| **C2** ✅ | **P1 性能** | `files.py:144 / 264 / 357` | `fs.resolve()` 与随后的 `read_text()` 各启动一次进程，而后者**已包含路径校验** | ~~每次 `file_read` / `file_restore` 白花 1 次启动（≈0.3s）~~ → 已修 |
| **C3** ✅ | **P1 提示正确性** | `files.py:154` | `_read` 对**任何** `SandboxFsError` 都追加「请用 offset/limit 分段读取」 | ~~文件不存在 / 二进制时也这么说，把模型往错误方向带~~ → 已修 |
| **C4** ✅ | **P1 上下文** | `llm/context.py:159-185`、`files.py:76` | `trim_messages` **永不裁剪最近 `keep_recent` 条**，而 `file_read.limit` **无上限** | ~~单次读取可注入 2MB（预算 33 倍）~~ → 已修（两层：`limit` 上界 + 单条消息硬顶） |
| **C5** ✅ | **P2 性能** | `audit/logger.py:79, 86-102` | 每写一条审计都 `glob` 一次目录 + `mkdir` | ~~长任务上百条记录 = 上百次目录扫描~~ → 已修，见第六节 |
| **C6** ✅ | **P2 功能** | `memory/sessions.py:55` | 会话索引按**文件数**回溯（30 个），轮转片会吃掉配额 | ~~启用 `AGENT_AUDIT_MAX_MB` 后，可回溯天数缩水，旧会话静默消失~~ → 已修 |
| **C7** ✅ | **P1 正确性** | `graph/nodes/tools.py:190` | `artifact.get("ok") and _mutates_workspace(...)` —— "是否改动"与"是否成功"耦合 | ~~shell 改了文件但非零退出时不置 `dirty` → 跳过验证~~ → 已修（上一轮 B8 的遗留） |
| **C8** ✅ | **P2 资源** | `runtime.py:156-162`、`wsl_exec.py:252`、`act.py:75` | 三个按 thread_id 无界增长的字典；超时后 `communicate()` 无 timeout；异步图里用同步 `llm.invoke` | ~~长会话内存累积；理论可永久挂住；Ctrl-C 无法取消~~ → 已修（沿用 B10 / B14） |

---

## 二、逐项详述

### C1 · 一次运行里有三份沙箱，缓存不跨实例

三处各自 `new` 一个 `WslSandbox`：

| 持有者 | 位置 | 用途 |
|---|---|---|
| `AgentRuntime._sandbox` | `runtime.py:146` | `workspace` / 快照 / 记忆 |
| `build_tools()` 内部 | `registry.py:34` | 6 个工具构造器共用（这一份内部是共享的 ✅） |
| `make_verify_node(WslSandbox(settings))` | `build.py:131` | 只有 verify 用 |

`cached_probe` 挂在**实例**上（这是刻意的，见 `wsl_exec.py` 注释：不能跨运行串味），
于是每份实例各探一次 `$HOME`。默认配置（`AGENT_WSL_WORKSPACE` 为空）下的冷启动账：

```
改动前：6 个构造器各 1 次 + rg 探测 1 + verify 1 + runtime 1      = 9 次 ≈ 2.7s
改动后：build_tools 的 home 1 + rg 1 + verify 的 home 1 + runtime 1 = 4 次 ≈ 1.2s
理论最优（三份合一）                                              = 2 次 ≈ 0.6s
```

**修法**：`build_graph(..., sandbox=...)` 接受外部沙箱，`AgentRuntime._ensure_graph()`
把自己的 `self._sandbox` 传进去；`build_tools` 也复用它。改动约 10 行，且能让
`bwrap_available()` 的探测也从 3 次降到 1 次。

> 附带：`web/app.py:109` 每个 `/api/run` 新建 `AgentRuntime`，因此每请求重付这 1.2s，
> 并额外开合一次 sqlite 连接。合并沙箱是这一项的前提。

### C2 · 文件工具的冗余 `resolve`

`SandboxFs.read_bytes`（`fs.py:176`）在**一次**进程启动里完成：路径词法校验 +
realpath 校验 + 存在性 + 类型 + 大小 + 取内容。注释写得明白："一次进程启动搞定全部"。
但工具层在这之前又调了一次 `fs.resolve()`（一次纯 I/O）：

| 工具 | 当前启动次数 | 实际需要 |
|---|---|---|
| `file_read` `files.py:144 → 151` | 2 | **1**（`read_text` 自带完整校验，且给出一致的错误） |
| `file_edit` `files.py:264 → …` | 4–5 | 3–4 |
| `file_restore` `files.py:357 → 358` | 2 | **1**（且这里只是要归一化路径，词法归一化即可，无 I/O） |

`file_restore` 那处尤其冤：`resolve()` 只为拿到规范化后的路径去比对快照列表，
用 `ensure_inside()`（纯 Python、零 I/O）就够。

### C3 · `_read` 的错误提示无条件追加「请用 offset/limit」

`files.py:154`：

```python
except SandboxFsError as exc:
    return _error(target, "read", f"{exc}请用 offset/limit 分段读取。")
```

而 `fs.read_text` 抛的错至少有五种：**文件不存在**、**不是普通文件**、**文件过大**、
**二进制文件**、**非 UTF-8**。只有"文件过大"才与 offset/limit 有关。

也就是说，模型读一个拼错的路径会被告知「文件不存在：xxx。请用 offset/limit 分段读取。」
—— 它会照做，然后拿着更大的 offset 再读一次，白烧一轮工具预算。
这条正好是 B4 里"提示只说事实、不说办法"原则的**漏网之鱼**：那次只改了 `fs.py`，
`files.py` 这个拼接点没一起改。

### C4 · 上下文预算对"最近消息"完全无效

两个事实叠加：

1. `trim_messages`（`context.py:167`）的裁剪条件是 `index >= cutoff … continue` ——
   **最近 `keep_recent`（默认 12）条消息一律原样保留**，降档那一轮也一样。
   所以"预算"只约束历史，不约束当下。
2. `file_read` 的 `limit` 参数只有 `ge=1`（`files.py:76`），**没有上限**。
   `fs.read_text` 的上限是 `max_file_read_bytes`，默认 **2,000,000 字节**。

于是模型只要传 `limit=100000`，一次 `file_read` 就能把 2MB 灌进上下文 —— 是
`context_max_chars`（60,000）的 **33 倍**，而它属于"最近消息"，任何裁剪都不会碰它。
默认不传 `limit` 时是 `DEFAULT_READ_LINES = 400` 行，通常安全；但 400 行 × 长行
（minified JS、生成代码）同样可以轻松超过 60k。

后果不是崩溃而是**账单与 400**：超长 prompt 直接推高 token 成本，逼近模型上下文上限时
provider 返回错误，而这个错误会以 `RunFailed` 的形态出现，看上去像"模型坏了"。

**修法**（任选，成本都很低）：
- 给 `ReadInput.limit` 加上限（如 `le=2000`），让它与 `context_tool_chars` 同量级；
- 对"最近消息"里超过 `tool_chars` 的 ToolMessage 也做硬裁剪（保住不变量：
  **只截断内容、绝不丢消息**，所以仍然安全）；
- 或把 `max_file_read_bytes` 调到与上下文预算同量级（会削弱读大文件的能力，不推荐）。

### C5 · 每写一条审计就扫一次目录

`AuditLogger.write()` 每次都走 `_active_path()` → `files_today()` → `directory.glob()`
（`logger.py:79`），外加一次 `mkdir(parents=True)`。
一个 5 步任务轻松产生上百条记录（每条工具调用 2 条、每步验证 1 条…），
就是上百次目录扫描 + 上百次 mkdir，全部是纯浪费 ——
目录在一天之内不会自己长出新的天文件。

**修法**：缓存"当天的活跃片路径"，跨天或轮转时失效。注意 `files_today()` 是
**给外部用的**（TUI `/audit` 要列出当天所有片），不能把它本身缓存掉，
要缓存的是 `_active_path()` 的结果。

### C6 · 会话索引按"文件数"回溯

`SessionIndex._audit_files()` 取 `sorted(glob("*.jsonl"))[-30:]` —— 参数是**文件数**，
注释却写"只扫最近这么多**天**"（`sessions.py:18`）。默认 `audit_max_mb=0`（不轮转）时
两者等价；一旦启用轮转，一天会产生 `<date>.1.jsonl`、`<date>.2.jsonl`…，
30 个文件可能只覆盖几天，旧会话**静默消失**（列表里查不到，但审计文件还在）。

顺带一个排序瑕疵：`"2026-10-05.1.jsonl" < "2026-10-05.jsonl"`（`'1' < 'j'`），
所以同一天的轮转片会排在首片**之前**。`list()` 用的是 max/min 时间戳，结论不受影响，
但读起来是反的；若将来有人按文件顺序做增量扫描就会踩到。

### C7 · "是否改动"与"是否成功"耦合（沿用 B8）

```python
# graph/nodes/tools.py:190
if artifact.get("ok") and _mutates_workspace(name, args):
    dirty = True
```

shell 命令**改了文件但以非零码退出**时（`sed -i` 成功但后续命令失败、
`gcc` 生成了产物却编译报错），`ok=False` → 不置 `dirty` → 本步**不跑验证**。
"工作区被改动了"和"这次调用成功了"是两件事，应当解耦：
`_mutates_workspace` 判的是前者，不该被后者的结果否决。

### C8 · 资源与健壮性（沿用 B10 / B14）

- `runtime.py:156-162`：`_pending_calls` / `_progress` / `_resume_counts` 按 thread_id
  无界增长，任务结束不清空。长会话 TUI 会持续累积；`_pending_calls` 还可能跨任务串号。
- `wsl_exec.py:252`：`proc.kill()` 之后 `communicate()` **没有 timeout 参数**，
  理论上可永久挂住（外层已有超时，但清理这一下没有兜底）。
- `act.py:75` / `respond.py:70`：异步图里用同步 `llm.invoke`，靠 LangGraph 线程池执行，
  **无法取消**，LLM 调用期间占满一个线程，Ctrl-C 不生效。

---

## 三、建议动手顺序

| 顺序 | 编号 | 改动量 | 理由 |
|---|---|---|---|
| 1 | **C3** ✅ | ~3 行 | 提示错误会直接浪费模型轮次，改法无争议 |
| 2 | **C1** ✅ | ~10 行 | 让上一轮的缓存真正发挥到边界：1.2s → 0.6s，且省掉重复的 bwrap 探测 |
| 3 | **C2** ✅ | ~15 行 | 每次文件操作省 0.3s，是 TUI 里最容易被感知的一档 |
| 4 | **C4** ✅ | ~15 行 | 挡住 33 倍的超预算注入（`limit` 上界 + 单条消息硬顶，两层都要） |
| 5 | **C5 / C6** ✅ | ~15 行 | 都是"只在特定配置下才发作"，但改法清晰 |
| 6 | **C7 / C8** ✅ | ~20 行 | 正确性收益真实但触发路径较窄，可攒成一次清理 |

八项全部修完（见第五、六节）。

---

## 四、与上一轮的关系

| 上一轮 | 状态 |
|---|---|
| B1 / B2 / B5 / B7 / B3 / B4 / B9 / B13 | 已修，测试覆盖到位 |
| B6（call_id 缺失时审计丢参数） | **仍未修**，触发路径窄（fail closed，安全上无害） |
| B8（= 本轮 C7） | 已修 |
| B10 / B12 / B14（= 本轮 C8） | 已修 |
| B9 的收益记述 | **本轮更正**：9 → 4 次，不是 9 → 2 次 |

---

## 五、已修复：C1–C4

验证结果：`pytest tests/unit tests/tui -m "not wsl and not llm and not wsl_env"`
→ **804 passed, 0 failed, 1 skipped**（较本轮基线 +13 条）。

### C3 · 超限变成可区分的失败类型

`fs.py` 新增 `SandboxFsTooLarge(SandboxFsError)`，`read_bytes` 只在大小越界时抛它。
`files.py` 的 `_read` / `_edit` 各自 `except SandboxFsTooLarge` 才补退路，
其余 `SandboxFsError` 原样透传。

**顺带修掉了 `_edit` 的同款问题**：它此前对任何错误都拼「file_edit 需要整份读出、
精确替换后再写回，改不了这么大的文件」——文件不存在时也这么说。
同一条不变量散在两个文件里（`fs.py` 与 `files.py`），上轮只改了一半。

回归用例：`test_oversized_read_raises_a_distinguishable_error`、
`test_other_read_failures_are_not_reported_as_oversized`。

### C1 · 一次运行只建一份沙箱

- `build_tools(settings, *, allow_write, sandbox=None)`、`build_graph(..., sandbox=None)`
  都可注入沙箱；`runtime._ensure_graph` 传 `self._sandbox`。
- `build_graph` 里 `root = resolve_workspace(settings, sandbox)` **只解析一次**再传给
  `make_verify_node(sandbox, root)`。

**这一步差点引入回归**：verify 原本自己 `new WslSandbox(settings)`，那份 settings 是
`build_graph` 收到的（已写回 `--workspace` 覆盖）。改成共用 runtime 的沙箱后，
沙箱自带的 settings 未必带覆盖，让它自己推会**退回 $HOME**，验证就跑在另一个目录里。
所以 root 必须显式传 —— `test_verify_node_uses_the_root_it_is_given` 守这条。

收益：冷启动探测 **4 次 → 2 次**（`$HOME` 1 次 + `command -v rg` 1 次），
bwrap 探测 3 次 → 1 次。

### C2 · 文件工具不再为同一次读付两次启动

`SandboxFs` 新增 `lexical(path)`：只做 `ensure_inside` 词法归一化，**零 I/O**
（校验逻辑本来就在 Python 侧，不需要沙箱）。`_read` / `_edit` / `_restore`（两处）
改用它——随后的 `read_text` / `latest_for` 会完成 realpath 校验或压根不需要它。

`_restore` 那两处最冤：只为拿到一个展示/比对用的相对路径，却跑了一趟 wsl.exe。

代价：`_read` 现在必须 `except SandboxPathError`——此前符号链接逃逸由 `resolve`
提前挡下，`read_text` 的那次校验不会触发；跳过 resolve 后它会真的抛出来。
不接住就是未捕获异常。

### C4 · 上下文：两道闸门

1. **`ReadInput.limit` 加 `le=MAX_READ_LINES`（2000）**。这是理智闸门，不是保证——
   行长不受控，2000 行仍可能很大。
2. **`trim_messages` 对最近窗口也设单条硬顶**：一条消息超过 `max_chars` 就截到
   `max_chars`。阈值取整个预算是有意的——正常读取（几百行、十几 KB）碰不到它，
   只有真正病态的单条消息才会被截断，不会误伤模型刚拿到的工具结果。

两层都要：只有 `le=` 挡不住长行，只有硬顶则会把离谱的请求先读进内存再截断。

**改了一条编码旧行为的既有测试**：`test_recent_window_is_never_trimmed` 用 5000 字符的
消息配 2000 预算——那正是要修的病态情形。改为在预算内验证同一条不变量，
另加 `test_a_recent_message_within_the_budget_is_untouched` 反向守着。

---

## 六、已修复：C5–C8

验证：`pytest tests/unit tests/tui -m "not wsl and not llm and not wsl_env"`
→ **823 passed, 0 failed, 1 skipped**（本批新增 19 条：C5 四条、C6 三条、
C7 两条、C8 十条）。

### C5 · 审计写入不再每写一条扫一次目录

缓存的粒度是「**当天该写哪一片**」`_active = (day, path)`：

- 判满只做一次 `stat()`，写满才用新增的 `_next_piece()` 推进片号 —— 比扫目录便宜得多，
  而且顺带能兜住"别的进程已经开了更高的片号"（循环继续往后找）。
- 跨天失效：`_active` 里存着 day，对不上就重新扫一次。
- `mkdir(parents=True)` 只做一次（`_dir_ready`）。目录中途被删的话下一次 `open()` 会失败，
  照旧转成 `AuditError` —— 没有削弱"审计有缺口必须显式失败"这条不变量。

**要缓存的是 `_active_path()`，不是 `files_today()`。** 后者是给外部读的（TUI 的
`/audit` 要列出当天每一片），缓存它就会漏片。这条边界写进了两个方法的 docstring。

顺带修掉一个 `NameError` 隐患：`write()` 里 `target` 在 `mkdir` 先失败时尚未赋值，
而 `except` 分支要用它拼错误信息 —— 改成 `target or self.path`。

新增 `audit_files_by_day()` 并从 `audit/__init__.py` 导出：`files_today()` 与 C6 的
会话索引都需要「按片号排序的天文件分组」，同一套解析只写一遍（上一轮吃过亏：
同一条不变量散在两个文件里，改一半会漏）。

回归用例：`test_active_piece_is_not_rescanned_on_every_write`（把 `files_today` 换成
会炸的桩来证明不再扫）、`test_active_piece_advances_when_the_cached_one_fills`、
`test_active_piece_follows_a_new_day`、`test_directory_is_created_once`。

### C6 · 会话回溯按天，不按文件

`DEFAULT_LOOKBACK_FILES` → `DEFAULT_LOOKBACK_DAYS`，构造参数 `lookback_files` →
`lookback_days`；`_audit_files()` 改为按天分组后取最近 N 天（含该天的全部轮转片）。

之前记的那个排序瑕疵（`"day.1.jsonl" < "day.jsonl"`，同一天的轮转片排在首片之前）
由 `audit_files_by_day()` 统一按片号排序解决了 —— 分组 helper 同时服务两处，
这类"读起来是反的"的细节就不会各写各的。

回归用例：`test_lookback_is_counted_in_days_not_files`（一天 3 片 × 2 天，
`lookback_days=2` 必须两边都看到 —— 旧实现只能看到后一天）、
`test_lookback_days_excludes_older_sessions`、`test_pieces_of_one_day_are_all_scanned`。

### C7 · "改动了"与"成功了"解耦

```python
if _mutates_workspace(name, args):
    if name == SHELL_TOOL_NAME or artifact.get("ok"):
        dirty = True
```

**只有文件工具才看 `ok`**：对 `file_write` / `file_edit` 来说 `ok=False` 就是没写成
（路径越界、替换没命中），工作区没变，不该触发验证。shell 则相反 —— 非零退出
不代表没动过文件（`sed -i` 改完了后面的命令才失败、`gcc` 出了产物才报错），
那种情况恰恰**最需要**验证，旧代码却把它跳过了。

`_SHELL_MUTATION_NOTE` 也跟着解耦：改动确实发生了、且没有快照留底，
这与退出码无关，所以不看 `ok` 直接追加。

回归用例：`test_failed_shell_mutation_still_marks_dirty`、
`test_failed_file_edit_does_not_mark_dirty`（反向，防止解耦过头把没写成的也算上）。

### C8 · 资源与可取消性

**(a) 按 thread 累积的状态在收尾时清空。** 新增 `_forget_thread()`，在
RunFinished / RunFailed 的三条出口上调用。

这里改的过程中被既有测试逮到一次：`test_resume_is_capped_per_thread` 变红。
`_resume_counts` **不能清** —— 它封顶的是"同一 thread 反复挂起-恢复"，
跑完一轮就清零等于把护栏拆掉（前端跑完再 resume 就能无限续）。
所以只清 `_pending_calls` / `_progress`（真正持有数据的两个），
计数每 thread 只占一个 int，本来也不是内存问题的所在。

**(b) `kill()` 之后的 `communicate()` 补 timeout。** 新增 `KILL_GRACE = 5`：
kill 只保证 wsl.exe 收到信号，卡在不可中断状态时无参 `communicate()` 会永久挂住 ——
一次已经判定超时的命令，不该在清理阶段把整轮运行钉死。再超时就放弃回收，
`stdout` 记空串（不能是 `None`，下游要 `.decode()`）。

**(c) 调模型的节点改成 async + `ainvoke`。** act / respond / planner / replan 四个
（replan 是这次顺带发现的：它也调模型，原先同样是同步）。

代价与两个陷阱，都值得记下：

1. **LangGraph 的 async 节点不能用 `graph.invoke()` 同步调用** —— 实测报
   `TypeError: No synchronous function provided to "xxx"`。本项目只走 `astream`
   （唯一调用点在 `runtime._stream`），无影响；但这是一条新的隐式约束。
2. 测试桩件全部要补 `ainvoke`。同步测试用 `asyncio.run(...)` 驱动
   （`_sync()` / `_act()` 两个小 helper），没有引入 pytest-asyncio 依赖。

回归用例：四个节点各一条「桩**只**实现 `ainvoke`，退回同步立刻炸」
（`test_*_goes_through_the_async_llm_api`），
状态清理 3 条（含反向的 `test_thread_state_survives_a_suspension`），
超时清理 3 条（`tests/unit/test_sandbox_timeout_cleanup.py`，新增文件）。

---

## 七、仍未修 / 已知遗留

| # | 位置 | 问题 | 为什么这轮没动 |
|---|---|---|---|
| **B6** | `runtime._audit_tool_call` | `call_id` 缺失时审计丢参数 | 触发路径窄，fail closed，安全上无害 |
| **工具仍是同步执行** | `graph/nodes/tools.py:183` `tool.invoke(...)` | shell / file / git 工具是阻塞 I/O，占线程池、无法取消 | 要动到每个工具实现与沙箱层，是独立的一次改造；目前耗时由 `shell_timeout` 兜住，不会永久挂住 |

另外记一条**新引入的隐式约束**：图节点已全部 async（调模型的那四个），
将来任何 `graph.invoke()` 同步调用会直接报 `No synchronous function provided`。
要用同步入口，得给节点同时提供 sync 实现（`RunnableCallable(func=..., afunc=...)`）。
