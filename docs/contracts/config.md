<!-- 由 scripts/gen_contracts.py 从源码生成，禁止手改。改代码后重跑生成，CI 会校验一致性。 -->

# 配置契约

事实源：`src/coding_agent/config.py`。**这份是全量**，README 的配置表只是摘录。
机器可读版本（可直接用来校验有没有捏造配置项）：[`config.json`](config.json)。

优先级：构造函数参数 **>** .env **>** 环境变量 **>** 默认值。

> 刻意偏离「环境变量 > .env」的通用约定：某些工具链会在进程启动时注入用户看不见也删不掉的陈旧值，导致改 .env 不生效。项目目录里的 .env 是显式写下、看得见、可编辑的意图，因此让它优先。

共 **150** 个配置项。

## 通用

| 环境变量 | 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `AGENT_ALLOW_RULES` | `allow_rules` | `str` | `""` | 持久放行规则，逗号分隔的 `tool:glob`，例如 `shell_exec:git status*,run_tests:*`。只覆盖原本会 ASK 的 L2。 |
| `AGENT_APPROVAL_MODE` | `approval_mode` | `str` | `"ask"` | ask（逐条询问）\| approve（自动放行 L2；L3 仍需确认）\| deny（一律拒绝） |
| `AGENT_AUDIT_DIR` | `audit_dir` | `str` | `""` | 留空表示自动用 <当前目录>/.agent/audit |
| `AGENT_AUDIT_ENABLED` | `audit_enabled` | `bool` | `true` |  |
| `AGENT_AUDIT_MAX_MB` | `audit_max_mb` | `int` | `0` | 单个审计文件超过该大小（MB）就轮转；0 表示不轮转 |
| `AGENT_BILLING_ENABLED` | `billing_enabled` | `bool` | `false` | 是否在 run 结束时拉取 provider 账单做对账；默认关闭避免额外网络请求。 |
| `AGENT_BILLING_TIMEOUT_S` | `billing_timeout_s` | `float` | `10.0` |  |
| `AGENT_CHECKPOINT_KEEP` | `checkpoint_keep` | `int` | `200` | 每个 thread/命名空间保留的 checkpoint 数；0 = 不轮转。 每轮工具调用都会落一个 checkpoint，长期使用需要上限。 |
| `AGENT_CHECKPOINT_PATH` | `checkpoint_path` | `str` | `""` | ---------- 持久化与审计 ---------- 留空表示自动用 <当前目录>/.agent/checkpoints.sqlite；填 :memory: 强制不落盘 |
| `AGENT_CONCURRENCY_LLM_GLOBAL` | `concurrency_llm_global` | `int` | `6` |  |
| `AGENT_CONCURRENCY_LLM_PER_PROVIDER` | `concurrency_llm_per_provider` | `int` | `4` |  |
| `AGENT_CONCURRENCY_LLM_TIMEOUT_S` | `concurrency_llm_timeout_s` | `float` | `30.0` |  |
| `AGENT_CONCURRENCY_TOOLS` | `concurrency_tools` | `int` | `0` |  |
| `AGENT_CONCURRENCY_WORKERS` | `concurrency_workers` | `int` | `0` | ---------- 统一并发治理 ---------- 0 = 回退到旧的单项配置（orchestrator_max_concurrent_workers / sandbox_max_concurrent_commands / max_concurrent_tools）。 |
| `AGENT_CONCURRENCY_WORKER_TIMEOUT_S` | `concurrency_worker_timeout_s` | `float` | `60.0` |  |
| `AGENT_CONCURRENCY_WSL` | `concurrency_wsl` | `int` | `0` |  |
| `AGENT_CONCURRENCY_WSL_ADAPTIVE` | `concurrency_wsl_adaptive` | `bool` | `true` | 基于命令 p95 延迟的弹性并发：高延迟降并发，低延迟逐步恢复。 |
| `AGENT_CONCURRENCY_WSL_LATENCY_WINDOW` | `concurrency_wsl_latency_window` | `int` | `20` |  |
| `AGENT_CONCURRENCY_WSL_MAX` | `concurrency_wsl_max` | `int` | `0` |  |
| `AGENT_CONCURRENCY_WSL_MIN` | `concurrency_wsl_min` | `int` | `2` |  |
| `AGENT_CONCURRENCY_WSL_RESERVE` | `concurrency_wsl_reserve` | `int` | `1` | 给健康探针/短探测保留的 WSL 槽位；普通命令不能用。 |
| `AGENT_CONCURRENCY_WSL_TARGET_MS` | `concurrency_wsl_target_ms` | `int` | `1500` |  |
| `AGENT_CONCURRENCY_WSL_TIMEOUT_S` | `concurrency_wsl_timeout_s` | `float` | `30.0` |  |
| `AGENT_CONTEXT_KEEP_RECENT` | `context_keep_recent` | `int` | `12` |  |
| `AGENT_CONTEXT_MAX_CHARS` | `context_max_chars` | `int` | `60000` | ---------- 上下文裁剪 ---------- 0 表示不裁剪；这些值作用于发给模型的历史，不改写会话状态 |
| `AGENT_CONTEXT_MAX_TOKENS` | `context_max_tokens` | `int` | `0` |  |
| `AGENT_CONTEXT_TOOL_CHARS` | `context_tool_chars` | `int` | `1500` |  |
| `AGENT_COST_ALERT_RATIO` | `cost_alert_ratio` | `float` | `0.8` | 达到预算的该比例时发出告警（0~1） |
| `AGENT_DELEGATE_LARGE_CALLS` | `delegate_large_calls` | `int` | `4` |  |
| `AGENT_DELEGATE_LARGE_FILES` | `delegate_large_files` | `int` | `300` |  |
| `AGENT_DELEGATE_MEDIUM_CALLS` | `delegate_medium_calls` | `int` | `2` |  |
| `AGENT_DELEGATE_MEDIUM_FILES` | `delegate_medium_files` | `int` | `100` |  |
| `AGENT_DELEGATE_MIN_FILES` | `delegate_min_files` | `int` | `30` | ---- 成本感知的委派预算（按仓库文件数分级）---- 小于 min_files：不委派，直接读；[min, medium)：medium_calls； [medium, large)：large_calls；>= large：max_delegate_calls。 |
| `AGENT_DEPS_DETECT_TTL_S` | `deps_detect_ttl_s` | `float` | `30.0` | 包管理器探测缓存时间；安装后会主动失效。 |
| `AGENT_EXPLORE_ENABLED` | `explore_enabled` | `bool` | `false` | ---------- 子智能体（Phase 1：只读 Explore）---------- **默认关闭**：它会给主模型多一个工具、改变 agent 行为，打开后旧评测基线 不再可比。评测时显式设置 AGENT_EXPLORE_ENABLED=true 做 A/B。 |
| `AGENT_EXPLORE_MAX_REPORT_CHARS` | `explore_max_report_chars` | `int` | `4000` | 返回给主模型的摘要上限（字符） |
| `AGENT_EXPLORE_MAX_ROUNDS` | `explore_max_rounds` | `int` | `8` | 子智能体最多几轮「模型 → 工具」；到顶后强制生成报告，状态记 partial。 定位一个跨文件功能通常需要 6–10 轮只读检索；6 轮实测会频繁 partial。 |
| `AGENT_EXPLORE_TIMEOUT` | `explore_timeout` | `int` | `120` | 整个子智能体的墙钟上限（秒） |
| `AGENT_INSTRUCTIONS_ENABLED` | `instructions_enabled` | `bool` | `true` | 是否发现并注入仓库内的 AGENTS.md / CLAUDE.md（分层项目指令）。 |
| `AGENT_LLM_AIMD_MAX` | `llm_aimd_max` | `int` | `8` |  |
| `AGENT_LLM_AIMD_MIN` | `llm_aimd_min` | `int` | `1` | LLM 每个 provider 的自适应并发范围。 |
| `AGENT_LLM_FALLBACK_ON_ERROR` | `llm_fallback_on_error` | `bool` | `true` | 非 429 错误是否也尝试下一个 provider。 |
| `AGENT_LLM_PROVIDERS` | `llm_providers` | `str` | `""` | 可选多 provider JSON；空 = 只用 DEEPSEEK_* 单 provider。 [{"name":"deepseek","base_url":"...","api_key":"...","model":"...", "priority":0}, {"name":"backup","api_key_env":"BACKUP_API_KEY",...}] |
| `AGENT_LLM_PROVIDER_MAX_WAIT_S` | `llm_provider_max_wait_s` | `float` | `30.0` | 所有 provider 都在冷却时，单次调用最多等待多久再放弃。 |
| `AGENT_LLM_ROUTING_STRATEGY` | `llm_routing_strategy` | `str` | `"cost"` | cost（按 provider 单价排序）\| priority（按配置顺序） |
| `AGENT_LLM_TEMPERATURE` | `llm_temperature` | `float` | `0.0` |  |
| `AGENT_LLM_TIMEOUT` | `llm_timeout` | `int` | `120` |  |
| `AGENT_LSP_COMMAND` | `lsp_command` | `str` | `""` | 可选 language server 命令；空 = 不启用（symbol_search 仍用 AST）。 |
| `AGENT_LSP_DIAGNOSTICS_WAIT_S` | `lsp_diagnostics_wait_s` | `float` | `2.0` | pull 诊断不可用时，等待 publishDiagnostics 的最长时间。 |
| `AGENT_LSP_TIMEOUT` | `lsp_timeout` | `float` | `10.0` |  |
| `AGENT_MAX_CAPTURE_BYTES` | `max_capture_bytes` | `int` | `1000000` | 宿主从沙箱捕获单条命令输出的字节上限（stdout/stderr 各自）。 `max_output_chars` 只控制回灌给模型的文本；捕获阶段若不设上限， 一条 `yes`/`cat big.log` 会在截断前先把宿主内存打满。 |
| `AGENT_MAX_CONCURRENT_AGENTS` | `max_concurrent_agents` | `int` | `4` | 同一条消息里的多个 delegate 调用最多同时跑几个。 子智能体只读，所以并发不会产生写冲突；限制主要是控制成本与 wsl.exe 压力。 |
| `AGENT_MAX_CONCURRENT_TOOLS` | `max_concurrent_tools` | `int` | `4` | 同一条消息里的多个只读工具最多同时跑几个；写工具始终串行。 |
| `AGENT_MAX_DELEGATE_CALLS` | `max_delegate_calls` | `int` | `6` | 单个任务最多委派几次探索 —— 与主 agent 的工具轮次预算相互独立。 默认给到 6：提示词鼓励把互不依赖的区域拆成多个并发 delegate， 预算若只有 2 会与这条建议直接冲突（实测 5 个里 3 个被预算拒绝）。 |
| `AGENT_MAX_FILE_READ_BYTES` | `max_file_read_bytes` | `int` | `2000000` |  |
| `AGENT_MAX_OUTPUT_CHARS` | `max_output_chars` | `int` | `20000` |  |
| `AGENT_MAX_PLAN_STEPS` | `max_plan_steps` | `int` | `5` |  |
| `AGENT_MAX_REPAIR_ROUNDS` | `max_repair_rounds` | `int` | `3` | ---------- Agent 循环 ---------- |
| `AGENT_MAX_REPLANS` | `max_replans` | `int` | `2` | 执行中「重建剩余计划」的次数上限。计划可以修正，但不能无限改 —— 边做边改没有收敛点，而每次重规划都要多一次模型调用 |
| `AGENT_MAX_RESUMES` | `max_resumes` | `int` | `50` | 同一会话内「挂起→恢复」的总次数上限，防前端反复 resume 绕开 recursion_limit |
| `AGENT_MAX_SAME_FAILURES` | `max_same_failures` | `int` | `2` | 连续出现同一失败签名多少次后停止 repair、改走 replan。 24 次相同失败验证的教训：没有这道闸，repair 会把同一类修改重复到额度耗尽。 |
| `AGENT_MAX_TOOL_ROUNDS` | `max_tool_rounds` | `int` | `12` |  |
| `AGENT_MEMORY_ROOT` | `memory_root` | `str` | `""` | 长期记忆根目录（WSL 绝对路径）；空则按 trust_repo_memory 推导。 |
| `AGENT_MEMORY_TOP_K` | `memory_top_k` | `int` | `8` | 每次运行注入系统提示的长期记忆条数（按与请求的相关度取前 K 条）。 0 表示不限制，整份注入 —— 记忆上限 50 条，但每条都会进入每一轮 system prompt。 |
| `AGENT_NUDGE_EMPTY_STEPS` | `nudge_empty_steps` | `bool` | `true` | 「一步没有任何改动」时是否给它一次重做的机会。  背景：`dirty=False` 时 verify 与 review 都会短路，控制流直接 advance —— 于是「这一步只读了文件」与「这一步做完了」在图上**无法区分**。实测过一整批 长程任务因此把计划"读"完就算完成：10 次运行里 5 次一次写都没尝试过。  默认开启，但**必须可关**：它会改变所有任务的行为，现有基线因此不再可比 （评测的 baseline 里记了这个开关）。 |
| `AGENT_ORCHESTRATOR_ADD_EXPLORE_TESTS` | `orchestrator_add_explore_tests` | `bool` | `false` | 只读模式下模型只给一个任务时，是否强行补一个「找测试」explore 任务。 默认关闭：这会增加一次 worker 成本，只有演示并发/确实需要测试画像时才值得。 |
| `AGENT_ORCHESTRATOR_ALLOW_OVERLAP` | `orchestrator_allow_overlap` | `bool` | `false` | 允许多个 implement 修改同一文件；合并时用 git apply --3way。 有真实冲突时整批回滚，不会把冲突标记落进主工作区。 |
| `AGENT_ORCHESTRATOR_CHECKPOINT_EVERY` | `orchestrator_checkpoint_every` | `int` | `4` | scheduler 每完成多少个任务返回一次父图，形成 checkpoint/干预边界。 |
| `AGENT_ORCHESTRATOR_CONTEXT_CHARS` | `orchestrator_context_chars` | `int` | `8000` | 交给主 agent 的 worker 汇总上限（字符）。 |
| `AGENT_ORCHESTRATOR_ENABLED` | `orchestrator_enabled` | `bool` | `true` | ---------- Orchestrator（Phase 3A）---------- 新架构默认启用：满足规模阈值的仓库走 harness；小仓库仍走 direct 快速路径。 |
| `AGENT_ORCHESTRATOR_FANIN_REDUCER` | `orchestrator_fanin_reducer` | `bool` | `false` | fan-in 超预算/有冲突时才调用一次 reducer LLM；失败回退确定性汇总。 |
| `AGENT_ORCHESTRATOR_FANOUT_ARTIFACTS` | `orchestrator_fanout_artifacts` | `bool` | `true` | fan-in 是否落 artifact store 并输出结构化结果。 |
| `AGENT_ORCHESTRATOR_MAX_CONCURRENT_WORKERS` | `orchestrator_max_concurrent_workers` | `int` | `4` | 同一批 ready 任务最多并发几个只读 worker。 |
| `AGENT_ORCHESTRATOR_MAX_PENDING_MERGES` | `orchestrator_max_pending_merges` | `int` | `8` | 在飞 implement + 待合并补丁的上限；超过后暂停启动新的 implement， 先让合并队列排空，避免补丁/快照堆积。 |
| `AGENT_ORCHESTRATOR_MAX_TASKS` | `orchestrator_max_tasks` | `int` | `6` |  |
| `AGENT_ORCHESTRATOR_MIN_FILES` | `orchestrator_min_files` | `int` | `30` | 小于该文件数不做编排，直接单 agent。 |
| `AGENT_ORCHESTRATOR_RATE_LIMIT_BACKOFF_S` | `orchestrator_rate_limit_backoff_s` | `float` | `2.0` | 遇到 429/限流时 scheduler 的首次退避秒数；之后指数增长，上限 30s。 |
| `AGENT_ORCHESTRATOR_RUN_DEADLINE_S` | `orchestrator_run_deadline_s` | `int` | `900` | 整个编排阶段（含所有 worker）的墙钟上限，0 表示不限制。 |
| `AGENT_ORCHESTRATOR_TASK_RETRIES` | `orchestrator_task_retries` | `int` | `1` | 单个任务失败后最多重试几次（0 = 不重试）。重试仍失败才把依赖方标 blocked。 |
| `AGENT_ORCHESTRATOR_TASK_TOKENS` | `orchestrator_task_tokens` | `int` | `0` | 单任务 token 上限，0 表示不限制（由 provider 回报的 usage 累计判断）。 |
| `AGENT_ORCHESTRATOR_TOTAL_TOKENS` | `orchestrator_total_tokens` | `int` | `0` | 整个编排阶段的 token 上限，0 = 不限制。scheduler 在派发前按角色预留， 预留 + 已用超过上限时不再启动新任务。 |
| `AGENT_ORCHESTRATOR_WORKER_MODEL` | `orchestrator_worker_model` | `str` | `""` | worker 专用模型；空串表示继承 DEEPSEEK_MODEL。用于 explore/review 降本。 |
| `AGENT_ORCHESTRATOR_WORKER_REPAIRS` | `orchestrator_worker_repairs` | `int` | `2` |  |
| `AGENT_ORCHESTRATOR_WORKER_ROUNDS` | `orchestrator_worker_rounds` | `int` | `12` |  |
| `AGENT_ORCHESTRATOR_WORKER_TIMEOUT` | `orchestrator_worker_timeout` | `int` | `240` |  |
| `AGENT_ORCHESTRATOR_WRITE_ENABLED` | `orchestrator_write_enabled` | `bool` | `false` | ---- Phase 3C：并发写 worker（默认关闭）---- 打开后，orchestrator 才可能生成 implement 任务；还要求 --write、工作区是 git 仓库且干净。worker 在独立 git worktree 中修改，完成后由宿主合并补丁。 |
| `AGENT_ORCHESTRATOR_WRITE_PLAN_APPROVAL` | `orchestrator_write_plan_approval` | `bool` | `false` | 派发 implement 前一次性展示 write_scope 并等待确认（并发区不弹审批）。 |
| `AGENT_ORCHESTRATOR_WRITE_SCOPE_ENFORCE` | `orchestrator_write_scope_enforce` | `bool` | `true` | 写 worker 的 write_scope 在 I/O 前强制校验（三重防线之一）。 |
| `AGENT_PERSISTENT_SHELL` | `persistent_shell` | `bool` | `true` | 常驻 WSL bash 会话池。默认开启：实测读/检索从 ~170ms 降到 ~20ms。 会话异常时会自动退回逐命令 wsl.exe，因此可安全地作为默认值。 |
| `AGENT_PERSISTENT_SHELL_POOL` | `persistent_shell_pool` | `int` | `4` |  |
| `AGENT_RATE_LIMIT_STATE_PATH` | `rate_limit_state_path` | `str` | `""` | 跨进程限流状态文件；留空 = <当前目录>/.agent/rate_limits.json；:memory: 关闭 |
| `AGENT_READ_CACHE_MAX_BYTES` | `read_cache_max_bytes` | `int` | `"64 * 1024 * 1024"` | run 级文件内容缓存的字节上限（LRU）。0 = 关闭缓存。 |
| `AGENT_READ_DENY_GLOBS` | `read_deny_globs` | `str` | `"DEFAULT_READ_DENY_GLOBS"` | 秘密文件读保护 glob（逗号分隔）；.env.example 等模板自动放行。 |
| `AGENT_REPO_MAP_CHARS` | `repo_map_chars` | `int` | `4000` | repo map 注入 planner 的字符预算（0 = 关闭）。 |
| `AGENT_RETRY_ATTEMPTS` | `retry_attempts` | `int` | `3` | 节点级瞬时错误重试（超时/连接重置/5xx/429/环境抖动）。 attempts 是**总尝试次数**；1 表示不重试。退避为指数 + 抖动， 且整次重试累计等待不超过 max_total_wait，避免长时间卡住。 |
| `AGENT_RETRY_BASE_DELAY_S` | `retry_base_delay_s` | `float` | `1.0` |  |
| `AGENT_RETRY_MAX_DELAY_S` | `retry_max_delay_s` | `float` | `30.0` |  |
| `AGENT_RETRY_MAX_TOTAL_WAIT_S` | `retry_max_total_wait_s` | `float` | `60.0` |  |
| `AGENT_REVIEW_ENABLED` | `review_enabled` | `bool` | `true` | ---------- 代码审查 ---------- 一步做完、验证通过后，对本次改动跑一遍确定性静态检查（`tools/review.py`）。 与自动 verify 同一口径：不过审批，所以只跑宿主写死的检查，不读工作区里的 任何"配置"。关掉它等于去掉「测试通过 ≠ 代码正确」那第二道关。 |
| `AGENT_REVIEW_LINT_BLOCKING` | `review_lint_blocking` | `bool` | `true` | 探测到的 linter/formatter 对**本次改动文件**的发现是否阻断。 只把改动文件提升为 blocking：仓库里已有的历史问题不该每一步都卡死。 |
| `AGENT_RUN_COST_BUDGET_USD` | `run_cost_budget_usd` | `float` | `0.0` | 单次 run 的成本预算（USD）；0 = 不告警 |
| `AGENT_RUN_DEADLINE_S` | `run_deadline_s` | `int` | `1800` | 一次完整运行（含审批/暂停后的 resume）的总墙钟上限，0 表示不限制。 与 recursion_limit 互补：后者管步数，这里管时间（例如模型长时间卡住）。 |
| `AGENT_SANDBOX_MAX_CONCURRENT_COMMANDS` | `sandbox_max_concurrent_commands` | `int` | `8` | 同时启动的 wsl.exe 进程上限。并发 worker 会各自调用沙箱，没有这层 背压时 4 个 worker 的一次工具轮次就能同时拉起十几个 WSL 进程。 |
| `AGENT_SECURITY_PROFILE` | `security_profile` | `str` | `"local"` | ---------- 安全 profile 与不可信仓库 ---------- local（默认，面向可信仓库）\| untrusted（bwrap + 禁网 + 强制 ask + 不信任仓库指令/记忆）。 |
| `AGENT_SESSION_BRIEF_CHARS` | `session_brief_chars` | `int` | `4000` | 编排子图/worker 的会话约束摘要上限（字符）。独立子图不复制 messages， 这里从 checkpoint 抽最近若干轮压缩后注入；0 表示关闭。 |
| `AGENT_SHELL_CPU_SECONDS` | `shell_cpu_seconds` | `int` | `600` | 沙箱资源上限，0 表示不限制 |
| `AGENT_SHELL_ENV_ALLOWLIST` | `shell_env_allowlist` | `str` | `""` | WSL 命令默认 `env -i` 清空宿主环境。这里按名称显式放行少量构建变量 （如 JAVA_HOME/GOPATH/VIRTUAL_ENV）。名称含 KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/AUTH/COOKIE/SESSION 的一律忽略。 |
| `AGENT_SHELL_MAX_FILE_MB` | `shell_max_file_mb` | `int` | `512` |  |
| `AGENT_SHELL_MAX_PROCESSES` | `shell_max_processes` | `int` | `1024` |  |
| `AGENT_SHELL_MEMORY_MB` | `shell_memory_mb` | `int` | `0` |  |
| `AGENT_SHELL_NETWORK` | `shell_network` | `bool` | `true` | 仅 bwrap 模式生效：false 时加 --unshare-net，命令完全无网络。 off 模式没有内核级隔离，词法策略无法禁止 curl/wget 出网。 |
| `AGENT_SHELL_SANDBOX` | `shell_sandbox` | `str` | `"off"` | shell 隔离后端：off（默认，仅工作区 cwd 约束）\| bwrap（挂载命名空间隔离） bwrap 模式下若沙箱内不可用，会**拒绝执行**（fail closed），不静默降级 |
| `AGENT_SHELL_TIMEOUT` | `shell_timeout` | `int` | `60` |  |
| `AGENT_STEERING_DIR` | `steering_dir` | `str` | `""` | 未消费 steering 指令的持久化目录；留空 = <当前目录>/.agent/steering |
| `AGENT_SYMBOL_INDEX_TTL_S` | `symbol_index_ttl_s` | `float` | `10.0` | AST 符号索引缓存时间；写操作不会主动失效，过期后自动重建。 |
| `AGENT_TOOL_JOURNAL_ENABLED` | `tool_journal_enabled` | `bool` | `true` | 有副作用工具的执行日志：崩溃后同一 call_id 不自动重放。 |
| `AGENT_TRUST_REPO_INSTRUCTIONS` | `trust_repo_instructions` | `bool` | `true` | 仓库内的 AGENTS.md / CLAUDE.md 是否作为可信指令注入。 |
| `AGENT_TRUST_REPO_MEMORY` | `trust_repo_memory` | `bool` | `true` | 是否读取仓库内 `.agent/memory.md`；false 时记忆改存 memory_root。 |
| `AGENT_VERIFY_COMMAND` | `verify_command` | `str` | `""` | 留空表示按项目清单自动识别（pytest / cargo / go / npm / make） |
| `AGENT_VERIFY_ENABLED` | `verify_enabled` | `bool` | `true` | ---------- 验证 ---------- |
| `AGENT_VERIFY_ONLY_FINAL` | `verify_only_final` | `bool` | `true` | 多步计划里，非最终的 implement 步不跑全量测试：早期步骤只完成了一部分， 全量套件必然因为后面还没做的步骤而失败，会把 agent 卡死在这一步。 只在 kind=verify 或最后一步验证；单步任务不受影响。 |
| `AGENT_VERIFY_TIMEOUT` | `verify_timeout` | `int` | `300` | 自动验证的独立超时。测试/构建通常比普通 shell 命令长，不能共用 60s； 0 表示回退到 shell_timeout。 |
| `AGENT_WEB_ALLOW_ANONYMOUS_LOCAL` | `web_allow_anonymous_local` | `bool` | `true` | 未配置 token 时是否允许 loopback 匿名访问（单用户本地模式）。 |
| `AGENT_WEB_AUTH_TOKENS` | `web_auth_tokens` | `str` | `""` | ---------- Web 多用户 ---------- JSON：{"token": "user"}；与 AGENT_WEB_AUTH_TOKENS_FILE 二选一。 |
| `AGENT_WEB_AUTH_TOKENS_FILE` | `web_auth_tokens_file` | `str` | `""` |  |
| `AGENT_WEB_DATA_ROOT` | `web_data_root` | `str` | `""` | per-user 宿主侧数据根目录（审计/checkpoint/steering）； 空 = <当前目录>/.agent/web/users。 |
| `AGENT_WEB_MAX_BODY_BYTES` | `web_max_body_bytes` | `int` | `"256 * 1024"` |  |
| `AGENT_WEB_MAX_PROMPT_CHARS` | `web_max_prompt_chars` | `int` | `100000` |  |
| `AGENT_WEB_MAX_RUNTIMES` | `web_max_runtimes` | `int` | `8` |  |
| `AGENT_WEB_MAX_RUNTIMES_PER_USER` | `web_max_runtimes_per_user` | `int` | `2` |  |
| `AGENT_WEB_MEMORY_ROOT` | `web_memory_root` | `str` | `""` | per-user WSL 侧记忆根目录；空 = 沙箱 $HOME/agent-memory。 记忆在沙箱内、仓库之外，避免不可信仓库预置记忆。 |
| `AGENT_WEB_RATE_LIMIT_PER_MIN` | `web_rate_limit_per_min` | `int` | `60` |  |
| `AGENT_WEB_RUNTIME_TTL_S` | `web_runtime_ttl_s` | `int` | `1800` | 挂起 runtime 的空闲保留时间；超时后释放，resume 时从 checkpoint 重建。 |
| `AGENT_WEB_WORKSPACES_ROOT` | `web_workspaces_root` | `str` | `""` | per-user 工作区根目录；空 = 沙箱 $HOME/agent-ws/users。 |
| `AGENT_WORKSPACE_LOCK` | `workspace_lock_enabled` | `bool` | `true` | 同一工作区只允许一个 runtime 运行；锁文件在审计目录下。 |
| `AGENT_WRITE_DENY_GLOBS` | `write_deny_globs` | `str` | `""` | 追加的内容级写保护 glob（逗号分隔）；默认规则始终生效。 |
| `AGENT_WSL_BREAKER_COOLDOWN_S` | `wsl_breaker_cooldown_s` | `float` | `15.0` |  |
| `AGENT_WSL_BREAKER_FAILURES` | `wsl_breaker_failures` | `int` | `3` |  |
| `AGENT_WSL_BREAKER_MAX_COOLDOWN_S` | `wsl_breaker_max_cooldown_s` | `float` | `60.0` |  |
| `AGENT_WSL_DISTRO` | `wsl_distro` | `str` | `"Ubuntu"` | ---------- WSL2 沙箱 ---------- |
| `AGENT_WSL_HEALTH_CHECK` | `wsl_health_check_enabled` | `bool` | `true` | WSL 健康检查与熔断。连续失败达到阈值后，短时间内直接 fail fast， 避免每个工具调用都去拉起一个注定失败的 wsl.exe；冷却后放一个探测请求。 |
| `AGENT_WSL_HEALTH_TIMEOUT_S` | `wsl_health_timeout_s` | `int` | `10` |  |
| `AGENT_WSL_HEALTH_TTL_S` | `wsl_health_ttl_s` | `float` | `30.0` |  |
| `AGENT_WSL_WORKSPACE` | `wsl_workspace` | `str` | `""` | 留空表示自动使用沙箱内 $HOME/agent-ws，避免与真实用户名不匹配 |
| `DEEPSEEK_API_KEY` | `deepseek_api_key` | `str` | `""` | ---------- DeepSeek ---------- |
| `DEEPSEEK_BASE_URL` | `deepseek_base_url` | `str` | `"https://api.deepseek.com"` |  |
| `DEEPSEEK_MODEL` | `deepseek_model` | `str` | `"deepseek-chat"` |  |
| `LANGSMITH_API_KEY` | `langsmith_api_key` | `str` | `""` |  |
| `LANGSMITH_PROJECT` | `langsmith_project` | `str` | `"coding-agent"` |  |
| `LANGSMITH_TRACING` | `langsmith_tracing` | `bool` | `false` | ---------- LangSmith ---------- |

