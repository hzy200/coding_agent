"""Phase 3C：并发写 worker（git worktree 隔离）。

每个 implement 任务在**独立 worktree** 中运行，工具只作用于该 worktree；
完成后把变更导出成 patch，由 dispatch 顺序合并回主工作区。这样：

- 并发写不会互相踩文件；
- 主工作区在合并前保持不变；
- 合并失败时补丁不会被应用（先 `git apply --check`）。

前置条件是工作区为干净的 git 仓库；不满足时 runner 直接失败，调用方会
退回只读编排。
"""

from __future__ import annotations

import asyncio
import hashlib
import posixpath
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from coding_agent.config import Settings
from coding_agent.llm.prompts import (
    IMPLEMENT_WORKER_PROMPT,
    format_verification_feedback,
)
from coding_agent.messages import text_of
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.tools.artifacts import unpack
from coding_agent.tools.files import (
    EDIT_TOOL_NAME,
    READ_TOOL_NAME,
    WRITE_TOOL_NAME,
)
from coding_agent.tools.registry import build_tools
from coding_agent.tools.search import FIND_FILES, SEARCH_CODE
from coding_agent.tools.testrun import run_verification

IMPLEMENT_TOOL_NAMES = frozenset(
    {READ_TOOL_NAME, WRITE_TOOL_NAME, EDIT_TOOL_NAME, SEARCH_CODE, FIND_FILES}
)

ImplementRunner = Callable[..., Awaitable[WorkerResult]]


def _safe_id(task_id: str) -> str:
    """worktree / patch 文件名。

    截断后必须再拼一个短哈希：两个长 id 的前缀相同时，只截断会让它们共用
    同一个 worktree 与 patch 路径（并发时互相覆盖）。
    """
    text = re.sub(r"[^A-Za-z0-9_-]", "_", task_id)[:40]
    digest = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:8]
    return f"{text}-{digest}" if text else digest


def _usage(message: Any) -> tuple[int, int]:
    meta = getattr(message, "usage_metadata", None) or {}
    return (
        int(meta.get("input_tokens") or 0),
        int(meta.get("output_tokens") or 0),
    )


def _child_config(config: RunnableConfig | None, task_id: str) -> dict[str, Any]:
    base = dict(config or {})
    metadata = dict(base.get("metadata") or {})
    metadata.update({"agent_id": f"implement:{task_id}", "role": "implement"})
    return {
        **base,
        "run_name": f"implement:{task_id}",
        "tags": [*(base.get("tags") or []), "subagent", "implement"],
        "metadata": metadata,
    }


def _within_scope(path: str, scopes: list[str]) -> bool:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    return any(
        normalized == scope or normalized.startswith(scope.rstrip("/") + "/")
        for scope in scopes
    )


# 工作区不一定有 .gitignore（评测种子就没有）。worker 自己的快照、worktree、
# patch 与 Python 字节码必须显式排除，否则会被当成用户改动 / 收进补丁。
STAGE_EXCLUDES = (
    ":(exclude).agent",
    ":(exclude).agent/**",
    ":(exclude,glob)**/__pycache__/**",
)


def stage_args() -> str:
    """`git status` / `git add` / `git diff` 共用的排除 pathspec。"""
    return " ".join(shlex.quote(item) for item in STAGE_EXCLUDES)


def _git_ready(sandbox: WslSandbox, root: str) -> tuple[bool, str]:
    inside = sandbox.run("git rev-parse --is-inside-work-tree", cwd=root)
    if not (inside.ok and inside.stdout.strip().endswith("true")):
        return False, "工作区不是 git 仓库"
    # worktree 基于整个仓库的 HEAD，patch 路径也是仓库相对的。工作区若只是
    # 仓库的子目录，合并会落到错误的根上 —— 必须要求它就是仓库根。
    toplevel = sandbox.run("git rev-parse --show-toplevel", cwd=root)
    physical = sandbox.run("pwd -P", cwd=root)
    if not toplevel.ok or not physical.ok:
        return False, "无法确认 git 仓库根目录"
    if toplevel.stdout.strip().rstrip("/") != physical.stdout.strip().rstrip("/"):
        return False, "工作区不是 git 仓库根目录（写编排需要以仓库根作为工作区）"
    head = sandbox.run("git rev-parse HEAD", cwd=root)
    if not head.ok:
        return False, "git 仓库还没有 HEAD（需要至少一次提交）"
    # `.agent/` 里是 worktree、patch、快照等 agent 自己的产物；并发 worker
    # 互相看到对方刚创建的 worktree 是正常现象，不能据此判定"用户工作区脏"。
    status = sandbox.run(
        f"git status --porcelain -- . {stage_args()}", cwd=root
    )
    if not status.ok or status.stdout.strip():
        return False, "工作区有未提交改动，写 worker 已禁用"
    return True, ""


def _changed_files(sandbox: WslSandbox, worktree: str) -> list[str]:
    """本 worker 改动过的路径。

    用 `--name-status -M -z` 而不是 `--name-only`：重命名时后者只给新路径，
    合并前按这个列表留底会漏掉被删除的旧路径，回滚就不完整。
    """
    excludes = stage_args()
    add = sandbox.run(f"git add -A -- . {excludes}", cwd=worktree)
    if not add.ok:
        return []
    names = sandbox.run(
        f"git diff --cached --name-status -M -z HEAD -- . {excludes}",
        cwd=worktree,
    )
    if not names.ok:
        return []

    paths: list[str] = []
    fields = names.stdout.split("\0")
    index = 0
    while index < len(fields):
        status = fields[index]
        if not status:
            index += 1
            continue
        if status.startswith(("R", "C")) and index + 2 < len(fields):
            # R100\0old\0new —— 两条路径都算改动
            paths.extend([fields[index + 1], fields[index + 2]])
            index += 3
        elif index + 1 < len(fields):
            paths.append(fields[index + 1])
            index += 2
        else:
            index += 1
    return sorted({path.strip() for path in paths if path.strip()})


def _make_patch(
    sandbox: WslSandbox, worktree: str, root: str, task_id: str
) -> str:
    excludes = stage_args()
    diff = sandbox.run(
        f"git diff --cached --binary HEAD -- . {excludes}", cwd=worktree
    )
    if not diff.ok or not diff.stdout.strip():
        return ""
    patch_rel = posixpath.join(".agent", "patches", f"{_safe_id(task_id)}.patch")
    try:
        SandboxFs(sandbox, root).write_text(patch_rel, diff.stdout)
    except Exception:  # noqa: BLE001
        return ""
    return posixpath.join(root, patch_rel)


def make_implement_runner(
    *,
    llm: BaseChatModel,
    sandbox: WslSandbox,
    root: str,
    settings: Settings,
) -> ImplementRunner:
    max_rounds = max(int(settings.orchestrator_worker_rounds), 1)
    max_repairs = max(int(settings.orchestrator_worker_repairs), 0)
    timeout = max(int(settings.orchestrator_worker_timeout), 1)

    async def run(
        task: dict[str, Any],
        *,
        parent_request: str,
        cwd: str = "",
        config: RunnableConfig | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> WorkerResult:
        started = time.monotonic()
        task_id = str(task.get("id", ""))
        safe = _safe_id(task_id)
        goal = str(task.get("goal", "")).strip()
        scopes = [
            str(scope).strip().replace("\\", "/").rstrip("/")
            for scope in (task.get("write_scope") or [])
            if str(scope).strip()
        ]
        worker_id = f"implement:{task_id}"
        child_config = _child_config(config, task_id)

        async def shell(command: str, *, cwd: str | None = None):
            """把同步沙箱调用挪出事件循环，避免阻塞同批其它 worker。"""
            return await asyncio.to_thread(
                sandbox.run, command, cwd=cwd or root
            )

        def failed(summary: str, *, error: str = "") -> WorkerResult:
            return WorkerResult(
                task_id=task_id,
                role="implement",
                worker_id=worker_id,
                goal=goal,
                status="failed",
                summary=summary,
                error=error,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if not scopes:
            return failed("implement 任务没有 write_scope，拒绝执行")
        ready, reason = await asyncio.to_thread(_git_ready, sandbox, root)
        if not ready:
            return failed(reason)

        worktree = posixpath.join(root, ".agent", "worktrees", safe)
        worktree_parent = posixpath.join(root, ".agent", "worktrees")
        await shell(f"mkdir -p {shlex.quote(worktree_parent)}")
        await shell(f"git worktree remove --force {shlex.quote(worktree)}")
        await shell(f"rm -rf {shlex.quote(worktree)}")
        created = await shell(
            f"git worktree add --detach {shlex.quote(worktree)} HEAD"
        )
        if not created.ok:
            return failed(
                f"创建 worktree 失败：{created.render(400)}",
                error=created.stderr,
            )

        scoped = settings.model_copy(update={"wsl_workspace": worktree})
        tools = [
            tool
            for tool in build_tools(scoped, allow_write=True, sandbox=sandbox)
            if tool.name in IMPLEMENT_TOOL_NAMES
        ]
        tool_map: dict[str, BaseTool] = {tool.name: tool for tool in tools}
        child_llm = llm.bind_tools(tools)

        messages: list[Any] = [
            SystemMessage(
                content=IMPLEMENT_WORKER_PROMPT.format(
                    cwd=worktree,
                    write_scope="\n".join(f"- {scope}" for scope in scopes),
                    max_rounds=max_rounds,
                )
            ),
            HumanMessage(
                content=(
                    f"用户请求：{parent_request or '（未提供）'}\n\n"
                    f"实现任务：{goal}\n\n"
                    f"write_scope：{', '.join(scopes)}"
                )
            ),
        ]
        records: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        verification: dict[str, Any] = {}
        error = ""
        cancelled = False

        async def _loop() -> None:
            nonlocal input_tokens, output_tokens, verification, cancelled
            repairs = int(task.get("max_repairs") or max_repairs)
            for attempt in range(repairs + 1):
                for round_index in range(1, max_rounds + 1):
                    if cancel is not None and cancel():
                        cancelled = True
                        return
                    response = await child_llm.ainvoke(messages, child_config)
                    used_in, used_out = _usage(response)
                    input_tokens += used_in
                    output_tokens += used_out
                    messages.append(response)
                    calls = getattr(response, "tool_calls", None) or []
                    if not calls:
                        break
                    for index, call in enumerate(calls):
                        if cancel is not None and cancel():
                            cancelled = True
                            return
                        name = str(call.get("name", ""))
                        call_id = str(call.get("id", "")) or f"r{round_index}-{index}"
                        call_args = dict(call.get("args") or {})
                        tool = tool_map.get(name)
                        tool_started = time.monotonic()
                        if tool is None:
                            content = (
                                f"错误：实现 worker 没有 {name} 工具。"
                                f"可用工具：{sorted(tool_map)}"
                            )
                            ok = False
                        else:
                            try:
                                raw = await asyncio.to_thread(
                                    tool.invoke, call_args, child_config
                                )
                                content, artifact = unpack(raw)
                                ok = (
                                    bool(artifact.get("ok", True))
                                    if isinstance(artifact, dict)
                                    else True
                                )
                            except Exception as exc:  # noqa: BLE001
                                content = f"工具执行异常：{type(exc).__name__}: {exc}"
                                ok = False
                        records.append(
                            {
                                "call_id": call_id,
                                "name": name,
                                "args": call_args,
                                "ok": ok,
                                "duration_ms": int(
                                    (time.monotonic() - tool_started) * 1000
                                ),
                                "round": round_index,
                            }
                        )
                        messages.append(
                            ToolMessage(
                                content=content,
                                tool_call_id=call_id,
                                name=name,
                            )
                        )

                changed = await asyncio.to_thread(
                    _changed_files, sandbox, worktree
                )
                if not changed:
                    return
                result = await asyncio.to_thread(
                    run_verification, sandbox, worktree, allow_manifest=False
                )
                verification = result.model_dump()
                if result.status == "ok":
                    return
                if attempt < repairs:
                    messages.append(
                        HumanMessage(
                            content=format_verification_feedback(
                                verification,
                                attempt=attempt + 1,
                                limit=repairs + 1,
                            )
                        )
                    )
                    continue
                return

        try:
            await asyncio.wait_for(_loop(), timeout=timeout)
        except TimeoutError:
            error = f"实现 worker 超时（>{timeout}s）"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        # 取消可能在最后一条工具调用之后才到：此时补丁还没合并，
        # 直接丢弃 worktree 比先落 patch 再由 dispatch 拦更干净。
        if not cancelled and cancel is not None and cancel():
            cancelled = True

        if cancelled:
            await shell(f"git worktree remove --force {shlex.quote(worktree)}")
            await shell("git worktree prune")
            return WorkerResult(
                task_id=task_id,
                role="implement",
                worker_id=worker_id,
                goal=goal,
                status="cancelled",
                summary="已按用户要求取消，改动未合并。",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=int((time.monotonic() - started) * 1000),
                tool_calls=records,
            )

        changed = await asyncio.to_thread(_changed_files, sandbox, worktree)
        out_of_scope = [path for path in changed if not _within_scope(path, scopes)]
        patch_path = ""
        status = "failed"
        summary = text_of(messages[-1]).strip() if messages else ""

        if out_of_scope:
            status = "failed"
            error = error or f"修改了 write_scope 外的文件：{', '.join(out_of_scope)}"
        elif not changed:
            status = "failed"
            error = error or "实现 worker 没有产生任何文件改动"
        else:
            patch_path = await asyncio.to_thread(
                _make_patch, sandbox, worktree, root, task_id
            )
            if not patch_path:
                status = "failed"
                error = error or "无法生成补丁"
            elif verification.get("status") == "ok":
                status = "success"
            else:
                status = "partial"

        if not summary:
            summary = str(verification.get("summary") or "") or error or status
        findings = [
            f"{issue.get('location', '')} {issue.get('message', '')}".strip()
            for issue in (verification.get("issues") or [])
        ]
        if out_of_scope:
            findings.append("越界修改：" + ", ".join(out_of_scope))

        # 清理 worktree：补丁已落盘，不再需要它。
        await shell(f"git worktree remove --force {shlex.quote(worktree)}")
        await shell("git worktree prune")

        return WorkerResult(
            task_id=task_id,
            role="implement",
            worker_id=worker_id,
            goal=goal,
            status=status,
            summary=summary,
            files=changed,
            findings=findings,
            verification=verification,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
            patch_path=patch_path,
            tool_calls=records,
        )

    return run
