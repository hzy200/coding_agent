"""审计记录。

一条记录对应一次可追责的动作。字段按 `kind` 分组使用，用单一模型是为了
让 JSONL 日志能被 grep / jq 直接处理，不必按类型分文件。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# kind 取值
RUN_START = "run_start"
PLAN = "plan"
REPLAN = "replan"
TOOL_CALL = "tool_call"
FILE_CHANGE = "file_change"
ROLLBACK = "rollback"
VERIFY = "verify"
REVIEW = "review"
NUDGE = "nudge"
REPAIR = "repair"
SUBAGENT = "subagent"
WORKFLOW = "workflow"
TASK_GRAPH = "task_graph"
WORKER = "worker"
ORCHESTRATION = "orchestration"
STEERING = "steering"
RUN_END = "run_end"
RUN_ERROR = "run_error"
RECOVERY = "recovery"
CONCURRENCY = "concurrency"

# decision 取值
DECISION_AUTO = "auto"          # 策略判定为自动放行并已执行
DECISION_REJECTED = "rejected"  # 被安全策略拦下
DECISION_APPROVED = "approved"  # W5：经人工确认后执行
DECISION_DENIED = "denied"      # W5：人工拒绝


class AuditRecord(BaseModel):
    ts: str
    kind: str
    # 哈希链：prev_hash 是上一条记录的 hash；hash 是本条 canonical JSON 的摘要。
    # 旧日志没有这两个字段（空串），verify 会把它当作 legacy 前缀跳过。
    prev_hash: str = ""
    hash: str = ""

    thread_id: str = ""
    workspace: str = ""

    # ---- tool_call ----
    call_id: str = ""
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    level: str = ""
    decision: str = ""
    ok: bool | None = None
    exit_code: int | None = None
    duration_ms: int | None = None

    # ---- plan / run ----
    steps: list[str] = Field(default_factory=list)
    # 与 steps 逐项对齐的步骤类型（explore / implement / verify / unknown）。
    # 只在 plan/replan 记录里出现；None 会被 exclude_none 省略。
    kinds: list[str] | None = None
    # 提示词与最终答复都可能很长，落库前已截断
    detail: str = ""

    # ---- run_end：模型回报的 token 用量（provider 未提供时为 None）----
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    billed_usd: float | None = None
    # provider prefix cache 命中/未命中 token（未回报时为 None）
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None

    # ---- file_change ----
    path: str = ""
    action: str = ""
    added: int = 0
    removed: int = 0
    snapshot_id: str | None = None

    # ---- subagent / 子智能体产生的工具调用 ----
    # 子智能体的动作必须能追溯到「哪个委派、哪个子 agent」，否则主审计里
    # 只看到一次 delegate 调用，实际读了哪些文件完全不可见。
    agent_id: str | None = None
    parent_call_id: str | None = None
    task: str | None = None

    # ---- steering：干预来源（user / system / test）----
    source: str | None = None

    # ---- orchestrator / worker ----
    workflow: str | None = None
    task_id: str | None = None
    role: str | None = None
    worker_id: str | None = None
    attempt: int | None = None
    merged: bool | None = None
    # ---- worker 成本/隔离指标 ----
    artifact_id: str | None = None
    timed_out: bool | None = None
    queue_wait_ms: int | None = None
    llm_ms: int | None = None
    tool_ms: int | None = None
    depth: int | None = None
    priority: int | None = None
    scope_violations: list[str] = Field(default_factory=list)
