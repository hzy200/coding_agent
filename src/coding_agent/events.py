"""领域事件：前端与编排层之间的唯一契约。

前端（CLI / TUI / Web）只消费这些事件，不认识 LangGraph、不认识工具实现。
所有安全判定与编排都发生在 AgentRuntime 内部，前端无法绕过。

事件全部是 pydantic 模型，`model_dump()` 出来即可直接作为 SSE 数据帧。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str = ""


# --------------------------------------------------------------------------
# 规划与步进
# --------------------------------------------------------------------------

class PlanCreated(Event):
    """planner 完成分解。"""

    type: Literal["plan_created"] = "plan_created"
    steps: list[str] = Field(default_factory=list)


class StepStarted(Event):
    """进入某个子任务。total == 1 时前端可不渲染步进标题。"""

    type: Literal["step_started"] = "step_started"
    index: int = 0
    total: int = 1
    text: str = ""


class StepFinished(Event):
    """当前子任务收尾（模型给出了小结，或工具预算耗尽被强制叫停）。"""

    type: Literal["step_finished"] = "step_finished"
    index: int = 0
    budget_exhausted: bool = False
    text: str = ""


# --------------------------------------------------------------------------
# 生成与执行
# --------------------------------------------------------------------------

class AssistantToken(Event):
    """流式吐字。node 标明来自 act（步骤小结）还是 respond（最终答复）。"""

    type: Literal["assistant_token"] = "assistant_token"
    node: str = ""
    text: str = ""


class ToolCallStarted(Event):
    """模型请求了一次工具调用，即将执行。

    level 供前端着色，不是安全判定。call_id 用于和 ToolCallFinished 配对 ——
    同一轮可能有多个并行工具调用，只靠顺序无法可靠对应。
    """

    type: Literal["tool_call_started"] = "tool_call_started"
    call_id: str = ""
    name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    level: str | None = None


class ToolCallFinished(Event):
    """工具执行完毕。

    ok/rejected 语义：rejected 表示被安全策略拦下（此时 exit_code 为 None）；
    两者都为 False 表示执行了但失败。文件工具没有退出码，exit_code 恒为 None。
    """

    type: Literal["tool_call_finished"] = "tool_call_finished"
    call_id: str = ""
    name: str = ""
    ok: bool = False
    rejected: bool = False
    # auto / approved / denied / rejected —— 审计据此区分「谁拒的」
    decision: str = "auto"
    exit_code: int | None = None
    duration_ms: int | None = None
    level: str | None = None
    preview: str = ""


class FileChanged(Event):
    """文件被创建/覆盖/编辑。只有真正改动文件的动作才会发这个事件。"""

    type: Literal["file_changed"] = "file_changed"
    path: str = ""
    action: str = ""
    added: int = 0
    removed: int = 0
    diff: str = ""
    snapshot_id: str | None = None


class Verification(Event):
    """一步做完后自动跑验证的结果。

    status: ok / failed（跑过）、not_configured（没有可用的测试命令）、skipped（没跑）。
    """

    type: Literal["verification"] = "verification"
    status: str = "skipped"
    command: str = ""
    ok: bool = True
    summary: str = ""
    issues: list[str] = Field(default_factory=list)


class RepairStarted(Event):
    """验证失败后开始第 N 次修复。"""

    type: Literal["repair_started"] = "repair_started"
    attempt: int = 1
    limit: int = 0
    summary: str = ""
    issues: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 人工审批
# --------------------------------------------------------------------------

class ApprovalRequested(Event):
    """图已挂起，等待人工确认。

    前端收集答复后调用 `AgentRuntime.resume(thread_id, {request_id: bool})`。
    若事件流以本事件收尾（而不是 RunFinished），即表示处于挂起状态。
    """

    type: Literal["approval_requested"] = "approval_requested"
    request_id: str = ""
    tool: str = ""
    command: str = ""
    level: str = ""
    reason: str = ""


# --------------------------------------------------------------------------
# 收尾
# --------------------------------------------------------------------------

class RunFinished(Event):
    type: Literal["run_finished"] = "run_finished"
    thread_id: str = ""
    answer: str = ""


class RunFailed(Event):
    """编排层自身出错（模型/沙箱不可用等），与「工具执行失败」不同。"""

    type: Literal["run_failed"] = "run_failed"
    message: str = ""


EventType = (
    PlanCreated
    | StepStarted
    | StepFinished
    | AssistantToken
    | ToolCallStarted
    | ToolCallFinished
    | FileChanged
    | Verification
    | RepairStarted
    | ApprovalRequested
    | RunFinished
    | RunFailed
)
