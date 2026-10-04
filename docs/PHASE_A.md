# 阶段 A：缺陷收口计划

> 定位：功能已完整、代码冻结后。本阶段**不动主架构**，只修缺陷、补边界、清工程债。
> 全部改动须满足：`ruff` 干净、全量测试通过、不破坏 [ARCHITECTURE.md](ARCHITECTURE.md) 第 6 节的不变量（现 15 条）。

工作量标记：**S** ≤0.5 天 / **M** ≈1 天 / **L** 2–3 天。

## 0. 背景

本轮完整审查发现并修复了 3 个 P0 与 4 个 P1（见 [§4 已完成](#4-已完成p0--p1)）。
阶段 A 收口**剩余**的缺陷，按"会不会影响正确性/安全/答辩结论"排序。

一条**撤销**：初判 `ulimit -f`（`sandbox/limits.py`）换算口径有误，实测 1 块 = 1024 字节
（`ulimit -f 1` 后文件被截在 1024B），代码里 `ulimit -f {mb*1024}` 换算正确 → **非缺陷，不改**。

## 1. 批次与依赖顺序

| 批次 | 任务 | 依赖 | 建议排期 |
|---|---|---|---|
| 1（风险优先） | A1、A4 | 无 | 第 1–2 天 |
| 2（正确性） | A2、A3 | 无 | 第 3–4 天 |
| 3（工程卫生） | A5 | 无 | 第 4–5 天 |
| 收尾 | A6 | A1–A5 完成 | 第 5 天 |

---

## A1　大文件读取：上限必须在读之前生效　【L】✅ 已完成

**目标**：`max_file_read_bytes` 真正阻止"读入内存"，而非读完再拒。

**现状与根因**：`sandbox/fs.py::read_bytes` 调 `_stat_script(path, include_data=True)`，
脚本无条件下 `echo "DATA=$(base64 -w0 -- "$p")"`，整份内容经 stdout 进入
`subprocess.communicate` 缓冲；Python 侧 `if info.size > max_bytes: raise` 发生在**解码之后**。
实测：100MB 文件 + `max_bytes=1000` → 6.68s、约 133MB base64 常驻内存。

**实施步骤**

1. `_stat_script` 增加 `max_bytes: int | None = None`；`include_data` 分支改为带大小的条件输出：

   ```sh
   if [ -f "$p" ] && { [ -z "<max>" ] || [ "$SIZE" -le <max> ]; }; then
     echo "DATA=$(base64 -w0 -- "$p")"
   fi
   ```

   `<max>` 以整数拼入（非用户可控，无需 quote）。
2. `read_bytes` 传入 `max_bytes`；理顺 "太大未读"（DATA 缺失）与现有 `size > max_bytes`
   分支的先后，确保优先报"文件过大"。
3. 核对 `_parse_stat` 对 `DATA` 缺失的语义，避免"太大"与"空文件"混淆
   （空文件应 `data is None` 且 `size == 0`）。

**测试**

- 集成（`tests/integration/test_file_tools.py`，wsl）：`max+1` 字节 → 报"文件过大"；
  `max` 字节 → 正常读；空文件 → 空内容不报错。
- 单测（新）：断言 `_stat_script(..., max_bytes=1000)` 生成的脚本含大小守卫（防回归）。
- 可选：大文件拒绝耗时 < 2s（宽松阈值，避免 flaky）。

**验收**：`file_read` 遇到超限文件即刻返回错误；内存不随文件大小线性增长；读/写/diff 测试全过。

**完成记录（2026-10-04）**

- `_stat_script` 增加 `max_bytes`，脚本内用 `[ "$size" -le N ]` 守卫，超限不跑 `base64`；`read_bytes` 传入上限。
- 顺带修出**潜在缺陷**：`_parse_stat` 把 `DATA=`（空文件）当成"没取到内容"，导致**空文件读不了**、
  `files.py` 的"（文件为空）"分支是死代码。改为区分「无 DATA 行→None」与「空 DATA→b""」。
- 测试：`tests/unit/test_fs_guard.py`（6 个，非 WSL）+ `test_file_tools.py` 两个行为用例
  （超限脚本不输出 DATA、空文件可读）。
- 实测：100MB 文件在 `max_bytes=1000` 下 **6.68s → 0.40s**，且不再物化 ~133MB base64。
- 结果：单元 `559 passed`（+6），WSL 集成 `file_tools + snapshots` `44 passed`，`ruff` 干净。

---

## A2　`history()` 解耦建图（无 API Key 可用）　【M】✅ 已完成

**目标**：只读 checkpoint 的历史恢复不再要求 API Key 与 WSL 就绪。

**现状与根因**：`runtime.py::history` 走 `await self._ensure_graph()` → `build_graph`
→ `build_llm`（缺 Key 抛 `MissingApiKeyError`）+ `build_tools`/`verify`（需 WSL）。
因此 Web `/api/thread/{id}`、TUI `/switch` 在无 Key 环境直接失败。

**实施步骤**

1. 抽 `async def _checkpointer(self)`：返回 `self._external_checkpointer` 或
   `await self._store.aopen()`；`_ensure_graph` 复用。
2. `history()` 直读：`tuple_ = await saver.aget_tuple({"configurable": {"thread_id": tid}})`，
   取 `tuple_.checkpoint["channel_values"]["messages"]`（None 安全）。
3. 保持现有过滤（仅 HumanMessage / 无 tool_calls 的 AIMessage）。
4. `aget_tuple` 返回 None（无会话）→ 返回 `[]`。

**测试**

- 新单测：`MemorySaver` 搭最小 `StateGraph`，invoke 写入 Human/AI/Tool 消息，
  作为 external checkpointer 传给 `AgentRuntime(Settings(_env_file=None))`（**空 API Key**），
  断言 `await runtime.history(tid)` 返回人/助手文本、跳过工具消息。
- 断言 `history()` 全程**不触发** `build_llm`（monkeypatch 使其抛异常来证明未被调用）。

**验收**：空 Key 下 `/api/thread`、`/switch` 正常；`test_runtime_stream.py` 不受影响。

**风险**：依赖 checkpoint 内部结构 `channel_values["messages"]`（对固定 StateGraph 稳定）；
在代码注释标注该耦合点。

**完成记录（2026-10-04）**

- 抽出 `AgentRuntime._checkpointer()`（外部传入优先，否则 `store.aopen()`），`_ensure_graph` 复用。
- `history()` 改为 `saver.aget_tuple(...)` 直读 `checkpoint["channel_values"]["messages"]`，
  不再调用 `build_graph`（因而不再要求 API Key / WSL）。缺失 `aget_tuple` 或会话不存在返回 `[]`。
- 测试：`tests/unit/test_runtime_history.py`（4 例）——含"把 `build_graph` 换成必抛异常来证明未建图"、
  空 Key 可用、跳过工具消息、未知 thread 返回空。
- 结果：单元 `571 passed`（+4），`ruff` 干净。

---

## A3　上下文裁剪支持 content blocks　【M】✅ 已完成

**目标**：模型返回 content blocks 列表时裁剪依然生效，且不破坏消息结构。

**现状与根因**：`llm/context.py::_shorten` 对非 `str` content 原样返回；
`message_chars` 用 `len(str(content))` 估算列表长度（按 repr 计，虚高且与真实文本无关）。

**实施步骤**

1. 新增 `_content_text_len(content)`：`str` → 长度；list → 累加
   `{"type": "text", "text": ...}` 块的文本长度；其他 → `len(str(...))`。`message_chars` 改用它。
2. `_shorten` 支持 list：仅截断 text 块（`model_copy(update={"content": new_list})`），
   保留非文本块（图片/tool_use 等）；截断后仍超限则退化为"全部文本块合并为截断字符串"。
3. 守住"绝不丢消息"不变量：只改 content，不删消息。

**测试**（`tests/unit/test_context.py`）

- list content 超限 → 被截断，块类型保留；
- `total_chars` 统计文本而非 repr；
- 既有 `test_preserves_tool_call_pairing` 等仍过。

**验收**：块内容裁剪生效；字符预算估算合理；无消息被丢弃。

**完成记录（2026-10-04）**

- 新增 `_content_text_len`（只累加 `type == "text"` 的文本块），`message_chars` 改用它。
- `_shorten` 支持 list：`_truncate_blocks` 按序保留前 N 个字符的文本、附省略说明，
  **非文本块（图片等）原样保留**；块边界处无文本块可附着时补一条独立说明块。
- 既有 `test_non_string_content_does_not_crash` 只断言"不崩"，本次把它升级为真正断言内容被裁剪。
- 测试：`test_context.py` 新增 4 例（字符按文本计、块被裁剪、非文本块保留、短内容不动）。
- 结果：单元 `575 passed`（+4），`ruff` 干净。

---

## A4　shell 路径的变更也要触发验证（`dirty`）　【M】✅ 已完成

**目标**：经审批执行的变更类 shell 命令（`sed -i`、`git add`、`mkdir` 等）让本步
`dirty=True`，从而跑一次 verify。

**现状与根因**：`graph/nodes/tools.py::MUTATING_TOOLS` 只按**工具名**判定，`shell_exec`
不在其中。模型用 `sed -i`（L2 批准）改源码后 `dirty` 仍为 False → verify 被跳过 →
"失败驱动修复"循环看不到这次改动。

**实施步骤**

1. tools 节点执行成功（`artifact.ok`）分支里，对 `shell_exec` 追加：
   `tool_level(name, args) >= CommandLevel.LOW_WRITE` → `dirty = True`
   （`tool_level` 已在 `tools.py` 导入）。
2. 仅对**实际执行**（decision ∈ {auto, approved}）的调用生效，被拒的不算。
3. 工具返回文本补一句提示（可选）："通过 shell 修改的文件没有快照留底，建议改用 `file_edit`"。

**明确不做**：不为 shell 变更生成快照/`FileChanged`（shell 输出不可结构化解析）——
这是能力边界，写进 [ACCEPTANCE.md](ACCEPTANCE.md) 已知限制。

**测试**（`tests/unit/test_nodes.py` 或 `test_approve.py` 风格）

- `shell_exec` 跑 `mkdir x` / `sed -i ...`（已放行）→ `dirty=True`；
- `shell_exec` 跑 `ls` → `dirty` 保持 False；
- 被拒绝的变更命令 → `dirty` 保持 False。

**验收**：批准变更类 shell 后有 verify；只读 shell 无副作用；集成 shell 测试不受影响。

**完成记录（2026-10-04）**

- `tools.py` 抽出 `_mutates_workspace(name, args)`：命名变更工具直接为真；
  `shell_exec` 按 `tool_level(...) >= LOW_WRITE` 判定。`dirty` 置位改用该函数。
- 消费端补测：`tests/unit/test_verify_node.py`（`dirty` 短路 / 触发验证 / `verify_enabled=False`）。
- 测试：`test_nodes.py` 新增 5 例（命名工具、`sed -i`、`git add`、只读、被拒）。
- 结果：单元 `567 passed`（+8），`test_shell_tool.py` 9 passed，`ruff` 干净。
- 能力边界（写入 `ACCEPTANCE.md` 已知限制，随 A6 落地）：shell 变更仍**不产生快照 /
  `FileChanged`**，「可回滚的修改流程」只覆盖 `file_*` 工具路径。

---

## A5　工程卫生杂项　【M（可并行）】✅ 已完成

| 子项 | 现状/根因 | 改法 | 测试/验收 |
|---|---|---|---|
| A5.1 超时码消歧（C3） | `limits.py` `124` 与命令自身退出码 124 不可区分 | `wsl_exec.run` 中 `timed_out` 判定改为 `exit_code == 124 and duration_ms >= wall*1000*0.9` | 单测：短时长的 124 → 不算超时；长 → 算 |
| A5.2 删除空 `main.py`（E1） | 0 字节、被 git 跟踪、全仓无引用 | `git rm main.py` | `git ls-files` 无该文件 |
| A5.3 CI 与开发依赖（E2） | 无 CI；`addopts=-n auto` 缺 xdist 时 `pytest` 直接报错 | 加 `Makefile`（`make check` = ruff + pytest）；加 CI（`pip install -e ".[dev]"` → ruff → `pytest -m "not wsl"`）；README 注明装 dev extras | CI 绿；新克隆按 README 一条命令跑通 |
| A5.4 `RunStarted` 入契约（E3） | `events.py` 联合缺 `run_started`；web 用裸 `Event(type=...)` | 新增 `RunStarted` 模型并加入 `EventType`；web 改用它 | `test_events.py` 补断言；web SSE 帧类型统一 |
| A5.5 `.coverage` 入库 | 覆盖率二进制产物被 git 跟踪，每次 `--cov` 都产生噪声 diff | `git rm --cached .coverage` 并加入 `.gitignore` | `git ls-files` 无 `.coverage` |

> **撤销**：C4（`ulimit -f` 换算）经实测确认为正确实现，不改。

**验收**：`ruff` 干净；新克隆 `make check` 一步跑通；事件契约覆盖 web 全部帧类型。

**完成记录（2026-10-04）**

- **A5.1**：`wsl_exec.run` 的 `timed_out` 增加"跑满墙钟时间"判定（`duration >= wall*0.9`）；
  新增集成用例 `test_exit_code_124_is_not_mistaken_for_timeout`（`exit 124` 不再误报）。
- **A5.2**：`git rm main.py`（0 字节空文件，全仓无引用）。
- **A5.3**：新增 `Makefile`（`make check` / `test-all`）、`.github/workflows/ci.yml`
  （`pip install -e ".[dev]"` → ruff → `pytest -m "not wsl and not llm"`）；README 开发段补
  "先装 dev 依赖"与 `make check`。
- **A5.4**：`events.py` 新增 `RunStarted` 并入 `EventType`；web `/api/run` 首帧改用它；补事件序列化用例。
- **A5.5**：`git rm --cached .coverage`，`.gitignore` 增补 `.coverage` / `htmlcov/`。
- 结果：单元 `575 passed`，`test_wsl_exec.py` 9 passed，`ruff` 干净。

---

## A6　文档同步　【S】✅ 已完成

**目标**：消除本轮 P0/P1 修复与阶段 A 造成的文档漂移。

1. `ARCHITECTURE.md` 第 6 节不变量表：补 4 条新防线——审批恢复保留参数、悬空链接判定、
   只读命令越界升级、引号感知判定。
2. `ACCEPTANCE.md`：更新测试/覆盖率数字；"已知限制"补"shell 变更无快照/无 `FileChanged`"
   （A4 的能力边界）、"仅到 WSL 发行版级隔离（方案 B 待办）"。
3. `PLAN.md` 第 8 节：追加"冻结后缺陷修复"记录。

**验收**：文档数字与 `pytest`/覆盖率实测一致；每个新防线都能在文档里找到对应测试名。

**完成记录（2026-10-04）**

- `ARCHITECTURE.md` 不变量表补第 10–15 条（审批跨恢复保留参数、悬空链接、只读命令越界升级、
  引号感知、读取上限前置、`dirty` 覆盖 shell）。
- `ACCEPTANCE.md` 更新测试/覆盖率数字与 CI 说明；已知限制补"shell 变更无快照"、
  沙箱隔离表述补"词法越界升级 + 方案 B 待办"。
- `PLAN.md` §8 增"冻结后记录：缺陷收口"。
- 数字：全量（不含 LLM）**749 passed**，覆盖率 **89%**。

---

## A7　快照排序：同一秒内的多次留底要能区分（A6 全量跑时发现）　【S】✅ 已完成

**目标**：`SnapshotStore.list` / `latest_for` 的"新旧"判定必须可靠。

**现状与根因**：`snapshot_id` 原为 `%Y%m%dT%H%M%S-<uuid6>`（**秒级**时间戳），
`list()` 按 id 字典序倒序。agent 常在一步里连续改好几处，多次留底落在同一秒时，
排序退化成按**随机 uuid**，`latest_for` 与"最老一次"会选错版本。全量测试
`test_restore_across_multiple_edits` 因此在负载下偶发失败（单独跑不易复现）。

**实施步骤**

1. 抽出 `_new_snapshot_id()`：时间戳精确到**微秒**，并追加**进程内递增序号** `seq3`
   —— 光靠微秒不够，系统时钟粒度可能让同一刻度内生成相同时间戳（单测已证）。
2. TUI 的 `_SNAPSHOT_ID_RE` 放宽为 `\d{6,18}`，同时兼容旧的秒级 id。

**测试**

- `tests/unit/test_snapshots_id.py`：50 个背靠背 id 严格递增、格式含微秒+序号。
- 集成 `test_snapshots.py` / `test_runtime_rollback.py`：**40 passed**（含此前偶发失败的用例）。

**验收**：同一秒（乃至同一时钟刻度）内的多次留底排序稳定，回滚选版正确。

---

## 2. 里程碑 Demo（阶段 A 收口时演示）

| 场景 | 操作 | 期望 |
|---|---|---|
| 大文件保护 | 工作区放 100MB 文件，`file_read` 它 | 秒级拒绝，进程内存不涨 |
| 无 Key 历史 | 清空 `DEEPSEEK_API_KEY`，TUI `/switch <会话>` | 正常载入历史 |
| 变更必验证 | 批准 `sed -i` 改源码 | 事件流出现 `Verification` |
| 搜索不误伤 | shell 跑 `grep -rn "rm -rf" docs/` | L0，不弹审批 |
| 越界要确认 | shell 跑 `cat ~/.ssh/id_rsa` | 弹 L2 审批 |

## 3. 完成标准（Definition of Done）

1. `ruff check src tests` 干净；
2. `pytest -m "not wsl"` 与 `pytest -m wsl` 全过（`-n auto`）；
3. 每个任务都有**失败可复现**的回归测试先行（先红后绿）；
4. 不破坏 [ARCHITECTURE.md](ARCHITECTURE.md) 第 6 节的不变量（现 15 条）；
5. 文档（A6）同步。

---

## 4. 已完成（P0 / P1）

作为本计划的前置，以下缺陷已在冻结后修复（均带回归测试）：

| # | 问题 | 修复落点 |
|---|---|---|
| P0-1 | shell 只读命令无文件系统边界（`cat ~/.ssh/id_rsa` 等 L0 自动放行） | `sandbox/policy.py::_escalate_auto_command`（越界/替换/`find -exec` → L2） |
| P0-2 | 审批恢复后审计丢失工具调用参数 | `runtime.py` 跨 run/resume 的 `pending` 配对表 |
| P0-3 | 悬空符号链接可写到工作区外 | `sandbox/fs.py` `-e \|\| -L` 判定（`write_text` + `_stat_script`） |
| P1-1 | `recursion_limit` 偏小，重负载误报失败 | `runtime.py` 系数 `2*rounds+2` → `3*rounds+3` |
| P1-2 | 危险模式原始全文匹配误伤（`grep "rm -rf"` 判 L3） | `sandbox/policy.py` 引号感知（`_mask_quoted` + `sh -c` 包装保留扫描） |
| P1-3 | `budget_exhausted` 不参与路由，空转步骤被静默跳过 | `graph/routing.py` 窄规则 + `respond.py` 如实上报 |
| P1-4 | 审批 `call_id` 空/重复时串号 | `graph/nodes/approve.py` 整批 fail-closed |
