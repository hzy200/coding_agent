"""全局配置。

约定：环境变量名与业界惯例保持一致（如 ``DEEPSEEK_API_KEY``），
Agent 自有配置统一加 ``AGENT_`` 前缀。

**优先级：构造函数参数 > `.env` > 环境变量 > 默认值。**

通用约定是「环境变量 > .env」，这里刻意反过来。原因是本机踩到的实际故障：
harness 在进程启动时把陈旧的值注入进程环境，用户既看不到来源（不在注册表、
也不在任何配置文件里）也删不掉，于是改 `.env` 完全不生效 ——
表现为「明明更新了 API key 却一直 403」。

项目目录里的 `.env` 是**显式写下、看得见、可编辑**的意图，让它优先；
需要临时覆盖时用命令行前缀或环境变量改 `.env` 本身即可。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_READ_DENY_GLOBS = (
    ".env,.env.*,*.pem,*.key,*.p12,*.pfx,"
    "id_rsa*,id_ed25519*,id_ecdsa*,.ssh/**,"
    # 常见凭据文件：不能靠“扩展名是 .env”来判断敏感与否
    ".netrc,.npmrc,.pypirc,.git-credentials,"
    "credentials,**/credentials,credentials.json,**/credentials.json,"
    "*service-account*.json,*service_account*.json,"
    ".aws/**,**/.aws/**,.kube/**,**/.kube/**,"
    ".config/gcloud/**,**/.config/gcloud/**,"
    ".docker/config.json,**/.docker/config.json,"
    "*.tfstate,*.tfstate.backup,*.jks,*.keystore,*.gpg"
)

# 这些 profile 面向生产/只读审计：命令变更一律拒绝，写能力也被强制关闭。
PRODUCTION_PROFILES = frozenset({"production", "ops"})


class _NonEmptyDotEnvSource(PydanticBaseSettingsSource):
    """给 `.env` 源加一层空值过滤：`KEY=` 这种占位不算「配置过」。

    否则从 `.env.example` 复制来的空行会盖掉环境变量里真正可用的值 ——
    那正是这套优先级最容易被吐槽的地方。

    **委托**而不是重建：`Settings(_env_file=None)` 这类显式覆盖必须继续生效，
    重建一个源会把它无视掉，测试里大量依赖这一点。
    """

    def __init__(self, inner: PydanticBaseSettingsSource) -> None:
        super().__init__(inner.settings_cls)
        self._inner = inner

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # 不使用：取值全部委托给 _inner

    def __call__(self) -> dict[str, Any]:
        data = self._inner()
        return {k: v for k, v in data.items() if v not in ("", None)}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        # 构造函数参数仍然最高优先 —— 测试与嵌入式用法依赖这一点
        return (
            init_settings,
            _NonEmptyDotEnvSource(dotenv_settings),
            env_settings,
            file_secret_settings,
        )

    # ---------- DeepSeek ----------
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    # 可选多 provider JSON；空 = 只用 DEEPSEEK_* 单 provider。
    # [{"name":"deepseek","base_url":"...","api_key":"...","model":"...",
    #   "priority":0}, {"name":"backup","api_key_env":"BACKUP_API_KEY",...}]
    llm_providers: str = Field(default="", alias="AGENT_LLM_PROVIDERS")
    # 非 429 错误是否也尝试下一个 provider。
    llm_fallback_on_error: bool = Field(
        default=True, alias="AGENT_LLM_FALLBACK_ON_ERROR"
    )
    # 所有 provider 都在冷却时，单次调用最多等待多久再放弃。
    llm_provider_max_wait_s: float = Field(
        default=30.0, alias="AGENT_LLM_PROVIDER_MAX_WAIT_S"
    )
    # 节点级瞬时错误重试（超时/连接重置/5xx/429/环境抖动）。
    # attempts 是**总尝试次数**；1 表示不重试。退避为指数 + 抖动，
    # 且整次重试累计等待不超过 max_total_wait，避免长时间卡住。
    retry_attempts: int = Field(default=3, alias="AGENT_RETRY_ATTEMPTS")
    retry_base_delay_s: float = Field(
        default=1.0, alias="AGENT_RETRY_BASE_DELAY_S"
    )
    retry_max_delay_s: float = Field(
        default=30.0, alias="AGENT_RETRY_MAX_DELAY_S"
    )
    retry_max_total_wait_s: float = Field(
        default=60.0, alias="AGENT_RETRY_MAX_TOTAL_WAIT_S"
    )
    # cost（按 provider 单价排序）| priority（按配置顺序）
    llm_routing_strategy: str = Field(
        default="cost", alias="AGENT_LLM_ROUTING_STRATEGY"
    )
    # 单次 run 的成本预算（USD）；0 = 不告警
    run_cost_budget_usd: float = Field(
        default=0.0, alias="AGENT_RUN_COST_BUDGET_USD"
    )
    # 达到预算的该比例时发出告警（0~1）
    cost_alert_ratio: float = Field(
        default=0.8, alias="AGENT_COST_ALERT_RATIO"
    )
    # 是否在 run 结束时拉取 provider 账单做对账；默认关闭避免额外网络请求。
    billing_enabled: bool = Field(default=False, alias="AGENT_BILLING_ENABLED")
    billing_timeout_s: float = Field(
        default=10.0, alias="AGENT_BILLING_TIMEOUT_S"
    )

    # ---------- LangSmith ----------
    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: str = Field(default="", alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="coding-agent", alias="LANGSMITH_PROJECT")

    # ---------- WSL2 沙箱 ----------
    wsl_distro: str = Field(default="Ubuntu", alias="AGENT_WSL_DISTRO")
    # 留空表示自动使用沙箱内 $HOME/agent-ws，避免与真实用户名不匹配
    wsl_workspace: str = Field(default="", alias="AGENT_WSL_WORKSPACE")
    # shell 隔离后端：off（默认，仅工作区 cwd 约束）| bwrap（挂载命名空间隔离）
    # bwrap 模式下若沙箱内不可用，会**拒绝执行**（fail closed），不静默降级
    shell_sandbox: str = Field(default="off", alias="AGENT_SHELL_SANDBOX")
    # WSL 命令默认 `env -i` 清空宿主环境。这里按名称显式放行少量构建变量
    # （如 JAVA_HOME/GOPATH/VIRTUAL_ENV）。名称含
    # KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/AUTH/COOKIE/SESSION 的一律忽略。
    shell_env_allowlist: str = Field(
        default="", alias="AGENT_SHELL_ENV_ALLOWLIST"
    )
    # 仅 bwrap 模式生效：false 时加 --unshare-net，命令完全无网络。
    # off 模式没有内核级隔离，词法策略无法禁止 curl/wget 出网。
    shell_network: bool = Field(default=True, alias="AGENT_SHELL_NETWORK")
    # 常驻 WSL bash 会话池。默认开启：实测读/检索从 ~170ms 降到 ~20ms。
    # 会话异常时会自动退回逐命令 wsl.exe，因此可安全地作为默认值。
    persistent_shell: bool = Field(default=True, alias="AGENT_PERSISTENT_SHELL")
    persistent_shell_pool: int = Field(
        default=4, alias="AGENT_PERSISTENT_SHELL_POOL"
    )
    shell_timeout: int = Field(default=60, alias="AGENT_SHELL_TIMEOUT")
    # WSL 健康检查与熔断。连续失败达到阈值后，短时间内直接 fail fast，
    # 避免每个工具调用都去拉起一个注定失败的 wsl.exe；冷却后放一个探测请求。
    wsl_health_check_enabled: bool = Field(
        default=True, alias="AGENT_WSL_HEALTH_CHECK"
    )
    wsl_health_ttl_s: float = Field(
        default=30.0, alias="AGENT_WSL_HEALTH_TTL_S"
    )
    wsl_health_timeout_s: int = Field(
        default=10, alias="AGENT_WSL_HEALTH_TIMEOUT_S"
    )
    wsl_breaker_failures: int = Field(
        default=3, alias="AGENT_WSL_BREAKER_FAILURES"
    )
    wsl_breaker_cooldown_s: float = Field(
        default=15.0, alias="AGENT_WSL_BREAKER_COOLDOWN_S"
    )
    wsl_breaker_max_cooldown_s: float = Field(
        default=60.0, alias="AGENT_WSL_BREAKER_MAX_COOLDOWN_S"
    )
    # 自动验证的独立超时。测试/构建通常比普通 shell 命令长，不能共用 60s；
    # 0 表示回退到 shell_timeout。
    verify_timeout: int = Field(default=300, alias="AGENT_VERIFY_TIMEOUT")
    # 追加的内容级写保护 glob（逗号分隔）；默认规则始终生效。
    write_deny_globs: str = Field(default="", alias="AGENT_WRITE_DENY_GLOBS")
    # repo map 注入 planner 的字符预算（0 = 关闭）。
    repo_map_chars: int = Field(default=4_000, alias="AGENT_REPO_MAP_CHARS")
    # 可选 language server 命令；空 = 不启用（symbol_search 仍用 AST）。
    lsp_command: str = Field(default="", alias="AGENT_LSP_COMMAND")
    lsp_timeout: float = Field(default=10.0, alias="AGENT_LSP_TIMEOUT")
    # pull 诊断不可用时，等待 publishDiagnostics 的最长时间。
    lsp_diagnostics_wait_s: float = Field(
        default=2.0, alias="AGENT_LSP_DIAGNOSTICS_WAIT_S"
    )
    # 沙箱资源上限，0 表示不限制
    shell_cpu_seconds: int = Field(default=600, alias="AGENT_SHELL_CPU_SECONDS")
    shell_memory_mb: int = Field(default=0, alias="AGENT_SHELL_MEMORY_MB")
    shell_max_file_mb: int = Field(default=512, alias="AGENT_SHELL_MAX_FILE_MB")
    shell_max_processes: int = Field(default=1024, alias="AGENT_SHELL_MAX_PROCESSES")
    max_output_chars: int = Field(default=20_000, alias="AGENT_MAX_OUTPUT_CHARS")
    # 宿主从沙箱捕获单条命令输出的字节上限（stdout/stderr 各自）。
    # `max_output_chars` 只控制回灌给模型的文本；捕获阶段若不设上限，
    # 一条 `yes`/`cat big.log` 会在截断前先把宿主内存打满。
    max_capture_bytes: int = Field(
        default=1_000_000, alias="AGENT_MAX_CAPTURE_BYTES"
    )
    max_file_read_bytes: int = Field(default=2_000_000, alias="AGENT_MAX_FILE_READ_BYTES")
    # 同时启动的 wsl.exe 进程上限。并发 worker 会各自调用沙箱，没有这层
    # 背压时 4 个 worker 的一次工具轮次就能同时拉起十几个 WSL 进程。
    sandbox_max_concurrent_commands: int = Field(
        default=8, alias="AGENT_SANDBOX_MAX_CONCURRENT_COMMANDS"
    )

    # ---------- 统一并发治理 ----------
    # 0 = 回退到旧的单项配置（orchestrator_max_concurrent_workers /
    # sandbox_max_concurrent_commands / max_concurrent_tools）。
    concurrency_workers: int = Field(
        default=0, alias="AGENT_CONCURRENCY_WORKERS"
    )
    concurrency_tools: int = Field(
        default=0, alias="AGENT_CONCURRENCY_TOOLS"
    )
    concurrency_wsl: int = Field(default=0, alias="AGENT_CONCURRENCY_WSL")
    # 给健康探针/短探测保留的 WSL 槽位；普通命令不能用。
    concurrency_wsl_reserve: int = Field(
        default=1, alias="AGENT_CONCURRENCY_WSL_RESERVE"
    )
    # 基于命令 p95 延迟的弹性并发：高延迟降并发，低延迟逐步恢复。
    concurrency_wsl_adaptive: bool = Field(
        default=True, alias="AGENT_CONCURRENCY_WSL_ADAPTIVE"
    )
    concurrency_wsl_min: int = Field(
        default=2, alias="AGENT_CONCURRENCY_WSL_MIN"
    )
    concurrency_wsl_max: int = Field(
        default=0, alias="AGENT_CONCURRENCY_WSL_MAX"
    )
    concurrency_wsl_target_ms: int = Field(
        default=1500, alias="AGENT_CONCURRENCY_WSL_TARGET_MS"
    )
    concurrency_wsl_latency_window: int = Field(
        default=20, alias="AGENT_CONCURRENCY_WSL_LATENCY_WINDOW"
    )
    concurrency_llm_per_provider: int = Field(
        default=4, alias="AGENT_CONCURRENCY_LLM_PER_PROVIDER"
    )
    concurrency_llm_global: int = Field(
        default=6, alias="AGENT_CONCURRENCY_LLM_GLOBAL"
    )
    concurrency_worker_timeout_s: float = Field(
        default=60.0, alias="AGENT_CONCURRENCY_WORKER_TIMEOUT_S"
    )
    concurrency_llm_timeout_s: float = Field(
        default=30.0, alias="AGENT_CONCURRENCY_LLM_TIMEOUT_S"
    )
    concurrency_wsl_timeout_s: float = Field(
        default=30.0, alias="AGENT_CONCURRENCY_WSL_TIMEOUT_S"
    )
    # LLM 每个 provider 的自适应并发范围。
    llm_aimd_min: int = Field(default=1, alias="AGENT_LLM_AIMD_MIN")
    llm_aimd_max: int = Field(default=8, alias="AGENT_LLM_AIMD_MAX")

    # ---------- 上下文裁剪 ----------
    # 0 表示不裁剪；这些值作用于发给模型的历史，不改写会话状态
    context_max_chars: int = Field(default=60_000, alias="AGENT_CONTEXT_MAX_CHARS")
    context_max_tokens: int = Field(default=0, alias="AGENT_CONTEXT_MAX_TOKENS")
    context_keep_recent: int = Field(default=12, alias="AGENT_CONTEXT_KEEP_RECENT")
    context_tool_chars: int = Field(default=1_500, alias="AGENT_CONTEXT_TOOL_CHARS")
    # run 级文件内容缓存的字节上限（LRU）。0 = 关闭缓存。
    read_cache_max_bytes: int = Field(
        default=64 * 1024 * 1024, alias="AGENT_READ_CACHE_MAX_BYTES"
    )
    # AST 符号索引缓存时间；写操作不会主动失效，过期后自动重建。
    symbol_index_ttl_s: float = Field(
        default=10.0, alias="AGENT_SYMBOL_INDEX_TTL_S"
    )
    # 包管理器探测缓存时间；安装后会主动失效。
    deps_detect_ttl_s: float = Field(
        default=30.0, alias="AGENT_DEPS_DETECT_TTL_S"
    )
    # 每次运行注入系统提示的长期记忆条数（按与请求的相关度取前 K 条）。
    # 0 表示不限制，整份注入 —— 记忆上限 50 条，但每条都会进入每一轮 system prompt。
    memory_top_k: int = Field(default=8, alias="AGENT_MEMORY_TOP_K")
    # 是否发现并注入仓库内的 AGENTS.md / CLAUDE.md（分层项目指令）。
    instructions_enabled: bool = Field(
        default=True, alias="AGENT_INSTRUCTIONS_ENABLED"
    )
    # 编排子图/worker 的会话约束摘要上限（字符）。独立子图不复制 messages，
    # 这里从 checkpoint 抽最近若干轮压缩后注入；0 表示关闭。
    session_brief_chars: int = Field(
        default=4_000, alias="AGENT_SESSION_BRIEF_CHARS"
    )

    # ---------- 验证 ----------
    verify_enabled: bool = Field(default=True, alias="AGENT_VERIFY_ENABLED")
    # 多步计划里，非最终的 implement 步不跑全量测试：早期步骤只完成了一部分，
    # 全量套件必然因为后面还没做的步骤而失败，会把 agent 卡死在这一步。
    # 只在 kind=verify 或最后一步验证；单步任务不受影响。
    verify_only_final: bool = Field(default=True, alias="AGENT_VERIFY_ONLY_FINAL")
    # 留空表示按项目清单自动识别（pytest / cargo / go / npm / make）
    verify_command: str = Field(default="", alias="AGENT_VERIFY_COMMAND")

    # 「一步没有任何改动」时是否给它一次重做的机会。
    #
    # 背景：`dirty=False` 时 verify 与 review 都会短路，控制流直接 advance ——
    # 于是「这一步只读了文件」与「这一步做完了」在图上**无法区分**。实测过一整批
    # 长程任务因此把计划"读"完就算完成：10 次运行里 5 次一次写都没尝试过。
    #
    # 默认开启，但**必须可关**：它会改变所有任务的行为，现有基线因此不再可比
    # （评测的 baseline 里记了这个开关）。
    nudge_empty_steps: bool = Field(default=True, alias="AGENT_NUDGE_EMPTY_STEPS")

    # ---------- 代码审查 ----------
    # 一步做完、验证通过后，对本次改动跑一遍确定性静态检查（`tools/review.py`）。
    # 与自动 verify 同一口径：不过审批，所以只跑宿主写死的检查，不读工作区里的
    # 任何"配置"。关掉它等于去掉「测试通过 ≠ 代码正确」那第二道关。
    review_enabled: bool = Field(default=True, alias="AGENT_REVIEW_ENABLED")
    # 探测到的 linter/formatter 对**本次改动文件**的发现是否阻断。
    # 只把改动文件提升为 blocking：仓库里已有的历史问题不该每一步都卡死。
    review_lint_blocking: bool = Field(
        default=True, alias="AGENT_REVIEW_LINT_BLOCKING"
    )

    # ---------- 子智能体（Phase 1：只读 Explore）----------
    # **默认关闭**：它会给主模型多一个工具、改变 agent 行为，打开后旧评测基线
    # 不再可比。评测时显式设置 AGENT_EXPLORE_ENABLED=true 做 A/B。
    explore_enabled: bool = Field(default=False, alias="AGENT_EXPLORE_ENABLED")
    # 子智能体最多几轮「模型 → 工具」；到顶后强制生成报告，状态记 partial。
    # 定位一个跨文件功能通常需要 6–10 轮只读检索；6 轮实测会频繁 partial。
    explore_max_rounds: int = Field(default=8, alias="AGENT_EXPLORE_MAX_ROUNDS")
    # 整个子智能体的墙钟上限（秒）
    explore_timeout: int = Field(default=120, alias="AGENT_EXPLORE_TIMEOUT")
    # 返回给主模型的摘要上限（字符）
    explore_max_report_chars: int = Field(default=4_000, alias="AGENT_EXPLORE_MAX_REPORT_CHARS")
    # 单个任务最多委派几次探索 —— 与主 agent 的工具轮次预算相互独立。
    # 默认给到 6：提示词鼓励把互不依赖的区域拆成多个并发 delegate，
    # 预算若只有 2 会与这条建议直接冲突（实测 5 个里 3 个被预算拒绝）。
    max_delegate_calls: int = Field(default=6, alias="AGENT_MAX_DELEGATE_CALLS")
    # 同一条消息里的多个 delegate 调用最多同时跑几个。
    # 子智能体只读，所以并发不会产生写冲突；限制主要是控制成本与 wsl.exe 压力。
    max_concurrent_agents: int = Field(default=4, alias="AGENT_MAX_CONCURRENT_AGENTS")
    # 同一条消息里的多个只读工具最多同时跑几个；写工具始终串行。
    max_concurrent_tools: int = Field(
        default=4, alias="AGENT_MAX_CONCURRENT_TOOLS"
    )
    # ---- 成本感知的委派预算（按仓库文件数分级）----
    # 小于 min_files：不委派，直接读；[min, medium)：medium_calls；
    # [medium, large)：large_calls；>= large：max_delegate_calls。
    delegate_min_files: int = Field(default=30, alias="AGENT_DELEGATE_MIN_FILES")
    delegate_medium_files: int = Field(default=100, alias="AGENT_DELEGATE_MEDIUM_FILES")
    delegate_large_files: int = Field(default=300, alias="AGENT_DELEGATE_LARGE_FILES")
    delegate_medium_calls: int = Field(default=2, alias="AGENT_DELEGATE_MEDIUM_CALLS")
    delegate_large_calls: int = Field(default=4, alias="AGENT_DELEGATE_LARGE_CALLS")

    # ---------- Orchestrator（Phase 3A）----------
    # 新架构默认启用：满足规模阈值的仓库走 harness；小仓库仍走 direct 快速路径。
    orchestrator_enabled: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_ENABLED"
    )
    # 小于该文件数不做编排，直接单 agent。
    orchestrator_min_files: int = Field(default=30, alias="AGENT_ORCHESTRATOR_MIN_FILES")
    orchestrator_max_tasks: int = Field(default=6, alias="AGENT_ORCHESTRATOR_MAX_TASKS")
    # 同一批 ready 任务最多并发几个只读 worker。
    orchestrator_max_concurrent_workers: int = Field(
        default=4, alias="AGENT_ORCHESTRATOR_MAX_CONCURRENT_WORKERS"
    )
    # 单个任务失败后最多重试几次（0 = 不重试）。重试仍失败才把依赖方标 blocked。
    orchestrator_task_retries: int = Field(
        default=1, alias="AGENT_ORCHESTRATOR_TASK_RETRIES"
    )
    # ---- Phase 3C：并发写 worker（默认关闭）----
    # 打开后，orchestrator 才可能生成 implement 任务；还要求 --write、工作区是
    # git 仓库且干净。worker 在独立 git worktree 中修改，完成后由宿主合并补丁。
    orchestrator_write_enabled: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_WRITE_ENABLED"
    )
    orchestrator_worker_rounds: int = Field(
        default=12, alias="AGENT_ORCHESTRATOR_WORKER_ROUNDS"
    )
    orchestrator_worker_repairs: int = Field(
        default=2, alias="AGENT_ORCHESTRATOR_WORKER_REPAIRS"
    )
    orchestrator_worker_timeout: int = Field(
        default=240, alias="AGENT_ORCHESTRATOR_WORKER_TIMEOUT"
    )
    # 交给主 agent 的 worker 汇总上限（字符）。
    orchestrator_context_chars: int = Field(
        default=8_000, alias="AGENT_ORCHESTRATOR_CONTEXT_CHARS"
    )

    # 写 worker 的 write_scope 在 I/O 前强制校验（三重防线之一）。
    orchestrator_write_scope_enforce: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_WRITE_SCOPE_ENFORCE"
    )
    # 允许多个 implement 修改同一文件；合并时用 git apply --3way。
    # 有真实冲突时整批回滚，不会把冲突标记落进主工作区。
    orchestrator_allow_overlap: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_ALLOW_OVERLAP"
    )
    # fan-in 是否落 artifact store 并输出结构化结果。
    orchestrator_fanout_artifacts: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_FANOUT_ARTIFACTS"
    )
    # 整个编排阶段（含所有 worker）的墙钟上限，0 表示不限制。
    orchestrator_run_deadline_s: int = Field(
        default=900, alias="AGENT_ORCHESTRATOR_RUN_DEADLINE_S"
    )
    # 单任务 token 上限，0 表示不限制（由 provider 回报的 usage 累计判断）。
    orchestrator_task_tokens: int = Field(
        default=0, alias="AGENT_ORCHESTRATOR_TASK_TOKENS"
    )
    # 整个编排阶段的 token 上限，0 = 不限制。scheduler 在派发前按角色预留，
    # 预留 + 已用超过上限时不再启动新任务。
    orchestrator_total_tokens: int = Field(
        default=0, alias="AGENT_ORCHESTRATOR_TOTAL_TOKENS"
    )
    # 在飞 implement + 待合并补丁的上限；超过后暂停启动新的 implement，
    # 先让合并队列排空，避免补丁/快照堆积。
    orchestrator_max_pending_merges: int = Field(
        default=8, alias="AGENT_ORCHESTRATOR_MAX_PENDING_MERGES"
    )
    # 遇到 429/限流时 scheduler 的首次退避秒数；之后指数增长，上限 30s。
    orchestrator_rate_limit_backoff_s: float = Field(
        default=2.0, alias="AGENT_ORCHESTRATOR_RATE_LIMIT_BACKOFF_S"
    )
    # scheduler 每完成多少个任务返回一次父图，形成 checkpoint/干预边界。
    orchestrator_checkpoint_every: int = Field(
        default=4, alias="AGENT_ORCHESTRATOR_CHECKPOINT_EVERY"
    )
    # worker 专用模型；空串表示继承 DEEPSEEK_MODEL。用于 explore/review 降本。
    orchestrator_worker_model: str = Field(
        default="", alias="AGENT_ORCHESTRATOR_WORKER_MODEL"
    )
    # fan-in 超预算/有冲突时才调用一次 reducer LLM；失败回退确定性汇总。
    orchestrator_fanin_reducer: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_FANIN_REDUCER"
    )
    # 只读模式下模型只给一个任务时，是否强行补一个「找测试」explore 任务。
    # 默认关闭：这会增加一次 worker 成本，只有演示并发/确实需要测试画像时才值得。
    orchestrator_add_explore_tests: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_ADD_EXPLORE_TESTS"
    )
    # 派发 implement 前一次性展示 write_scope 并等待确认（并发区不弹审批）。
    orchestrator_write_plan_approval: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_WRITE_PLAN_APPROVAL"
    )

    # ---------- 持久化与审计 ----------
    # 留空表示自动用 <当前目录>/.agent/checkpoints.sqlite；填 :memory: 强制不落盘
    checkpoint_path: str = Field(default="", alias="AGENT_CHECKPOINT_PATH")
    # 每个 thread/命名空间保留的 checkpoint 数；0 = 不轮转。
    # 每轮工具调用都会落一个 checkpoint，长期使用需要上限。
    checkpoint_keep: int = Field(default=200, alias="AGENT_CHECKPOINT_KEEP")
    # 有副作用工具的执行日志：崩溃后同一 call_id 不自动重放。
    tool_journal_enabled: bool = Field(
        default=True, alias="AGENT_TOOL_JOURNAL_ENABLED"
    )
    # 同一工作区只允许一个 runtime 运行；锁文件在审计目录下。
    workspace_lock_enabled: bool = Field(
        default=True, alias="AGENT_WORKSPACE_LOCK"
    )
    audit_enabled: bool = Field(default=True, alias="AGENT_AUDIT_ENABLED")
    # 留空表示自动用 <当前目录>/.agent/audit
    audit_dir: str = Field(default="", alias="AGENT_AUDIT_DIR")
    # 未消费 steering 指令的持久化目录；留空 = <当前目录>/.agent/steering
    steering_dir: str = Field(default="", alias="AGENT_STEERING_DIR")
    # 跨进程限流状态文件；留空 = <当前目录>/.agent/rate_limits.json；:memory: 关闭
    rate_limit_state_path: str = Field(
        default="", alias="AGENT_RATE_LIMIT_STATE_PATH"
    )
    # 单个审计文件超过该大小（MB）就轮转；0 表示不轮转
    audit_max_mb: int = Field(default=0, alias="AGENT_AUDIT_MAX_MB")

    # ---------- 安全 profile 与不可信仓库 ----------
    # local（默认，面向可信仓库）| untrusted（bwrap + 禁网 + 强制 ask +
    # 不信任仓库指令/记忆）。
    security_profile: str = Field(
        default="local", alias="AGENT_SECURITY_PROFILE"
    )
    # 仓库内的 AGENTS.md / CLAUDE.md 是否作为可信指令注入。
    trust_repo_instructions: bool = Field(
        default=True, alias="AGENT_TRUST_REPO_INSTRUCTIONS"
    )
    # 是否读取仓库内 `.agent/memory.md`；false 时记忆改存 memory_root。
    trust_repo_memory: bool = Field(
        default=True, alias="AGENT_TRUST_REPO_MEMORY"
    )
    # 长期记忆根目录（WSL 绝对路径）；空则按 trust_repo_memory 推导。
    memory_root: str = Field(default="", alias="AGENT_MEMORY_ROOT")
    # 秘密文件读保护 glob（逗号分隔）；.env.example 等模板自动放行。
    read_deny_globs: str = Field(
        default=DEFAULT_READ_DENY_GLOBS,
        alias="AGENT_READ_DENY_GLOBS",
    )

    # ---------- Web 多用户 ----------
    # JSON：{"token": "user"}；与 AGENT_WEB_AUTH_TOKENS_FILE 二选一。
    web_auth_tokens: str = Field(
        default="", alias="AGENT_WEB_AUTH_TOKENS"
    )
    web_auth_tokens_file: str = Field(
        default="", alias="AGENT_WEB_AUTH_TOKENS_FILE"
    )
    # per-user 工作区根目录；空 = 沙箱 $HOME/agent-ws/users。
    web_workspaces_root: str = Field(
        default="", alias="AGENT_WEB_WORKSPACES_ROOT"
    )
    # per-user 宿主侧数据根目录（审计/checkpoint/steering）；
    # 空 = <当前目录>/.agent/web/users。
    web_data_root: str = Field(default="", alias="AGENT_WEB_DATA_ROOT")
    # per-user WSL 侧记忆根目录；空 = 沙箱 $HOME/agent-memory。
    # 记忆在沙箱内、仓库之外，避免不可信仓库预置记忆。
    web_memory_root: str = Field(
        default="", alias="AGENT_WEB_MEMORY_ROOT"
    )
    web_max_body_bytes: int = Field(
        default=256 * 1024, alias="AGENT_WEB_MAX_BODY_BYTES"
    )
    web_max_prompt_chars: int = Field(
        default=100_000, alias="AGENT_WEB_MAX_PROMPT_CHARS"
    )
    web_rate_limit_per_min: int = Field(
        default=60, alias="AGENT_WEB_RATE_LIMIT_PER_MIN"
    )
    web_max_runtimes: int = Field(
        default=8, alias="AGENT_WEB_MAX_RUNTIMES"
    )
    web_max_runtimes_per_user: int = Field(
        default=2, alias="AGENT_WEB_MAX_RUNTIMES_PER_USER"
    )
    # 挂起 runtime 的空闲保留时间；超时后释放，resume 时从 checkpoint 重建。
    web_runtime_ttl_s: int = Field(
        default=1800, alias="AGENT_WEB_RUNTIME_TTL_S"
    )
    # 未配置 token 时是否允许 loopback 匿名访问（单用户本地模式）。
    web_allow_anonymous_local: bool = Field(
        default=True, alias="AGENT_WEB_ALLOW_ANONYMOUS_LOCAL"
    )

    # ---------- Agent 循环 ----------
    max_repair_rounds: int = Field(default=3, alias="AGENT_MAX_REPAIR_ROUNDS")
    # 连续出现同一失败签名多少次后停止 repair、改走 replan。
    # 24 次相同失败验证的教训：没有这道闸，repair 会把同一类修改重复到额度耗尽。
    max_same_failures: int = Field(default=2, alias="AGENT_MAX_SAME_FAILURES")
    # 执行中「重建剩余计划」的次数上限。计划可以修正，但不能无限改 ——
    # 边做边改没有收敛点，而每次重规划都要多一次模型调用
    max_replans: int = Field(default=2, alias="AGENT_MAX_REPLANS")
    max_tool_rounds: int = Field(default=12, alias="AGENT_MAX_TOOL_ROUNDS")
    max_plan_steps: int = Field(default=5, alias="AGENT_MAX_PLAN_STEPS")
    # 同一会话内「挂起→恢复」的总次数上限，防前端反复 resume 绕开 recursion_limit
    max_resumes: int = Field(default=50, alias="AGENT_MAX_RESUMES")
    # 一次完整运行（含审批/暂停后的 resume）的总墙钟上限，0 表示不限制。
    # 与 recursion_limit 互补：后者管步数，这里管时间（例如模型长时间卡住）。
    run_deadline_s: int = Field(default=1800, alias="AGENT_RUN_DEADLINE_S")
    # ask（逐条询问）| approve（自动放行 L2；L3 仍需确认）| deny（一律拒绝）
    approval_mode: str = Field(default="ask", alias="AGENT_APPROVAL_MODE")
    # 持久放行规则，逗号分隔的 `tool:glob`，例如
    # `shell_exec:git status*,run_tests:*`。只覆盖原本会 ASK 的 L2。
    allow_rules: str = Field(default="", alias="AGENT_ALLOW_RULES")
    llm_temperature: float = Field(default=0.0, alias="AGENT_LLM_TEMPERATURE")
    llm_timeout: int = Field(default=120, alias="AGENT_LLM_TIMEOUT")

    @property
    def context_budget(self) -> Any:
        """延迟导入：config 不该在导入期拉起 llm 层。"""
        from coding_agent.llm.context import ContextBudget

        return ContextBudget(
            max_chars=self.context_max_chars,
            max_tokens=self.context_max_tokens,
            keep_recent=self.context_keep_recent,
            tool_chars=self.context_tool_chars,
        )

    @property
    def resolved_checkpoint_path(self) -> str:
        """checkpoint 落盘位置；空配置落到当前目录的 .agent/ 下。"""
        return self.checkpoint_path or str(Path.cwd() / ".agent" / "checkpoints.sqlite")

    @property
    def resolved_audit_dir(self) -> Path:
        return Path(self.audit_dir) if self.audit_dir else Path.cwd() / ".agent" / "audit"

    @property
    def resolved_steering_dir(self) -> Path:
        return (
            Path(self.steering_dir)
            if self.steering_dir
            else Path.cwd() / ".agent" / "steering"
        )

    @property
    def resolved_rate_limit_state_path(self) -> str | None:
        raw = str(self.rate_limit_state_path or "").strip()
        if raw == ":memory:":
            return None
        return raw or str(Path.cwd() / ".agent" / "rate_limits.json")

    def apply_tracing_env(self) -> None:
        """把配置写回 os.environ，供 langsmith 自动读取。"""
        if not self.langsmith_tracing:
            os.environ["LANGSMITH_TRACING"] = "false"
            return
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_PROJECT"] = self.langsmith_project
        if self.langsmith_api_key:
            os.environ["LANGSMITH_API_KEY"] = self.langsmith_api_key


def apply_security_profile(settings: Settings) -> Settings:
    """把 `AGENT_SECURITY_PROFILE` 展开成具体的安全开关。

    `untrusted` 面向不可信仓库/公网：bwrap 不可用时 fail closed、命令无网络、
    L2/L3 全部人工确认、不注入仓库自带的指令/记忆、清空持久放行规则。
    """
    profile = str(settings.security_profile or "").strip().lower()
    if profile == "untrusted":
        update = {
            "shell_sandbox": "bwrap",
            "shell_network": False,
            "approval_mode": "ask",
            # 持久放行规则会把 L2 自动放行，与 untrusted 的“全部人工确认”冲突。
            "allow_rules": "",
            "trust_repo_instructions": False,
            "trust_repo_memory": False,
            "audit_enabled": True,
            "tool_journal_enabled": True,
            "workspace_lock_enabled": True,
        }
        if not str(settings.read_deny_globs or "").strip():
            update["read_deny_globs"] = DEFAULT_READ_DENY_GLOBS
        return settings.model_copy(update=update)

    if profile in PRODUCTION_PROFILES:
        # production/ops 是**只读审计档**：任何 L2/L3 变更都直接拒绝，
        # 写能力由 AgentRuntime/build_graph 强制关闭（见 allow_write 覆盖）。
        update = {
            "shell_sandbox": "bwrap",
            "shell_network": False,
            "approval_mode": "deny",
            "allow_rules": "",
            "trust_repo_instructions": False,
            "trust_repo_memory": False,
            "audit_enabled": True,
            "tool_journal_enabled": True,
            "workspace_lock_enabled": True,
        }
        if not str(settings.read_deny_globs or "").strip():
            update["read_deny_globs"] = DEFAULT_READ_DENY_GLOBS
        return settings.model_copy(update=update)

    return settings


def shadowed_env_keys(env_file: str = ".env") -> dict[str, str]:
    """找出「环境变量里有、但被 .env 覆盖掉」的键。

    优先级是 `.env` > 环境变量，所以这是**有意的**行为；但它同样静默 ——
    如果环境变量里那份才是你以为在生效的（比如 CI 注入的），
    结果就会与预期不符。doctor 把差异列出来，避免又一次「改了没生效」的排查。

    返回值：{键: ".env"（当前生效的来源）}。
    """
    from dotenv import dotenv_values

    try:
        file_values = {k: v for k, v in dotenv_values(env_file).items() if v}
    except OSError:
        return {}

    return {
        key: ".env"
        for key, value in file_values.items()
        if key in os.environ and os.environ[key] != value
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = apply_security_profile(Settings())
    settings.apply_tracing_env()
    return settings
