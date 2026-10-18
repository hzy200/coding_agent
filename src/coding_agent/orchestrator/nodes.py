"""Phase 3A 编排节点：survey → select → plan → dispatch ⇄ synthesize。"""

from __future__ import annotations

import asyncio
import posixpath
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from coding_agent.config import Settings
from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import with_json_output
from coding_agent.messages import last_human_text
from coding_agent.orchestrator.contracts import TaskGraph, WorkerResult
from coding_agent.orchestrator.graph import (
    mark_blocked,
    ready_tasks,
    validate_proposals,
    validate_tasks,
)
from coding_agent.orchestrator.implement import stage_args
from coding_agent.orchestrator.workers import WorkerRunner
from coding_agent.sandbox.fs import SandboxFs, SandboxFsError, SandboxPathError
from coding_agent.sandbox.snapshots import MAX_OPERATIONAL_BYTES, SnapshotStore
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.steering import SteeringController
from coding_agent.tools.testrun import detect_test_command


def _default_write_task(
    request: str, summary: dict[str, Any]
) -> dict[str, Any] | None:
    """写模式下的保守兜底：单个 implement，覆盖源码顶层目录。

    任务图校验失败时不要让写路径静默消失；一个只写 worker 也能验证
    worktree/补丁/合并链路，而且不会与其它写任务冲突。
    """
    dirs = [
        str(item).strip().strip("/")
        for item in (summary.get("top_dirs") or [])
        if str(item).strip() and str(item).strip() not in {"tests", "test", ".agent"}
    ]
    # 扁平仓库（顶层只有文件 / 只有 tests）没有可用目录；退到顶层文件，
    # 否则写路径会在“降级”名义下悄悄消失。
    files = [
        str(item).strip().strip("/")
        for item in (summary.get("top_files") or [])
        if str(item).strip() and not str(item).startswith(".")
    ]
    scope = [f"{directory}/" for directory in dirs] if dirs else files
    if not scope:
        return None
    return {
        "id": "implement-1",
        "role": "implement",
        "goal": request or "完成用户请求",
        "inputs": [],
        "acceptance": [],
        "depends_on": [],
        "write_scope": scope,
    }


PLAN_TASKS_PROMPT = """\
你是编码智能体的编排器。把任务拆成 1–{max_tasks} 个子任务，交给并发 worker：

{role_notes}

{write_rules}

硬性要求：
- 每个任务必须有一个清晰、可独立完成的目标；
- 只有确实需要前序结果时才写 depends_on；
- `review` 通常应独立于 `test`（除非要专门审查测试结果），否则测试失败会把审查一起阻塞；
- 不要把一条连贯的功能线拆成多个重复的 explore；
- review/test 若只需要依赖任务的产出（即使它最终是 partial），把 depends_mode
  设为 "any"；其余任务保持 "full"（默认）。
- 只输出 JSON：{{"tasks": [{{"id": "t1", "role": "explore", "goal": "...",
  "inputs": [], "acceptance": [], "depends_on": [], "depends_mode": "full",
  "write_scope": []}}]}}
"""

_READ_ONLY_ROLE_NOTES = """\
- `explore`：定位/理解代码。给出 goal、inputs（优先看的文件/目录）、acceptance。
- `test`：运行项目现有测试并总结失败项。无需 inputs。
- `review`：对 inputs 中的文件做只读代码审查，返回风险/遗漏。不要修改文件。
- `test`/`review` 依赖实现任务时可用 depends_mode="any"：实现任务的产出
  （文件/摘要）已经可用即可，不要求它通过完整验证。"""

_IMPLEMENT_ROLE_NOTES = """\
- `implement`：修改 `write_scope` 内的文件完成任务。必须给出 write_scope
  （文件或目录，例如 "app/reporting.py" 或 "app/handlers/"）。
- `explore` / `test` / `review`：只读辅助任务，不能修改文件。
- `test`/`review` 依赖 implement 时可用 depends_mode="any" 消费 partial 产出。"""

_READ_ONLY_RULES = "- 只允许 explore / test / review，**不要生成实现/修改任务**。"

_IMPLEMENT_RULES = """\
- 允许 explore / test / review / implement。
- 每个 implement 必须写 write_scope；不同 implement 的 write_scope **不能重叠**。
- 如果用户请求点名了具体文件（例如「在 app/alpha.py 实现…」），write_scope
  必须**精确到这些文件**，不要用整个目录。
- implement 任务之间**不能有 depends_on**（它们基于同一个 HEAD 并发修改）。
- implement 只改源码，不要改测试文件（除非任务明确要求）。"""


def make_survey_node(
    sandbox: WslSandbox, root: str
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    def survey(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        summary = dict(state.get("repo_profile") or {})
        top_entries: dict[str, list[str]] = {"dirs": [], "files": []}
        try:
            dirs_result = sandbox.run(
                "find . -maxdepth 1 -mindepth 1 -type d -not -name '.*' | sort",
                cwd=root,
            )
            if dirs_result.ok:
                top_entries["dirs"] = [
                    line.strip()
                    for line in dirs_result.stdout.splitlines()
                    if line.strip()
                ][:20]
            files_result = sandbox.run(
                "find . -maxdepth 1 -mindepth 1 -type f -not -name '.*' | sort",
                cwd=root,
            )
            if files_result.ok:
                top_entries["files"] = [
                    line.strip()
                    for line in files_result.stdout.splitlines()
                    if line.strip()
                ][:20]
        except Exception:  # noqa: BLE001 - survey 失败不能拖垮 run
            pass

        try:
            test_command = detect_test_command(sandbox, root, allow_manifest=False)
        except Exception:  # noqa: BLE001
            test_command = ""

        # Phase 3C 的写隔离要求：git 仓库根、工作区干净（不含 .agent/ 等
        # agent 自己的产物）。工作区是仓库子目录时 patch 路径会错位，直接禁用。
        is_git = False
        git_root = False
        git_clean = False
        try:
            inside = sandbox.run(
                "git rev-parse --is-inside-work-tree", cwd=root
            )
            is_git = inside.ok and inside.stdout.strip().endswith("true")
            if is_git:
                toplevel = sandbox.run("git rev-parse --show-toplevel", cwd=root)
                physical = sandbox.run("pwd -P", cwd=root)
                git_root = bool(
                    toplevel.ok
                    and physical.ok
                    and toplevel.stdout.strip().rstrip("/")
                    == physical.stdout.strip().rstrip("/")
                )
                status = sandbox.run(
                    f"git status --porcelain -- . {stage_args()}", cwd=root
                )
                git_clean = status.ok and not status.stdout.strip()
        except Exception:  # noqa: BLE001
            is_git = False

        summary["top_dirs"] = top_entries["dirs"]
        summary["top_files"] = top_entries["files"]
        summary["test_command"] = test_command
        summary["git"] = is_git
        summary["git_root"] = git_root
        summary["git_clean"] = git_clean
        return {"repo_summary": summary}

    return survey


def _completed_view(
    tasks: list[dict[str, Any]],
    status: dict[str, str],
    results: list[dict[str, Any]],
) -> str:
    """给重规划模型看的「已完成，不要重复」清单。"""
    latest: dict[str, dict[str, Any]] = {}
    for result in results:
        task_id = str(result.get("task_id", ""))
        previous = latest.get(task_id)
        if previous is None or int(result.get("attempt", 1)) >= int(
            previous.get("attempt", 1)
        ):
            latest[task_id] = result
    lines: list[str] = []
    for task in tasks:
        task_id = str(task.get("id", ""))
        item = latest.get(task_id) or {}
        summary = str(item.get("summary", "")).strip()[:160]
        merged = "，已合并" if item.get("merged") else ""
        lines.append(
            f"- {task_id} ({task.get('role', '')}/{status.get(task_id, '')})"
            f"{merged}: {summary}"
        )
    return "\n".join(lines) or "（无）"


def make_plan_tasks_node(
    llm: BaseChatModel, settings: Settings, *, allow_write: bool = False
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    structured = with_json_output(llm, TaskGraph).with_retry(stop_after_attempt=2)

    async def plan_tasks(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        request = last_human_text(state["messages"])
        summary = state.get("repo_summary") or {}
        replan_mode = bool(state.get("steering_replan"))
        existing_tasks = [dict(task) for task in (state.get("tasks") or [])]
        existing_status = dict(state.get("task_status") or {})
        results = state.get("worker_results") or []
        completed_tasks = [
            task
            for task in existing_tasks
            if existing_status.get(task["id"]) in {"success", "partial"}
        ]
        merged_any = any(
            result.get("role") == "implement" and result.get("merged")
            for result in results
        )
        # 已合并的 implement 补丁留在主工作区；新 worktree 基于 HEAD，
        # 看不到这些改动，因此重规划后只能安全地生成只读任务。
        write_mode = bool(
            allow_write
            and settings.orchestrator_write_enabled
            and state.get("workflow") == "orchestrated_implement"
            and not merged_any
        )
        body = (
            f"用户请求：{request}\n\n"
            f"仓库画像：{summary}\n\n"
            f"工作流：{state.get('workflow', '')}\n\n"
        )
        if replan_mode:
            body += (
                "用户要求重新规划剩余工作。以下任务已经完成，不要重复：\n"
                f"{_completed_view(completed_tasks, existing_status, results)}\n\n"
            )
            if merged_any:
                body += "注意：已有实现补丁合并；新增任务只能使用 explore/test/review。\n\n"
        body += "请生成任务图。"

        tasks: list[dict[str, Any]] = []
        degraded = False
        errors: list[str] = []
        try:
            graph = await structured.ainvoke(
                [
                    SystemMessage(
                        content=PLAN_TASKS_PROMPT.format(
                            max_tasks=settings.orchestrator_max_tasks,
                            role_notes=(
                                _IMPLEMENT_ROLE_NOTES
                                if write_mode
                                else _READ_ONLY_ROLE_NOTES
                            ),
                            write_rules=(
                                _IMPLEMENT_RULES
                                if write_mode
                                else _READ_ONLY_RULES
                            ),
                        )
                    ),
                    HumanMessage(content=body),
                ],
                config,
            )
            tasks, degraded, errors = validate_tasks(
                graph.tasks,
                request=request,
                max_tasks=settings.orchestrator_max_tasks,
                allow_write=write_mode,
            )
            degraded = degraded or bool(graph.degraded)
        except Exception:  # noqa: BLE001 - 规划失败降级
            tasks, degraded, errors = validate_tasks(
                [],
                request=request,
                max_tasks=settings.orchestrator_max_tasks,
                allow_write=write_mode,
            )

        if degraded and write_mode:
            fallback = _default_write_task(request, summary)
            if fallback is not None:
                tasks, fallback_degraded, fallback_errors = validate_tasks(
                    [fallback],
                    request=request,
                    max_tasks=settings.orchestrator_max_tasks,
                    allow_write=True,
                )
                if not fallback_degraded:
                    degraded = True
                else:
                    errors = errors + fallback_errors
            else:
                # 空工作区/只有隐藏文件：明确说出来，而不是悄悄退化为只读。
                errors.append(
                    "写编排降级：工作区没有可用的顶层目录或文件作为 write_scope"
                )

        # 只读模式下，模型只给一个任务时补一个独立的「测试/期望」探索任务：
        # 编排路径要能真正演示并发，而不是一个 worker 的串行包装。
        # 写模式不补：第二个 implement 没有明确 write_scope 时不安全。
        if not degraded and not write_mode and len(tasks) == 1:
            used = {task["id"] for task in tasks}
            extra_id = "explore-tests"
            while extra_id in used:
                extra_id += "-2"
            tasks.append(
                {
                    "id": extra_id,
                    "role": "explore",
                    "goal": "定位与用户请求相关的测试文件，并总结现有断言/期望行为",
                    "inputs": [],
                    "acceptance": ["列出相关测试文件与关键断言"],
                    "depends_on": [],
                }
            )

        revised_reason = ""
        if replan_mode:
            # 新任务 id 统一加前缀，避免与旧 worker_results 里的 attempt
            # 撞号（fan-in 按 task_id 取最后一次尝试）。
            prefix = f"r{int(state.get('steering_replans', 1))}-"
            mapping = {
                str(task["id"]): f"{prefix}{task['id']}" for task in tasks
            }
            for task in tasks:
                old_id = str(task["id"])
                task["id"] = mapping[old_id]
                task["depends_on"] = [
                    mapping.get(str(dep), str(dep))
                    for dep in task.get("depends_on") or []
                ]
            cap = max(settings.orchestrator_max_tasks - len(completed_tasks), 1)
            tasks = completed_tasks + tasks[:cap]
            revised_reason = (
                f"用户要求重规划：保留 {len(completed_tasks)} 个已完成任务，"
                f"新增 {len(tasks) - len(completed_tasks)} 个"
            )

        keep_ids = {task["id"] for task in completed_tasks}
        status = {
            task["id"]: (
                existing_status.get(task["id"], "pending")
                if task["id"] in keep_ids
                else "pending"
            )
            for task in tasks
        }
        attempts = dict(state.get("task_attempts") or {})
        # errors 只用于日志/事件；不把它塞进主消息历史。
        return {
            "tasks": tasks,
            "task_status": status,
            "task_attempts": attempts,
            "orchestrator_degraded": degraded,
            "orchestrator_round": 0,
            "orchestrator_summary": "；".join(errors),
            "steering_replan": False,
            "workflow_revised_reason": revised_reason,
        }

    return plan_tasks


def _normalize_patch_path(raw: str) -> str:
    """补丁路径归一化；越界或 agent 内部路径返回空串。"""
    text = str(raw).strip().replace("\\", "/")
    normalized = posixpath.normpath(text)
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.startswith("../")
        or normalized.startswith("/")
        or normalized == ".agent"
        or normalized.startswith(".agent/")
        or "__pycache__" in normalized.split("/")
    ):
        return ""
    return normalized


def _patch_paths(
    sandbox: WslSandbox, patch_path: str, root: str
) -> tuple[list[str], str]:
    """补丁会碰到的路径（不应用补丁）。"""
    result = sandbox.run(
        f"git apply --numstat -z {shlex.quote(patch_path)}", cwd=root
    )
    if not result.ok:
        return [], result.render(400)

    paths: list[str] = []
    fields = result.stdout.split("\0")
    index = 0
    while index < len(fields):
        field = fields[index]
        if not field:
            index += 1
            continue
        parts = field.split("\t")
        if len(parts) >= 3:
            candidates = [parts[2]] if parts[2] else fields[index + 1 : index + 3]
            index += 1 if parts[2] else 3
        else:
            # 纯重命名可能是 0\t0\tnew\0；旧路径由 worker 的 files 补齐。
            candidates = [field]
            index += 1
        for candidate in candidates:
            normalized = _normalize_patch_path(str(candidate))
            if not normalized:
                return [], f"补丁包含不允许合并的路径：{candidate}"
            paths.append(normalized)
    return sorted(set(paths)), ""


def _snapshot_paths(
    sandbox: WslSandbox, root: str, paths: list[str]
) -> tuple[list[str], str]:
    """合并前给主工作区里的目标文件留底。"""
    fs = SandboxFs(sandbox, root)
    store = SnapshotStore(sandbox, fs, root)
    snapshot_ids: list[str] = []
    for relpath in paths:
        target = posixpath.join(root, relpath)
        try:
            resolved, info = fs.probe(target)
            if info.exists and not info.is_file:
                return snapshot_ids, f"补丁目标不是普通文件：{relpath}"
            existed = bool(info.exists and info.is_file)
            original = (
                fs.read_text(resolved, max_bytes=MAX_OPERATIONAL_BYTES)
                if existed
                else ""
            )
            snapshot_ids.append(store.save(resolved, original, existed=existed))
        except (SandboxFsError, SandboxPathError) as exc:
            return snapshot_ids, (
                f"无法为 {relpath} 留底，补丁未合并："
                f"{type(exc).__name__}: {exc}"
            )
    return snapshot_ids, ""


def make_dispatch_node(
    *,
    worker_runner: WorkerRunner,
    settings: Settings,
    sandbox: WslSandbox | None = None,
    root: str = "",
    steering: SteeringController | None = None,
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    async def dispatch(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        thread_id = str(
            (((config or {}).get("configurable")) or {}).get("thread_id", "")
        )

        def cancelled() -> bool:
            return bool(steering is not None and steering.cancel_requested(thread_id))

        tasks = [dict(task) for task in (state.get("tasks") or [])]
        status = dict(state.get("task_status") or {})
        attempts = dict(state.get("task_attempts") or {})
        results = list(state.get("worker_results") or [])
        round_no = int(state.get("orchestrator_round", 0))

        # 取消可能在上一个节点之后才到：不再启动任何 worker。
        if cancelled():
            for task in tasks:
                if status.get(task["id"], "pending") == "pending":
                    status[task["id"]] = "cancelled"
            return {
                "task_status": status,
                "task_attempts": attempts,
                "orchestrator_round": round_no,
            }

        # 上一轮失败、但还有重试额度的任务放回 pending，再算 blocked。
        for task in tasks:
            task_id = task["id"]
            if (
                status.get(task_id) == "failed"
                and attempts.get(task_id, 0)
                <= settings.orchestrator_task_retries
            ):
                status[task_id] = "pending"
        status = mark_blocked(tasks, status)

        if round_no >= settings.orchestrator_max_rounds:
            for task in tasks:
                if status.get(task["id"], "pending") == "pending":
                    status[task["id"]] = "blocked"
            return {
                "task_status": status,
                "task_attempts": attempts,
                "orchestrator_round": round_no,
            }

        ready = ready_tasks(tasks, status)
        if not ready:
            return {
                "task_status": status,
                "task_attempts": attempts,
                "orchestrator_round": round_no,
            }

        parent_request = last_human_text(state["messages"])
        cwd = str(state.get("cwd", ""))
        semaphore = asyncio.Semaphore(
            max(1, int(settings.orchestrator_max_concurrent_workers))
        )

        # LangGraph custom stream：worker 一启动就发事件，而不是等整批返回后
        # 由 runtime 回放。拿不到 writer（例如单测直接调用节点）时静默降级，
        # runtime 的 updates 分支仍会补齐事件。
        try:
            writer = get_stream_writer()
        except Exception:  # noqa: BLE001 - 不在图执行上下文里
            writer = None

        def emit(payload: dict[str, Any]) -> None:
            if writer is None:
                return
            try:
                writer(payload)
            except Exception:  # noqa: BLE001 - 前端断开不应拖垮 dispatch
                pass

        async def run_one(task: dict[str, Any]) -> WorkerResult:
            task_id = str(task.get("id", ""))
            role = str(task.get("role", "explore"))
            worker_id = f"{role}:{task_id}"
            # 并发任务 id 唯一，这里读到的 attempts 与 gather 后的计算一致。
            attempt = attempts.get(task_id, 0) + 1
            async with semaphore:
                # 拿到槽位才发 started：否则“排队等 Semaphore”的 worker 会被
                # 前端误算成正在并发执行。
                emit(
                    {
                        "type": "worker_started",
                        "worker_id": worker_id,
                        "task_id": task_id,
                        "role": role,
                        "goal": str(task.get("goal", "")),
                        "attempt": attempt,
                    }
                )
                try:
                    result = await worker_runner(
                        task,
                        parent_request=parent_request,
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
                    )
                result.attempt = attempt
                # implement 的 merged 要等主工作区合并后才知道；它的完成事件
                # 在合并阶段之后发。只读 worker 的结果此时已经最终。
                if result.role != "implement":
                    emit({"type": "worker_finished", "result": result.model_dump()})
                return result

        outcomes = await asyncio.gather(
            *(run_one(task) for task in ready), return_exceptions=True
        )
        fresh_records: list[dict[str, Any]] = []
        for task, outcome in zip(ready, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                task_id = str(task.get("id", ""))
                role = str(task.get("role", "explore"))
                result = WorkerResult(
                    task_id=task_id,
                    role=role,
                    worker_id=f"{role}:{task_id}",
                    goal=str(task.get("goal", "")),
                    status="failed",
                    summary=f"worker 异常：{type(outcome).__name__}: {outcome}",
                )
                result.attempt = attempts.get(task_id, 0) + 1
                if role != "implement":
                    emit({"type": "worker_finished", "result": result.model_dump()})
            else:
                result = outcome

            task_id = result.task_id
            attempt = attempts.get(task_id, 0) + 1
            attempts[task_id] = attempt
            result.attempt = attempt
            record = result.model_dump()
            results.append(record)
            fresh_records.append(record)

            # 失败且还有重试额度：保持 pending，下一轮 dispatch 再跑一次。
            if (
                result.status == "failed"
                and attempt <= settings.orchestrator_task_retries
            ):
                status[task_id] = "pending"
            else:
                status[task_id] = result.status

        # 用户已取消：这一批里还没合并的 patch 一律丢弃。
        # worker 自己通常已经返回 cancelled；这里兜住“刚好在取消前跑完”的竞态。
        if cancelled():
            for record in fresh_records:
                if record.get("role") != "implement":
                    continue
                if record.get("status") in {"success", "partial"}:
                    record["status"] = "cancelled"
                    record["merged"] = False
                    record["merge_error"] = "用户取消，未合并"
                    status[str(record.get("task_id"))] = "cancelled"

        # implement worker 的补丁在主工作区顺序合并。并发只发生在 worktree 里；
        # 合并阶段串行，避免两个 git apply 互相踩。
        merged_any = False
        if sandbox is not None and root and not cancelled():
            for record in fresh_records:
                if record.get("role") != "implement":
                    continue
                patch_path = str(record.get("patch_path") or "")
                if not patch_path or record.get("status") in {"failed", "cancelled"}:
                    continue
                check = await asyncio.to_thread(
                    sandbox.run,
                    f"git apply --check {shlex.quote(patch_path)}",
                    cwd=root,
                )
                if not check.ok:
                    record["merge_error"] = check.render(400)
                    status[str(record.get("task_id"))] = "failed"
                    continue

                # 合并前留底：worker 的 file 工具快照在 worktree 里，worktree
                # 完成即删；主工作区必须自己再留一份，`agent undo` 才能回滚。
                patch_paths, path_error = await asyncio.to_thread(
                    _patch_paths, sandbox, patch_path, root
                )
                if path_error:
                    record["merge_error"] = path_error
                    status[str(record.get("task_id"))] = "failed"
                    continue
                snapshot_paths = sorted(
                    {
                        normalized
                        for normalized in (
                            _normalize_patch_path(str(item))
                            for item in [
                                *(record.get("files") or []),
                                *patch_paths,
                            ]
                        )
                        if normalized
                    }
                )
                snapshot_ids, snapshot_error = await asyncio.to_thread(
                    _snapshot_paths, sandbox, root, snapshot_paths
                )
                if snapshot_error:
                    record["merge_error"] = snapshot_error
                    status[str(record.get("task_id"))] = "failed"
                    continue

                applied = await asyncio.to_thread(
                    sandbox.run,
                    f"git apply {shlex.quote(patch_path)}",
                    cwd=root,
                )
                if applied.ok:
                    record["merged"] = True
                    record["snapshot_ids"] = snapshot_ids
                    merged_any = True
                else:
                    record["merge_error"] = applied.render(400)
                    status[str(record.get("task_id"))] = "failed"

        # implement 的结果到这里才最终（merged / merge_error 已确定）。
        for record in fresh_records:
            if record.get("role") == "implement":
                emit({"type": "worker_finished", "result": record})

        update = {
            "worker_results": results,
            "task_status": status,
            "task_attempts": attempts,
            "orchestrator_round": round_no + 1,
        }
        if merged_any:
            update.update({"dirty": True, "run_dirty": True})
        return update

    return dispatch


def make_extend_tasks_node(
    settings: Settings,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """消费 worker 提议的只读任务（Phase 4D）。

    这是「动态任务图」的扩展点：worker 在探索/审查时可能发现需要补一轮调查，
    但提议必须经过 host 校验（只读角色、去重、依赖存在、总量上限、无环）。
    """

    def extend_tasks(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        tasks = [dict(task) for task in (state.get("tasks") or [])]
        results = list(state.get("worker_results") or [])
        seen = int(state.get("task_extensions_seen", 0))
        fresh = results[seen:]

        proposals: list[dict[str, Any]] = []
        for result in fresh:
            # 失败/取消的 worker 不作为任务来源；partial 仍可能带有效发现。
            if result.get("status") not in {"success", "partial"}:
                continue
            proposals.extend((result.get("proposed_tasks") or [])[:2])

        new_tasks, errors = validate_proposals(
            proposals,
            existing=tasks,
            max_total=settings.orchestrator_max_tasks,
        )
        status = dict(state.get("task_status") or {})
        for task in new_tasks:
            status[task["id"]] = "pending"

        return {
            "tasks": tasks + new_tasks,
            "task_status": status,
            "task_extensions_seen": len(results),
            "task_extensions_applied": new_tasks,
            "task_extension_errors": errors,
        }

    return extend_tasks


def make_synthesize_node(
    settings: Settings,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    def synthesize(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        tasks = state.get("tasks") or []
        # 最后一遍把依赖失败的任务标 blocked：route 只看 ready，不会替我们改状态。
        status = mark_blocked(tasks, state.get("task_status") or {})
        results = state.get("worker_results") or []

        # 同一任务可能重试多次：fan-in 只看最后一次尝试。
        latest: dict[str, dict[str, Any]] = {}
        for result in results:
            task_id = str(result.get("task_id", ""))
            previous = latest.get(task_id)
            if previous is None or int(result.get("attempt", 1)) >= int(
                previous.get("attempt", 1)
            ):
                latest[task_id] = result

        merged_count = sum(
            1
            for result in latest.values()
            if result.get("role") == "implement" and result.get("merged")
        )
        lines = [
            "[编排器汇总]"
            + (
                "\n注意：以下实现任务已由 worker 完成并合并到主工作区；"
                "优先验证与最小修复，不要重做已完成部分。"
                if merged_count
                else ""
            )
        ]
        file_tasks: dict[str, list[str]] = {}
        for result in latest.values():
            task_id = result.get("task_id", "?")
            role = result.get("role", "")
            state_text = result.get("status", "")
            attempt = int(result.get("attempt", 1))
            retry_note = f"（第 {attempt} 次尝试）" if attempt > 1 else ""
            summary = str(result.get("summary", "")).strip()
            merge_note = ""
            if role == "implement":
                if result.get("merged"):
                    merge_note = "，已合并"
                elif result.get("merge_error"):
                    merge_note = f"，合并失败：{result['merge_error'][:200]}"
                elif result.get("patch_path"):
                    merge_note = "，补丁未合并"
            lines.append(
                f"- {task_id} ({role}/{state_text}){retry_note}"
                f"{merge_note}: {summary}"
            )
            files = result.get("files") or []
            if files:
                lines.append("  相关文件：" + "、".join(str(f) for f in files[:8]))
                for path in files:
                    file_tasks.setdefault(str(path), []).append(str(task_id))
            findings = result.get("findings") or []
            if findings:
                lines.append("  发现：" + "；".join(str(f) for f in findings[:5]))

        overlaps = {
            path: task_ids
            for path, task_ids in file_tasks.items()
            if len(set(task_ids)) > 1
        }
        if overlaps:
            lines.append("多 worker 关注同一文件：")
            for path, task_ids in list(overlaps.items())[:10]:
                lines.append(f"- {path}（{', '.join(sorted(set(task_ids)))}）")

        unfinished = [
            task["id"]
            for task in tasks
            if status.get(task["id"], "pending")
            in {"pending", "blocked", "cancelled"}
        ]
        if unfinished:
            lines.append("未执行/阻塞的任务：" + "、".join(unfinished))

        context = "\n".join(lines)
        limit = max(int(settings.orchestrator_context_chars), 500)
        if len(context) > limit:
            context = context[:limit] + "\n…（汇总已截断）"

        completed = sum(1 for value in status.values() if value == "success")
        partial = sum(1 for value in status.values() if value == "partial")
        cancelled_count = sum(
            1 for value in status.values() if value == "cancelled"
        )
        failed = sum(
            1 for value in status.values()
            if value in {"failed", "blocked", "cancelled"}
        )
        return {
            "task_status": status,
            "worker_context": context,
            "orchestrator_summary": (
                f"成功 {completed}，部分 {partial}，失败/阻塞 {failed}"
                f"（其中取消 {cancelled_count}），"
                f"已合并 {merged_count}，重叠文件 {len(overlaps)}"
            ),
            "orchestrator_completed": completed,
            "orchestrator_failed": failed,
        }

    return synthesize


def route_entry(state: AgentState) -> str:
    return "survey" if state.get("execution_mode") == "orchestrated" else "planner"


def route_after_select(state: AgentState) -> str:
    return (
        "plan_tasks"
        if state.get("workflow")
        in {"parallel_readonly", "orchestrated_implement"}
        else "planner"
    )


def make_route_after_dispatch(
    settings: Settings,
) -> Callable[[AgentState], str]:
    def route_after_dispatch(state: AgentState) -> str:
        if int(state.get("orchestrator_round", 0)) >= settings.orchestrator_max_rounds:
            return "synthesize"
        if ready_tasks(
            state.get("tasks") or [], state.get("task_status") or {}
        ):
            return "dispatch"
        return "synthesize"

    return route_after_dispatch
