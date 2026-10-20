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
from coding_agent.llm.ratelimit import GLOBAL_LIMITER
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.graph import mark_blocked, validate_proposals
from coding_agent.orchestrator.merge import merge_implement_patches_atomic
from coding_agent.orchestrator.workers import WorkerRunner
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.steering import SteeringController

SCHEDULER = "scheduler"

_ROLE_PRIORITY = {"implement": 30, "test": 20, "review": 20, "explore": 10}

# 派发前的 token 预留估计。真实用量在 worker 返回后结算；这里只需要一个
# 保守上界，避免把预算全部押在一个大任务上。
_ROLE_TOKEN_RESERVE = {
    "implement": 8_000,
    "review": 3_000,
    "explore": 2_000,
    "test": 500,
}


def _token_reserve(task: dict[str, Any]) -> int:
    raw = task.get("max_tokens")
    if raw is not None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return _ROLE_TOKEN_RESERVE.get(str(task.get("role", "")), 2_000)
_RATE_LIMIT_HINTS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota",
    "overloaded",
)


def _is_rate_limited(record: dict[str, Any]) -> bool:
    text = (
        f"{record.get('summary', '')} {record.get('error', '')}"
    ).lower()
    return any(hint in text for hint in _RATE_LIMIT_HINTS)


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

        max_limit = max(1, int(settings.orchestrator_max_concurrent_workers))
        semaphore = asyncio.Semaphore(max_limit)
        # 429/限流时临时降并发；成功后再逐步恢复到 max_limit。
        current_limit = max_limit
        rate_limit_until = 0.0
        rate_limit_streak = 0
        backoff_base = max(
            float(settings.orchestrator_rate_limit_backoff_s), 0.01
        )
        token_budget = max(0, int(settings.orchestrator_total_tokens))
        spent_tokens = sum(
            int(result.get("input_tokens") or 0)
            + int(result.get("output_tokens") or 0)
            for result in results
        )
        reserved_tokens = 0
        reserved_by_task: dict[str, int] = {}
        token_budget_exhausted = False
        ready: list[tuple[int, int, str]] = []
        queued: set[str] = set()
        seq = 0
        in_flight: dict[str, asyncio.Task[WorkerResult]] = {}
        future_task: dict[asyncio.Task[WorkerResult], str] = {}
        queued_at: dict[str, float] = {}
        # 已发出 worker_started 的 (worker_id, attempt)。取消/超时时只为它们
        # 发 finished；还在 semaphore 排队的任务由 runtime 的 updates 分支
        # 补发成对事件，避免前端看到“没有开始就结束”。
        started_keys: set[str] = set()
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
                started_keys.add(f"{worker_id}:{attempt}")
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
            nonlocal reserved_tokens
            for future in list(in_flight.values()):
                future.cancel()
            if in_flight:
                await asyncio.gather(
                    *in_flight.values(), return_exceptions=True
                )
            for task_id in list(in_flight):
                reserved_tokens = max(
                    0, reserved_tokens - reserved_by_task.pop(task_id, 0)
                )
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
                key = f"{result.worker_id}:{result.attempt}"
                if key in started_keys:
                    emit({"type": "worker_finished", "result": record})
            in_flight.clear()

        merge_pending: list[dict[str, Any]] = []

        def finalize(record: dict[str, Any]) -> None:
            """落状态、发事件、处理重试与动态提议。"""
            nonlocal dirty, run_dirty, seen
            nonlocal rate_limit_streak, current_limit, rate_limit_until
            task_id = str(record.get("task_id", ""))
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

            if _is_rate_limited(record):
                rate_limit_streak += 1
                current_limit = max(1, current_limit - 1)
                delay = min(
                    backoff_base * (2 ** (rate_limit_streak - 1)), 30.0
                )
                rate_limit_until = time.monotonic() + delay
                # 与其他 runtime 协调：进程内共享一个短暂全局冷却。
                GLOBAL_LIMITER.block_all(delay)
                emit(
                    {
                        "type": "worker_throttled",
                        "task_id": task_id,
                        "delay_s": delay,
                        "current_limit": current_limit,
                    }
                )
            else:
                rate_limit_streak = 0
                if (
                    record.get("status") in {"success", "partial"}
                    and current_limit < max_limit
                ):
                    current_limit += 1

            consume_proposals(record)
            seen = len(results)
            status.update(mark_blocked(tasks, status))

        def implement_active() -> bool:
            # 已跑完、正等合并的 implement 不算“还在跑”，否则 flush 永远不触发。
            pending_merge = {
                str(record.get("task_id", "")) for record in merge_pending
            }
            for task_id, task in by_id.items():
                if task.get("role") != "implement":
                    continue
                if task_id in in_flight:
                    return True
                if (
                    status.get(task_id) == "pending"
                    and task_id not in pending_merge
                ):
                    return True
            return False

        async def flush_merges() -> None:
            """没有 implement 在飞/待启动时，原子合并整批补丁。"""
            nonlocal dirty, run_dirty
            if not merge_pending:
                return
            batch = list(merge_pending)
            merge_pending.clear()
            if sandbox is None or not root:
                for record in batch:
                    finalize(record)
                return
            outcomes = await merge_implement_patches_atomic(
                sandbox,
                root,
                batch,
                allow_overlap=settings.orchestrator_allow_overlap,
            )
            for record in batch:
                task_id = str(record.get("task_id", ""))
                merged, merge_error, snapshot_ids = outcomes.get(
                    task_id, (False, "未合并", [])
                )
                record["merged"] = merged
                record["merge_error"] = merge_error
                record["snapshot_ids"] = snapshot_ids
                if merged:
                    dirty = True
                    run_dirty = True
                elif record.get("status") in {"success", "partial"}:
                    record["status"] = "failed"
                finalize(record)

        def discard_merges(reason: str) -> None:
            for record in merge_pending:
                record["status"] = "cancelled"
                record["merged"] = False
                record["merge_error"] = reason
                finalize(record)
            merge_pending.clear()

        while True:
            if cancelled():
                for task in tasks:
                    if status.get(str(task["id"]), "pending") == "pending":
                        status[str(task["id"])] = "cancelled"
                discard_merges("用户取消，未合并")
                await abandon_inflight(reason="已按用户要求取消", timed_out=False)
                break

            if scheduler_deadline and time.monotonic() >= scheduler_deadline:
                for task in tasks:
                    if status.get(str(task["id"]), "pending") == "pending":
                        status[str(task["id"])] = "blocked"
                discard_merges("编排 deadline 到期，未合并")
                await abandon_inflight(reason="编排 deadline 到期", timed_out=True)
                break

            refresh_ready()
            now = time.monotonic()
            shared_wait = GLOBAL_LIMITER.global_remaining()
            if shared_wait > 0:
                rate_limit_until = max(rate_limit_until, now + shared_wait)
            while (
                ready
                and not stop
                and launched_since_boundary < checkpoint_every
                and len(in_flight) < current_limit
                and now >= rate_limit_until
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
                reserve = _token_reserve(task)
                if token_budget and (
                    spent_tokens + reserved_tokens + reserve > token_budget
                ):
                    if in_flight:
                        # 还有任务在跑：等它结算完再重新评估预算。
                        heapq.heappush(
                            ready, (_neg_priority, _seq, task_id)
                        )
                        queued.add(task_id)
                        break
                    # 没有在飞任务且当前任务都放不下：预算耗尽，剩余任务阻塞。
                    for pending_task in tasks:
                        pending_id = str(pending_task["id"])
                        if status.get(pending_id, "pending") == "pending":
                            status[pending_id] = "blocked"
                    ready.clear()
                    queued.clear()
                    token_budget_exhausted = True
                    break
                attempts[task_id] = attempts.get(task_id, 0) + 1
                reserved_tokens += reserve
                reserved_by_task[task_id] = reserve
                queued_at[task_id] = time.monotonic()
                future = asyncio.create_task(run_one(task))
                in_flight[task_id] = future
                future_task[future] = task_id
                launched_since_boundary += 1

            if not in_flight:
                now = time.monotonic()
                if now < rate_limit_until:
                    # 所有任务都在限流退避中：等一小段再重试，而不是直接结束。
                    await asyncio.sleep(
                        min(rate_limit_until - now, 1.0)
                    )
                    continue
                break

            timeout = None
            timeouts: list[float] = []
            if scheduler_deadline:
                timeouts.append(
                    max(0.0, scheduler_deadline - time.monotonic())
                )
            if rate_limit_until:
                timeouts.append(
                    max(0.0, rate_limit_until - time.monotonic())
                )
            if timeouts:
                timeout = min(timeouts)
            done, _pending = await asyncio.wait(
                set(in_flight.values()),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                if (
                    scheduler_deadline
                    and time.monotonic() >= scheduler_deadline
                ):
                    for task in tasks:
                        if status.get(str(task["id"]), "pending") == "pending":
                            status[str(task["id"])] = "blocked"
                    discard_merges("编排 deadline 到期，未合并")
                    await abandon_inflight(
                        reason="编排 deadline 到期", timed_out=True
                    )
                    break
                # 限流退避时间到了，回到循环顶部重新派发。
                continue

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
                reserved_tokens = max(
                    0, reserved_tokens - reserved_by_task.pop(task_id, 0)
                )
                spent_tokens += int(record.get("input_tokens") or 0) + int(
                    record.get("output_tokens") or 0
                )
                # 取消可能正好在 worker 返回之后才被看到：这一批的写补丁
                # 一律丢弃，避免“取消了但还是合并进去”。
                if cancelled() and record.get("role") == "implement":
                    if record.get("status") in {"success", "partial"}:
                        record["status"] = "cancelled"
                        record["merged"] = False
                        record["merge_error"] = "用户取消，未合并"
                results.append(record)

                if (
                    record.get("role") == "implement"
                    and sandbox is not None
                    and root
                    and not cancelled()
                    and record.get("status") not in {"cancelled", "failed"}
                ):
                    # 先攒着：等本批 implement 全部结束后一次性原子合并。
                    merge_pending.append(record)
                else:
                    finalize(record)
                completed_since_boundary += 1
                if completed_since_boundary >= checkpoint_every:
                    stop = True

            if not implement_active():
                await flush_merges()
            if stop and not in_flight:
                break

        if merge_pending:
            if cancelled() or implement_active():
                discard_merges("同批 implement 未完成，未合并")
            else:
                await flush_merges()

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
            "token_budget_limit": token_budget,
            "token_budget_spent": spent_tokens,
            "token_budget_reserved": reserved_tokens,
            "token_budget_exhausted": token_budget_exhausted,
            "scheduler_done": not scheduler_continue,
            "scheduler_continue": scheduler_continue,
        }

    return scheduler
