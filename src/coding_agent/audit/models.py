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
TOOL_CALL = "tool_call"
FILE_CHANGE = "file_change"
ROLLBACK = "rollback"
VERIFY = "verify"
REPAIR = "repair"
RUN_END = "run_end"
RUN_ERROR = "run_error"

# decision 取值
DECISION_AUTO = "auto"          # 策略判定为自动放行并已执行
DECISION_REJECTED = "rejected"  # 被安全策略拦下
DECISION_APPROVED = "approved"  # W5：经人工确认后执行
DECISION_DENIED = "denied"      # W5：人工拒绝


class AuditRecord(BaseModel):
    ts: str
    kind: str

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
    # 提示词与最终答复都可能很长，落库前已截断
    detail: str = ""

    # ---- file_change ----
    path: str = ""
    action: str = ""
    added: int = 0
    removed: int = 0
    snapshot_id: str | None = None
