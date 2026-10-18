"""编排器的数据契约（Phase 3A）。

`TaskSpec` 是 orchestrator 交给 worker 的任务描述；`WorkerResult` 是 worker
回传给 fan-in 的结构化结果。两者都只包含只读能力 —— 并发写 worker 留到
Phase 3C。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

TaskRole = Literal["explore", "test", "review", "implement"]
WorkerStatus = Literal["success", "partial", "failed", "blocked", "cancelled"]


class TaskSpec(BaseModel):
    id: str = Field(description="任务唯一 id，例如 t1")
    role: TaskRole = Field(default="explore", description="explore 或 test")
    goal: str = Field(description="这个任务要回答/完成什么")
    inputs: list[str] = Field(
        default_factory=list, description="优先查看的文件、目录或符号"
    )
    acceptance: list[str] = Field(
        default_factory=list, description="怎样算完成"
    )
    depends_on: list[str] = Field(
        default_factory=list, description="必须先行完成的任务 id"
    )
    # full：依赖必须 success（写任务与实现相关分析的默认值）；
    # any：依赖 success 或 partial 即可开始（review/test 只需要产出，不需要
    # 依赖方通过完整验证）。
    depends_mode: Literal["full", "any"] = Field(
        default="full", description="依赖满足口径：full=必须 success；any=success/partial"
    )
    # Phase 3C：implement 任务必须声明允许修改的文件/目录；只读任务必须为空。
    write_scope: list[str] = Field(default_factory=list)
    # 该任务在 worktree 中允许的修复轮次（宿主默认值兜底）。
    max_repairs: int | None = None


class TaskGraph(BaseModel):
    tasks: list[TaskSpec] = Field(default_factory=list)
    # 模型没给出可用任务图时为 True；调用方会退化为单 explore 任务。
    degraded: bool = False


class WorkflowDecision(BaseModel):
    workflow: Literal[
        "direct", "parallel_readonly", "orchestrated_implement"
    ] = "direct"
    reason: str = ""


class WorkerResult(BaseModel):
    task_id: str
    role: str = "explore"
    worker_id: str = ""
    goal: str = ""
    status: WorkerStatus = "failed"
    summary: str = ""
    files: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    # 只读 worker 建议追加的任务；由 extend_tasks 统一校验后进入 pending。
    proposed_tasks: list[dict[str, Any]] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    # 这是该任务的第几次尝试（1 = 首次）
    attempt: int = 1
    # Phase 3C：implement worker 生成的补丁路径与合并结果
    patch_path: str = ""
    merged: bool = False
    merge_error: str = ""
    # 合并前为主工作区里被补丁改动的文件留下的快照 id（失败为空）。
    # 没有它，`agent undo` 无法回滚编排写 worker 合并出来的改动。
    snapshot_ids: list[str] = Field(default_factory=list)
    error: str = ""
    # worker 内部执行的工具调用（供审计层逐条落账）
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
