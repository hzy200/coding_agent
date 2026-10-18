"""事件驱动的动态任务图调度器。

与旧 `dispatch` 的区别：

- 旧实现一次取一批 ready 任务，`gather` 等整批结束再进入下一轮；
- 这里维护 ready 堆与 in-flight 集合，任一任务完成就立即解锁依赖方；
- 失败任务在额度内带上下文重试，额度用尽立即阻塞下游；
- worker 提议的只读任务在完成点即时校验并加入队列。

checkpoint/干预仍然通过**边界**实现：scheduler 运行到队列暂空或完成数达到
`orchestrator_checkpoint_every` 时，先 drain 在飞任务再返回；随后由
`steering_dispatch` 节点消费 pause/add/replan/cancel。这样不会在节点中途
`interrupt()` 而丢掉未提交的 worker 结果。
"""

from __future__ import annotations

import asyncio
import heapq
import time
from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from coding_agent.config import Settings
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.graph import mark_blocked, validate_proposals
from coding_agent.orchestrator.merge import merge_implement_patch
from coding_agent.orchestrator.workers import WorkerRunner
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.steering import SteeringController

SCHEDULER = "scheduler"

_ROLE_PRIORITY = {"implement": 30, "test": 20, "review": 20, "explore": 10}


def _priority(task: dict[str, Any]) -> int:
    raw = task.get("priority")
    if raw is None:
        return _ROLE_PRIORITY.get(str(task.get("role", "")), 10)
    try:
        return max(0, min(int(raw), 100))
    except (TypeError, ValueError):
        return _ROLE_PRIORITY.get(str(task.get("role", "")), 10)


def _deps_met(task: dict[str, Any], status: dict[str, str]) -> bool:
    for dep in task.get("depends_on") or []:
        value = status.get(str(dep))
        if value == "success":
            continue
        if value == "partial" and task.get("depends_mode") == "any":
            continue
        return False
    return True


def _wire_implement_dependents(tasks: list[dict[str, Any]]) -> None:
    """把 test/review 自动挂到 implement 之后。

    否则它们会和写 worker 同批启动，看到的是合并前状态，产出天然是
    partial/blocked。`any` 允许消费 partial 的写产出。
    """
    implement_ids = [
        str(task["id"]) for task in tasks if task.get("role") == "implement"
    ]
    if not implement_ids:
        return
    for task in tasks:
        if task.get("role") not in {"test", "review"}:
            continue
        if task.get("depends_on"):
            continue
        task["depends_on"] = list(implement_ids)
        task["depends_mode"] = "any"


def _task_depth(tasks: list[dict[str, Any]], task_id: str) -> int:
    by_id = {str(task["id"]): task for task in tasks}
    seen: set[str] = set()

    def walk(node: str) -> int:
        if node in seen:
            return 0
        seen.add(node)
        deps = by_id.get(node, {}).get("depends_on") or []
        return 1 + max((walk(str(dep)) for dep in deps), default=0)

    return walk(task_id)


def _retry_context(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt": int(result.get("attempt", 1)),
        "status": str(result.get("status", "")),
        "summary": str(result.get("summary", ""))[:1_000],
        "error": str(result.get("error", ""))[:1_000],
        "findings": [str(item) for item in (result.get("findings") or [])[:8]],
        "verification": result.get("verification") or {},
    }


def make_scheduler_node(
    *,
    worker_runner: WorkerRunner,
    settings: Settings,
    sandbox: WslSandbox | None = None,
    root: str = "",
    steering: SteeringController | None = None,
) -> Callable[[dict[str, Any], RunnableConfig], Any]:
    async def scheduler(
        state: dict[str, Any], config: RunnableConfig
    ) -> dict[str, Any]:
        thread_id = str(
            (((config or {}).get("configurable")) or {}).get("thread_id", "")
        )
        request = str(state.get("request") or "")
        cwd = str(state.get("cwd", ""))

        tasks = [dict(task) for task in (state.get("tasks") or [])]
        status = dict(state.get("task_status") or {})
        attempts = dict(state.get("task_attempts") or {})
        results = list(state.get("worker_results") or [])
        round_no = int(state.get("orchestrator_round", 0))
        seen = int(state.get("task_extensions_seen", 0))
        applied: list[dict[str, Any]] = []
        extension_errors: list[str] = []
        dirty = bool(state.get("dirty"))
        run_dirty = bool(state.get("run_dirty"))
        scheduler_deadline = float(state.get("scheduler_deadline") or 0.0)
        if scheduler_deadline <= 0:
            seconds = int(settings.orchestrator_run_deadline_s or 0)
            scheduler_deadline = (
                time.monotonic() + seconds if seconds > 0 else 0.0
            )

        _wire_implement_dependents(tasks)
        by_id = {str(task["id"]): task for task in tasks}

        # 失败但还有重试额度的任务回到 pending（恢复/重入时也需要）。
        for task in tasks:
            task_id = str(task["id"])
            if (
                status.get(task_id) == "failed"
                and attempts.get(task_id, 0)
                <= settings.orchestrator_task_retries
            ):
                status[task_id] = "pending"
        status = mark_blocked(tasks, status)

        def cancelled() -> bool:
            return bool(steering is not None and steering.cancel_requested(thread_id))

        def emit(payload: dict[str, Any]) -> None:
            try:
                writer = get_stream_writer()
            except Exception:  # noqa: BLE001 - 单测直接调用节点时没有 writer
                return
            try:
                writer(payload)
            except Exception:  # noqa: BLE001 - 前端断开不拖垮调度
                pass

        def consume_proposals(result: dict[str, Any]) -> None:
            if result.get("status") not in {"success", "partial"}:
                return
            proposals = (result.get("proposed_tasks") or [])[:2]
            if not proposals:
                return
            new_tasks, errors = validate_proposals(
                proposals,
                existing=tasks,
                max_total=settings.orchestrator_max_tasks,
            )
            extension_errors.extend(errors)
            for task in new_tasks:
                task["depth"] = _task_depth(tasks, str(task["id"]))
                tasks.append(task)
                by_id[str(task["id"])] = task
                status[str(task["id"])] = "pending"
                applied.append(task)

        # 上次调用已经产生、但还没扫描提议的结果（正常不会发生，兼容恢复）。
        for result in results[seen:]:
            consume_proposals(result)
        seen = len(results)

        semaphore = asyncio.Semaphore(
            max(1, int(settings.orchestrator_max_concurrent_workers))
        )
        ready: list[tuple[int, int, str]] = []
        queued: set[str] = set()
        seq = 0
        in_flight: dict[str, asyncio.Task[WorkerResult]] = {}
        future_task: dict[asyncio.Task[WorkerResult], str] = {}
        queued_at: dict[str, float] = {}
        stop = False
        completed_since_boundary = 0
        launched_since_boundary = 0
        checkpoint_every = max(1, int(settings.orchestrator_checkpoint_every or 1))

        def refresh_ready() -> None:
            nonlocal seq
            for task in tasks:
                task_id = str(task["id"])
                if status.get(task_id, "pending") != "pending":
                    continue
                if task_id in in_flight or task_id in queued:
                    continue
                if not _deps_met(task, status):
                    continue
                heapq.heappush(
                    ready, (-_priority(task), seq, task_id)
                )
                seq += 1
                queued.add(task_id)

        async def run_one(task: dict[str, Any]) -> WorkerResult:
            task_id = str(task["id"])
            role = str(task.get("role", "explore"))
            worker_id = f"{role}:{task_id}"
            attempt = attempts.get(task_id, 1)
            started_wait = queued_at.get(task_id, time.monotonic())
            async with semaphore:
                queue_wait_ms = int(
                    (time.monotonic() - started_wait) * 1000
                )
                emit(
                    {
                        "type": "worker_started",
                        "worker_id": worker_id,
                        "task_id": task_id,
                        "role": role,
                        "goal": str(task.get("goal", "")),
                        "attempt": attempt,
                        "queue_wait_ms": queue_wait_ms,
                    }
                )
                try:
                    result = await worker_runner(
                        task,
                        parent_request=request,
                        cwd=cwd,
                        config=config,
                        cancel=cancelled,
                    )
                except Exception as exc:  # noqa: BLE001 - 单 worker 失败不拖垮同批
                    result = WorkerResult(
                        task_id=task_id,
                        role=role,
                        worker_id=worker_id,
                        goal=str(task.get("goal", "")),
                        status="failed",
                        summary=f"worker 异常：{type(exc).__name__}: {exc}",
                        error=str(exc),
                    )
                result.attempt = attempt
                result.queue_wait_ms = queue_wait_ms
                result.priority = _priority(task)
                result.depth = int(task.get("depth", 0))
                return result

        if not tasks:
            return {
                "scheduler_done": True,
                "scheduler_continue": False,
                "scheduler_deadline": scheduler_deadline,
                "task_extensions_applied": applied,
                "task_extension_errors": extension_errors,
                "task_extensions_seen": seen,
            }

        async def abandon_inflight(*, reason: str, timed_out: bool) -> None:
            """deadline/cancel：取消在飞任务并补齐结构化结果。

            worker 的取消是协作式的；这里给不出无限等待的保证，所以到点
            直接 cancel。worktree 清理由 implement runner 的 finally 兜底。
            """
            for future in list(in_flight.values()):
                future.cancel()
            if in_flight:
                await asyncio.gather(
                    *in_flight.values(), return_exceptions=True
                )
            for task_id in list(in_flight):
                task = by_id.get(task_id, {})
                result = WorkerResult(
                    task_id=task_id,
                    role=str(task.get("role", "explore")),
                    worker_id=f"{task.get('role', 'explore')}:{task_id}",
                    goal=str(task.get("goal", "")),
                    status="cancelled" if not timed_out else "failed",
                    summary=reason,
                    timed_out=timed_out,
                )
                result.attempt = attempts.get(task_id, 1)
                record = result.model_dump()
                results.append(record)
                status[task_id] = str(record["status"])
                emit({"type": "worker_finished", "result": record})
            in_flight.clear()

        while True:
            if cancelled():
                for task in tasks:
                    if status.get(str(task["id"]), "pending") == "pending":
                        status[str(task["id"])] = "cancelled"
                await abandon_inflight(reason="已按用户要求取消", timed_out=False)
                break

            if scheduler_deadline and time.monotonic() >= scheduler_deadline:
                for task in tasks:
                    if status.get(str(task["id"]), "pending") == "pending":
                        status[str(task["id"])] = "blocked"
                await abandon_inflight(reason="编排 deadline 到期", timed_out=True)
                break

            refresh_ready()
            while (
                ready
                and not stop
                and launched_since_boundary < checkpoint_every
            ):
                _neg_priority, _seq, task_id = heapq.heappop(ready)
                queued.discard(task_id)
                if status.get(task_id, "pending") != "pending":
                    continue
                if task_id in in_flight:
                    continue
                task = by_id.get(task_id)
                if task is None:
                    continue
                attempts[task_id] = attempts.get(task_id, 0) + 1
                queued_at[task_id] = time.monotonic()
                future = asyncio.create_task(run_one(task))
                in_flight[task_id] = future
                future_task[future] = task_id
                launched_since_boundary += 1

            if not in_flight:
                break

            timeout = None
            if scheduler_deadline:
                timeout = max(0.0, scheduler_deadline - time.monotonic())
            done, _pending = await asyncio.wait(
                set(in_flight.values()),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                for task in tasks:
                    if status.get(str(task["id"]), "pending") == "pending":
                        status[str(task["id"])] = "blocked"
                await abandon_inflight(reason="编排 deadline 到期", timed_out=True)
                break

            for future in done:
                task_id = future_task.pop(future)
                in_flight.pop(task_id, None)
                try:
                    result = future.result()
                except asyncio.CancelledError:
                    result = WorkerResult(
                        task_id=task_id,
                        role=str(by_id.get(task_id, {}).get("role", "explore")),
                        worker_id=f"{by_id.get(task_id, {}).get('role', 'explore')}:{task_id}",
                        goal=str(by_id.get(task_id, {}).get("goal", "")),
                        status="cancelled",
                        summary="调度器取消或 deadline 到期",
                        timed_out=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    result = WorkerResult(
                        task_id=task_id,
                        role=str(by_id.get(task_id, {}).get("role", "explore")),
                        worker_id=f"{by_id.get(task_id, {}).get('role', 'explore')}:{task_id}",
                        status="failed",
                        summary=f"worker 异常：{type(exc).__name__}: {exc}",
                        error=str(exc),
                    )
                result.attempt = attempts.get(task_id, 1)
                record = result.model_dump()
                results.append(record)

                if (
                    record.get("role") == "implement"
                    and sandbox is not None
                    and root
                    and not cancelled()
                ):
                    merged, merge_error, snapshot_ids = await merge_implement_patch(
                        sandbox, root, record
                    )
                    record["merged"] = merged
                    record["merge_error"] = merge_error
                    record["snapshot_ids"] = snapshot_ids
                    if merged:
                        dirty = True
                        run_dirty = True
                    elif record.get("status") in {"success", "partial"}:
                        record["status"] = "failed"
                # implement 的最终事件要等合并结果确定后再发；只读 worker
                # 也统一在这里发，保证 custom 与 updates 两条通道看到同一份结果。
                emit({"type": "worker_finished", "result": record})

                attempt = int(record.get("attempt", 1))
                if (
                    record.get("status") == "failed"
                    and attempt <= settings.orchestrator_task_retries
                ):
                    status[task_id] = "pending"
                    task = by_id.get(task_id)
                    if task is not None:
                        task["retry_context"] = _retry_context(record)
                else:
                    status[task_id] = str(record.get("status", "failed"))

                consume_proposals(record)
                seen = len(results)
                status = mark_blocked(tasks, status)
                completed_since_boundary += 1
                if completed_since_boundary >= checkpoint_every:
                    stop = True

            if stop and not in_flight:
                break

        # 清掉还没启动的排队任务状态：依赖失败已由 mark_blocked 处理；
        # 仍然 pending 但无 ready 的图（理论上只有环）标记 blocked，防止重入死循环。
        status = mark_blocked(tasks, status)
        refresh_ready()
        if not in_flight:
            pending_left = [
                str(task["id"])
                for task in tasks
                if status.get(str(task["id"]), "pending") == "pending"
            ]
            if pending_left and not ready:
                for task_id in pending_left:
                    status[task_id] = "blocked"

        pending_any = any(
            status.get(str(task["id"]), "pending") == "pending"
            for task in tasks
        )
        scheduler_continue = bool(pending_any and not cancelled())
        if scheduler_deadline and time.monotonic() >= scheduler_deadline:
            scheduler_continue = False

        return {
            "tasks": tasks,
            "task_status": status,
            "task_attempts": attempts,
            "worker_results": results,
            "orchestrator_round": round_no + 1,
            "task_extensions_seen": seen,
            "task_extensions_applied": applied,
            "task_extension_errors": extension_errors,
            "dirty": dirty,
            "run_dirty": run_dirty,
            "scheduler_deadline": scheduler_deadline,
            "scheduler_done": not scheduler_continue,
            "scheduler_continue": scheduler_continue,
        }

    return scheduler
