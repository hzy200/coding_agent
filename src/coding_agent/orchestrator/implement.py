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

from coding_agent.codeintel.python_index import (
    build_index,
    collect_python_sources,
    related_test_files,
)
from coding_agent.config import Settings
from coding_agent.llm.prompts import (
    IMPLEMENT_WORKER_PROMPT,
    format_verification_feedback,
)
from coding_agent.messages import text_of
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.retry import format_retry_context
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
from coding_agent.tools.testrun import related_test_paths, run_verification

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
    """一次 wsl.exe 完成 worktree 前置检查。

    原先拆成 5 条命令，4 个 implement worker 并发时就是 20 次进程启动；
    检查项本身不变，只是合并成一次脚本。
    """
    script = f"""\
inside=$(git rev-parse --is-inside-work-tree 2>/dev/null)
if [ "$inside" != "true" ]; then echo "INSIDE=0"; exit 0; fi
echo "INSIDE=1"
top=$(git rev-parse --show-toplevel 2>/dev/null)
physical=$(pwd -P)
if [ "$top" = "$physical" ]; then echo "ROOT=1"; else echo "ROOT=0"; fi
if git rev-parse HEAD >/dev/null 2>&1; then echo "HEAD=1"; else echo "HEAD=0"; fi
dirty=$(git status --porcelain -- . {stage_args()} 2>/dev/null)
if [ -z "$dirty" ]; then echo "CLEAN=1"; else echo "CLEAN=0"; fi
"""
    result = sandbox.run(script, cwd=root)
    if not result.ok:
        return False, "无法确认 git 仓库状态"
    flags: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            flags[key] = value
    if flags.get("INSIDE") != "1":
        return False, "工作区不是 git 仓库"
    # worktree 基于整个仓库的 HEAD，patch 路径也是仓库相对的。工作区若只是
    # 仓库的子目录，合并会落到错误的根上 —— 必须要求它就是仓库根。
    if flags.get("ROOT") != "1":
        return False, "工作区不是 git 仓库根目录（写编排需要以仓库根作为工作区）"
    if flags.get("HEAD") != "1":
        return False, "git 仓库还没有 HEAD（需要至少一次提交）"
    # `.agent/` 里是 worktree、patch、快照等 agent 自己的产物；并发 worker
    # 互相看到对方刚创建的 worktree 是正常现象，不能据此判定"用户工作区脏"。
    if flags.get("CLEAN") != "1":
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


_IMPORT_ERROR_RE = re.compile(
    r"cannot import name '[^']+' from '([^']+)'"
)
_MODULE_ERROR_RE = re.compile(r"No module named '([^']+)'")


def _peer_dependency_failure(
    verification: dict[str, Any], changed: list[str], scopes: list[str]
) -> bool:
    """测试失败是否来自同批其它 worker 尚未合并的模块。

    多个 implement worker 并发时，共享测试可能 import 其它 worker 的文件。
    这不是本 worker 能修的：它没有对应 write_scope，重试只会白烧轮次。
    合并后的主 agent 会跑完整验证，所以这里直接停在 partial。
    """
    text = "\n".join(
        [
            str(verification.get("output_tail") or ""),
            *[
                str(issue.get("message") or "")
                for issue in (verification.get("issues") or [])
                if isinstance(issue, dict)
            ],
        ]
    )
    modules = _IMPORT_ERROR_RE.findall(text) + _MODULE_ERROR_RE.findall(text)
    for module in modules:
        path = module.replace(".", "/") + ".py"
        if path not in changed and not _within_scope(path, scopes):
            return True
    return False


def _impacted_tests(
    sandbox: WslSandbox, worktree: str, changed: list[str]
) -> list[str]:
    """优先用 AST import 图选测试；索引不可用时回退文件名启发式。"""
    sources = collect_python_sources(sandbox, worktree)
    if sources:
        related = related_test_files(build_index(sources), changed)
        if related:
            return related
    return related_test_paths(sandbox, worktree, changed)


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
    default_rounds = max(int(settings.orchestrator_worker_rounds), 1)
    max_repairs = max(int(settings.orchestrator_worker_repairs), 0)
    default_timeout = max(int(settings.orchestrator_worker_timeout), 1)

    async def run(
        task: dict[str, Any],
        *,
        parent_request: str,
        cwd: str = "",
        config: RunnableConfig | None = None,
        cancel: Callable[[], bool] | None = None,
        memories: list[str] | None = None,
        instructions: str = "",
    ) -> WorkerResult:
        started = time.monotonic()
        task_id = str(task.get("id", ""))
        safe = _safe_id(task_id)
        goal = str(task.get("goal", "")).strip()
        retry_note = format_retry_context(task)
        scopes = [
            str(scope).strip().replace("\\", "/").rstrip("/")
            for scope in (task.get("write_scope") or [])
            if str(scope).strip()
        ]
        worker_id = f"implement:{task_id}"
        child_config = _child_config(config, task_id)
        # 任务级预算优先于全局默认；None 继承全局。轮次/超时至少为 1，
        # token 预算 0 表示不限制。
        raw_rounds = task.get("max_rounds")
        raw_timeout = task.get("timeout")
        raw_tokens = task.get("max_tokens")
        max_rounds = max(
            int(raw_rounds if raw_rounds is not None else default_rounds), 1
        )
        timeout = max(
            int(raw_timeout if raw_timeout is not None else default_timeout), 1
        )
        max_tokens = max(
            int(
                raw_tokens
                if raw_tokens is not None
                else settings.orchestrator_task_tokens or 0
            ),
            0,
        )

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

        def remove_worktree() -> None:
            try:
                sandbox.run(
                    f"git worktree remove --force {shlex.quote(worktree)}",
                    cwd=root,
                )
                sandbox.run("git worktree prune", cwd=root)
            except Exception:  # noqa: BLE001 - 清理失败不覆盖原始结果
                pass

        # 清理守卫从**创建之前**就生效：取消可能正好落在下面某个 await
        # 上，此时 worktree 目录可能已经部分创建。
        try:
            await shell(f"mkdir -p {shlex.quote(worktree_parent)}")
            await shell(f"git worktree remove --force {shlex.quote(worktree)}")
            await shell(f"rm -rf {shlex.quote(worktree)}")
            created = await shell(
                f"git worktree add --detach {shlex.quote(worktree)} HEAD"
            )
        except asyncio.CancelledError:
            remove_worktree()
            raise
        if not created.ok:
            remove_worktree()
            return failed(
                f"创建 worktree 失败：{created.render(400)}",
                error=created.stderr,
            )

        async def cleanup_worktree() -> None:
            """移除本次 worker 的 worktree；取消路径也必须执行。

            `git worktree remove` / `prune` 是宿主清理动作，不经过 worker
            工具。取消发生在 `await` 点时，finally 仍要把它跑完，否则
            `.agent/worktrees/` 会残留。

            正常路径把清理丢到线程里；**已被取消时改走同步清理**：
            取消状态下再次 `await` 会被立刻打断，清理可能只做了一半。
            同步调用虽然短暂阻塞事件循环，但只发生在取消/超时路径。
            """
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                remove_worktree()
                return
            try:
                await asyncio.shield(asyncio.to_thread(remove_worktree))
            except asyncio.CancelledError:
                remove_worktree()
                raise

        scoped = settings.model_copy(update={"wsl_workspace": worktree})
        # 写时边界：文件工具在 I/O 之前就会拒绝 write_scope 外的路径。
        # 事后 `_within_scope` 的 diff 校验仍保留，作为第二道防线。
        tools = [
            tool
            for tool in build_tools(
                scoped,
                allow_write=True,
                sandbox=sandbox,
                write_scope=(
                    scopes if settings.orchestrator_write_scope_enforce else None
                ),
                # worktree 内独立缓存；写路径会失效，不能与主工作区共享。
                read_cache={},
            )
            if tool.name in IMPLEMENT_TOOL_NAMES
        ]
        # 静态白名单断言：implement worker 永远不应拿到 shell/git/deps/test。
        assert set(tool.name for tool in tools) <= IMPLEMENT_TOOL_NAMES
        tool_map: dict[str, BaseTool] = {tool.name: tool for tool in tools}
        child_llm = llm.bind_tools(tools)

        repo_map = str(task.get("repo_map") or "").strip()
        system_text = IMPLEMENT_WORKER_PROMPT.format(
            cwd=worktree,
            write_scope="\n".join(f"- {scope}" for scope in scopes),
            max_rounds=max_rounds,
        )
        facts = [str(fact).strip() for fact in (memories or []) if str(fact).strip()]
        if facts:
            system_text += (
                "\n\n关于这个项目的长期记忆（可信，必须遵守）：\n"
                + "\n".join(f"- {fact}" for fact in facts)
            )
        if instructions:
            system_text += f"\n\n{instructions}"
        messages: list[Any] = [
            SystemMessage(content=system_text),
            HumanMessage(
                content=(
                    f"用户请求：{parent_request or '（未提供）'}\n\n"
                    f"实现任务：{goal}\n\n"
                    f"write_scope：{', '.join(scopes)}"
                    f"{retry_note}"
                    + (
                        f"\n\n仓库结构摘要（AST 生成，供定位）：\n{repo_map}"
                        if repo_map
                        else ""
                    )
                )
            ),
        ]
        records: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        llm_ms = 0
        tool_ms = 0
        scope_violations: list[str] = []
        verification: dict[str, Any] = {}
        error = ""
        cancelled = False
        timed_out = False
        budget_exhausted = False

        async def _loop() -> None:
            nonlocal input_tokens, output_tokens, verification, cancelled
            nonlocal budget_exhausted, llm_ms, tool_ms
            raw_repairs = task.get("max_repairs")
            repairs = int(
                raw_repairs if raw_repairs is not None else max_repairs
            )
            for attempt in range(repairs + 1):
                for round_index in range(1, max_rounds + 1):
                    if cancel is not None and cancel():
                        cancelled = True
                        return
                    llm_started = time.monotonic()
                    response = await child_llm.ainvoke(messages, child_config)
                    llm_ms += int((time.monotonic() - llm_started) * 1000)
                    used_in, used_out = _usage(response)
                    input_tokens += used_in
                    output_tokens += used_out
                    messages.append(response)
                    if (
                        max_tokens > 0
                        and input_tokens + output_tokens >= max_tokens
                    ):
                        budget_exhausted = True
                        return
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
                        artifact: dict[str, Any] | None = None
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
                        duration_ms = int(
                            (time.monotonic() - tool_started) * 1000
                        )
                        tool_ms += duration_ms
                        if (
                            isinstance(artifact, dict)
                            and artifact.get("rejected")
                            and name in {WRITE_TOOL_NAME, EDIT_TOOL_NAME}
                        ):
                            bad_path = str(artifact.get("path") or "")
                            if bad_path:
                                scope_violations.append(bad_path)
                        records.append(
                            {
                                "call_id": call_id,
                                "name": name,
                                "args": call_args,
                                "ok": ok,
                                "duration_ms": duration_ms,
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
                related = await asyncio.to_thread(
                    _impacted_tests, sandbox, worktree, list(changed)
                )
                result = await asyncio.to_thread(
                    run_verification,
                    sandbox,
                    worktree,
                    allow_manifest=False,
                    only=tuple(related),
                    # 有相关测试时 worker 只跑子集；合并后的主 agent
                    # 仍会跑完整 verify，避免多 worker 各跑一遍全量。
                    full_after_selected=not related,
                )
                verification = result.model_dump()
                if result.status == "ok":
                    return
                if _peer_dependency_failure(
                    verification, list(changed), scopes
                ):
                    verification["peer_dependency"] = True
                    verification["note"] = (
                        "测试依赖同批其它 implement worker 的改动；"
                        "等待合并后由主 agent 完整验证"
                    )
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

        async def finish() -> WorkerResult:
            nonlocal error
            if cancelled:
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
                    llm_ms=llm_ms,
                    tool_ms=tool_ms,
                    timed_out=timed_out,
                    scope_violations=list(scope_violations),
                    tool_calls=records,
                    budget_used={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "tool_calls": len(records),
                    },
                )

            changed = await asyncio.to_thread(_changed_files, sandbox, worktree)
            out_of_scope = [
                path for path in changed if not _within_scope(path, scopes)
            ]
            patch_path = ""
            status = "failed"
            summary = text_of(messages[-1]).strip() if messages else ""

            if out_of_scope:
                status = "failed"
                error = (
                    error
                    or f"修改了 write_scope 外的文件：{', '.join(out_of_scope)}"
                )
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
                summary = (
                    str(verification.get("summary") or "") or error or status
                )
            findings = [
                f"{issue.get('location', '')} {issue.get('message', '')}".strip()
                for issue in (verification.get("issues") or [])
            ]
            if out_of_scope:
                findings.append("越界修改：" + ", ".join(out_of_scope))

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
                llm_ms=llm_ms,
                tool_ms=tool_ms,
                timed_out=timed_out,
                error=error,
                patch_path=patch_path,
                tool_calls=records,
                scope_violations=sorted(
                    {*scope_violations, *out_of_scope}
                ),
                budget_used={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "tool_calls": len(records),
                },
            )

        # 无论正常结束、超时、异常还是被 scheduler 取消，worktree 都必须清掉。
        try:
            try:
                await asyncio.wait_for(_loop(), timeout=timeout)
            except TimeoutError:
                timed_out = True
                error = f"实现 worker 超时（>{timeout}s）"
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
            if budget_exhausted and not error:
                error = f"任务 token 预算已用尽（>{max_tokens}）"

            # 取消可能在最后一条工具调用之后才到：此时补丁还没合并，
            # 直接丢弃 worktree 比先落 patch 再由 dispatch 拦更干净。
            if not cancelled and cancel is not None and cancel():
                cancelled = True
            return await finish()
        finally:
            await cleanup_worktree()

    return run
