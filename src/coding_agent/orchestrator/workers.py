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
from coding_agent.instructions import load_project_instructions
from coding_agent.memory.longterm import LongTermMemory
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.implement import ImplementRunner
from coding_agent.orchestrator.retry import format_retry_context
from coding_agent.sandbox.fs import SandboxFs
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
    memory_top_k: int = 8,
) -> WorkerRunner:
    # 并发 worker 共享同一份记忆读取；只读文件，不持有业务状态
    memory = LongTermMemory(SandboxFs(sandbox, root), root)
    # 指令加载要起一次 WSL `find`：延迟到第一次真正跑 worker 时再做，
    # 纯 direct 会话不为编排功能付启动成本。
    instructions_cache: list[str] = []

    def _instructions() -> str:
        if not instructions_cache:
            try:
                text = load_project_instructions(sandbox, root)
            except Exception:  # noqa: BLE001 - 指令读取失败不阻断 worker
                text = ""
            instructions_cache.append(text)
        return instructions_cache[0]

    def _memories(task: dict[str, Any], parent_request: str) -> list[str]:
        """按任务目标筛 top-K；记忆读取失败不能阻断 worker。"""
        query = f"{task.get('goal', '')}\n{parent_request}"
        try:
            return memory.search(query, memory_top_k)
        except Exception:  # noqa: BLE001 - 记忆是增强信息，不是执行前提
            return []

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
        retry_note = format_retry_context(task)
        worker_id = f"{role}:{task_id}"
        memories = _memories(task, parent_request)

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
                    "goal": f"{goal}\n{retry_note}" if retry_note else goal,
                    "paths": list(task.get("inputs") or []),
                    "repo_map": str(task.get("repo_map") or ""),
                    "memories": memories,
                    "instructions": _instructions(),
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
                llm_ms=artifact.llm_ms,
                tool_ms=artifact.tool_ms,
                timed_out=artifact.timed_out,
                budget_used={
                    "input_tokens": artifact.input_tokens,
                    "output_tokens": artifact.output_tokens,
                },
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
                memories=memories,
                instructions=_instructions(),
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
