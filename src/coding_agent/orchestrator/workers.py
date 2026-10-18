"""只读 worker runner（Phase 3A）。

- `explore`：复用现有 Explore 子智能体（独立上下文 + 只读工具）；
- `test`：复用宿主测试 runner（确定性，不调 LLM）。

两者都返回结构化 `WorkerResult`，由 dispatch 统一汇总。Phase 3A 不允许写。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from coding_agent.agents.explore import DelegateRunner
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.implement import ImplementRunner
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.tools.testrun import run_verification

WorkerRunner = Callable[..., Awaitable[WorkerResult]]


def make_worker_runner(
    *,
    explore_runner: DelegateRunner | None,
    review_runner: DelegateRunner | None = None,
    implement_runner: ImplementRunner | None = None,
    sandbox: WslSandbox,
    root: str,
) -> WorkerRunner:
    async def run(
        task: dict[str, Any],
        *,
        parent_request: str,
        cwd: str,
        config: RunnableConfig | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> WorkerResult:
        task_id = str(task.get("id", ""))
        role = str(task.get("role", "explore"))
        goal = str(task.get("goal", ""))
        worker_id = f"{role}:{task_id}"

        if role in {"explore", "review"}:
            runner = explore_runner if role == "explore" else review_runner
            if runner is None:
                return WorkerResult(
                    task_id=task_id,
                    role=role,
                    worker_id=worker_id,
                    goal=goal,
                    status="failed",
                    summary=f"{role} worker 未配置（orchestrator 缺少 runner）",
                )
            artifact = await runner(
                args={
                    "goal": goal,
                    "paths": list(task.get("inputs") or []),
                    "reason": f"编排器派发的只读 {role} 任务",
                },
                call_id=task_id,
                parent_request=parent_request,
                cwd=cwd,
                config=config,
                cancel=cancel,
            )
            return WorkerResult(
                task_id=task_id,
                role=role,
                worker_id=worker_id,
                goal=goal,
                status=artifact.status,
                summary=artifact.summary,
                files=list(artifact.relevant_files),
                findings=list(artifact.findings),
                proposed_tasks=list(artifact.proposed_tasks),
                input_tokens=artifact.input_tokens,
                output_tokens=artifact.output_tokens,
                duration_ms=artifact.duration_ms,
            )

        if role == "implement":
            if implement_runner is None:
                return WorkerResult(
                    task_id=task_id,
                    role=role,
                    worker_id=worker_id,
                    goal=goal,
                    status="failed",
                    summary="实现 worker 未配置（需要开启写编排 + --write + 干净 git 仓库）",
                )
            return await implement_runner(
                task,
                parent_request=parent_request,
                cwd=cwd,
                config=config,
                cancel=cancel,
            )

        if role == "test":
            if cancel is not None and cancel():
                return WorkerResult(
                    task_id=task_id,
                    role=role,
                    worker_id=worker_id,
                    goal=goal,
                    status="cancelled",
                    summary="已按用户要求取消。",
                )
            result = await asyncio.to_thread(
                run_verification, sandbox, root, allow_manifest=False
            )
            if result.status == "ok":
                status = "success"
            elif result.status == "failed":
                status = "failed"
            else:
                # not_configured / skipped：没验证，不等于通过
                status = "partial"
            findings = [
                f"{issue.location} {issue.message}".strip()
                for issue in result.issues
            ]
            if result.failed_tests:
                findings.append("失败测试：" + "、".join(result.failed_tests))
            return WorkerResult(
                task_id=task_id,
                role=role,
                worker_id=worker_id,
                goal=goal,
                status=status,
                summary=result.summary or result.render(),
                findings=findings,
                verification=result.model_dump(),
                duration_ms=result.duration_ms or 0,
            )

        return WorkerResult(
            task_id=task_id,
            role=role,
            worker_id=worker_id,
            goal=goal,
            status="failed",
            summary=f"Phase 3A 不支持 worker 角色：{role}",
        )

    return run
