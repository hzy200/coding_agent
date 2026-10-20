"""tools 节点：执行模型请求的工具调用，把结果包成 ToolMessage 回灌。

**这里是所有工具执行的唯一咽喉，也是安全策略的强制执行点。**

approval_gate 只负责"决定"（放行/询问/拒绝），真正拦住调用的是这里：
没有拿到 auto 或 approved 的调用一律不执行，只回一条说明给模型。
两者分开的好处是判定逻辑保持纯函数，而执行侧的检查无法被绕过。

单个工具抛异常不会中断整张图 —— 错误信息会作为工具结果返回给模型，
让模型有机会自行纠正（这是失败驱动修复循环的基础）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from coding_agent.agents.explore import DelegateRunner, render_subagent_result
from coding_agent.graph.nodes.approve import tool_level
from coding_agent.graph.plan import EXPLORE, current_step_kind
from coding_agent.graph.state import AgentState
from coding_agent.messages import last_human_text
from coding_agent.sandbox.policy import APPROVED, AUTO, DENIED, DENY, CommandLevel
from coding_agent.tools.artifacts import (
    CallArtifact,
    FileArtifact,
    ShellArtifact,
    pack,
    unpack,
)
from coding_agent.tools.delegate import DELEGATE_EXPLORE
from coding_agent.tools.deps import DEPS_INSTALL
from coding_agent.tools.files import (
    EDIT_TOOL_NAME,
    READ_TOOL_NAME,
    RESTORE_TOOL_NAME,
    WRITE_TOOL_NAME,
)
from coding_agent.tools.git import GIT_ADD, GIT_COMMIT
from coding_agent.tools.shell import SHELL_TOOL_NAME

# 这些工具接受 cwd 参数；模型没显式给出时，用图状态里的当前工作目录补上，
# 保证工具跟着会话走，而不是固定在构造时的默认值。
CWD_INJECTED_TOOLS = frozenset({SHELL_TOOL_NAME})

# 成功的这些调用会改变工作区，本步因此需要跑一次验证
MUTATING_TOOLS = frozenset(
    {
        WRITE_TOOL_NAME,
        EDIT_TOOL_NAME,
        RESTORE_TOOL_NAME,
        GIT_ADD,
        GIT_COMMIT,
        DEPS_INSTALL,
    }
)

# 允许实际执行的审批结果
_EXECUTABLE = frozenset({AUTO, APPROVED})

# 文件工具的产出是 FileArtifact；其余工具（git_commit / deps_install / run_tests…）
# 的产出是 CallArtifact。事件层据此选对标签，别把前者安到后者头上。
_FILE_TOOLS = frozenset({READ_TOOL_NAME, WRITE_TOOL_NAME, EDIT_TOOL_NAME, RESTORE_TOOL_NAME})

_DENIED_TEXT = (
    "用户拒绝了这条命令，未执行任何操作。\n"
    "请换一种风险更低的方式达成同样的目的，或者直接向用户说明你需要什么授权。"
)

# 经 shell 的改动不会生成快照/FileChanged —— 提示模型改用受控的文件工具
_SHELL_MUTATION_NOTE = (
    "\n提示：这条命令改动了工作区，但**没有快照留底**，无法用 file_restore 回滚。"
    "后续改动请改用 file_write / file_edit，才能被记录并可回滚。"
)


def _mutates_workspace(name: str, args: dict[str, Any]) -> bool:
    """这次调用是否改动了工作区 —— 决定本步要不要跑验证。

    只认工具名会漏掉经 shell 的修改（`sed -i` / `git add` / `mkdir`…）：
    那些调用同样改了文件，却不会置 `dirty`，verify 就被跳过了。
    """
    if name in MUTATING_TOOLS:
        return True
    if name == SHELL_TOOL_NAME:
        return tool_level(name, args) >= CommandLevel.LOW_WRITE
    return False
_POLICY_DENIED_TEXT = (
    "该命令被会话策略拒绝，未执行任何操作。\n"
    "当前会话没有开放这个风险等级，请改用更低风险的方式。"
)
_NOT_APPROVED_TEXT = (
    "该调用没有获得审批记录，出于安全考虑未执行。\n"
    "这通常是编排层的问题，请换一种做法或告知用户。"
)


def _denied_artifact(name: str, args: dict[str, Any], decision: str) -> dict[str, Any]:
    """拒绝也要产出结构化产物，否则事件层与审计层会缺一条记录。

    按工具种类分派：shell → ShellArtifact，文件工具 → FileArtifact，
    其余（git_commit / deps_install / run_tests…）→ CallArtifact。
    全都塞进 FileArtifact 会让被拒的 git commit 在事件里被标成「文件工具」。
    """
    level = tool_level(name, args)
    if name == SHELL_TOOL_NAME:
        return ShellArtifact(
            command=str(args.get("command", "")),
            ok=False,
            rejected=True,
            level=int(level),
            level_label=level.label,
            decision=decision,
        ).model_dump()
    if name in _FILE_TOOLS:
        return FileArtifact(
            path=str(args.get("path", "")),
            action="deny",
            ok=False,
            rejected=True,
            decision=decision,
        ).model_dump()
    return CallArtifact(
        tool=name,
        ok=False,
        rejected=True,
        level=int(level),
        level_label=level.label,
        decision=decision,
    ).model_dump()


def make_tools_node(
    tools: list[BaseTool],
    *,
    delegate_runner: DelegateRunner | None = None,
    max_delegate_calls: int = 0,
    max_concurrent_agents: int = 1,
    max_concurrent_tools: int = 4,
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    tool_map = {tool.name: tool for tool in tools}

    async def run_tools(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []
        approvals = state.get("approvals") or {}
        # 本步是否改过东西 —— 决定 verify 要不要跑
        dirty = bool(state.get("dirty"))
        # 整个任务是否改过东西 —— 只在收尾时用来阻止「零改动却声称完成」。
        # 必须与 dirty 分开：dirty 每步被 advance/planner 重置，存不住。
        run_dirty = bool(state.get("run_dirty"))
        # 本次运行已改过的文件：一旦写入成功，历史里更早的 file_read 就过期了
        stale_paths = list(state.get("stale_paths") or [])
        # 任务级委派预算：planner 归零，这里递增
        delegate_calls = int(state.get("delegate_calls", 0))
        # 成本策略：按仓库规模给出的预算，以及「只有探索步能委派」的约束。
        delegation_allowed = bool(state.get("delegation_allowed", True))
        delegation_budget = int(state.get("delegation_budget", max_delegate_calls))
        effective_max = min(int(max_delegate_calls), delegation_budget)
        on_explore_step = current_step_kind(state) == EXPLORE

        # ---- 同一条消息里的多个 delegate 并发执行（fan-out）----
        # 先预扫描并占预算，再一次性 gather。预扫描保证：
        #   - 被拒/未批准/当前不允许委派的调用不占预算；
        #   - 超出有效预算的调用按原始顺序被拒绝；
        #   - 结果按 call_id 回填，与 ToolMessage 的配对语义一致。
        parent_request = last_human_text(state["messages"])
        cwd = str(state.get("cwd", ""))
        remaining = max(effective_max - delegate_calls, 0)
        jobs: list[tuple[str, dict[str, Any], str]] = []
        for call in calls:
            if str(call.get("name", "")) != DELEGATE_EXPLORE:
                continue
            call_id = str(call.get("id", ""))
            decision = approvals.get(call_id)
            if (
                decision not in _EXECUTABLE
                or delegate_runner is None
                or not delegation_allowed
                or not on_explore_step
                or remaining <= 0
            ):
                continue
            jobs.append((call_id, dict(call.get("args") or {}), decision))
            remaining -= 1

        delegate_messages: dict[str, ToolMessage] = {}
        if jobs:
            runner = delegate_runner
            assert runner is not None
            semaphore = asyncio.Semaphore(max(1, int(max_concurrent_agents)))

            async def _explore(
                call_id: str, args: dict[str, Any], decision: str
            ) -> ToolMessage:
                async with semaphore:
                    try:
                        sub = await runner(
                            args={
                                **args,
                                # 子智能体也遵守同一批项目约定与指令
                                "memories": state.get("memories") or [],
                                "instructions": state.get(
                                    "project_instructions"
                                )
                                or "",
                            },
                            call_id=call_id,
                            parent_request=parent_request,
                            cwd=cwd,
                            config=config,
                        )
                        sub.decision = decision
                        return ToolMessage(
                            content=pack(render_subagent_result(sub), sub),
                            tool_call_id=call_id,
                            name=DELEGATE_EXPLORE,
                            artifact=sub.model_dump(),
                        )
                    except Exception as exc:  # noqa: BLE001 - 单个子智能体失败不拖垮其它
                        return ToolMessage(
                            content=f"工具执行异常：{type(exc).__name__}: {exc}",
                            tool_call_id=call_id,
                            name=DELEGATE_EXPLORE,
                            artifact=None,
                        )

            outcomes = await asyncio.gather(
                *(_explore(*job) for job in jobs), return_exceptions=True
            )
            for (call_id, _args, _decision), outcome in zip(
                jobs, outcomes, strict=True
            ):
                if isinstance(outcome, BaseException):
                    delegate_messages[call_id] = ToolMessage(
                        content=(
                            f"工具执行异常：{type(outcome).__name__}: {outcome}"
                        ),
                        tool_call_id=call_id,
                        name=DELEGATE_EXPLORE,
                        artifact=None,
                    )
                else:
                    delegate_messages[call_id] = outcome
            delegate_calls += len(jobs)

        # ---- 同一条消息里的多个只读工具并发执行 ----
        # 写工具、未知工具与审批未通过的调用不进入这里，仍走下面的串行路径。
        parallel_jobs: list[tuple[str, str, dict[str, Any], str]] = []
        for call in calls:
            name = str(call.get("name", ""))
            call_id = str(call.get("id", ""))
            decision = approvals.get(call_id)
            if decision not in _EXECUTABLE or name == DELEGATE_EXPLORE:
                continue
            tool = tool_map.get(name)
            if tool is None:
                continue
            args = dict(call.get("args", {}) or {})
            if name in CWD_INJECTED_TOOLS and args.get("cwd") is None and state.get("cwd"):
                args["cwd"] = state["cwd"]
            if tool_level(name, args) != CommandLevel.READ:
                continue
            parallel_jobs.append((call_id, name, args, decision))

        parallel_messages: dict[str, ToolMessage] = {}
        if len(parallel_jobs) > 1:
            tool_semaphore = asyncio.Semaphore(
                max(1, int(max_concurrent_tools))
            )

            async def _read_only(
                call_id: str,
                name: str,
                args: dict[str, Any],
                decision: str,
            ) -> ToolMessage:
                async with tool_semaphore:
                    tool = tool_map[name]
                    try:
                        content, artifact = unpack(
                            await asyncio.to_thread(
                                tool.invoke, args, config
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        content = (
                            f"工具执行异常：{type(exc).__name__}: {exc}"
                        )
                        artifact = None
                    if isinstance(artifact, dict):
                        artifact["decision"] = decision
                        content = pack(content, artifact)
                    return ToolMessage(
                        content=content,
                        tool_call_id=call_id,
                        name=name,
                        artifact=artifact,
                    )

            outcomes = await asyncio.gather(
                *(_read_only(*job) for job in parallel_jobs),
                return_exceptions=True,
            )
            for (call_id, name, _args, _decision), outcome in zip(
                parallel_jobs, outcomes, strict=True
            ):
                if isinstance(outcome, BaseException):
                    parallel_messages[call_id] = ToolMessage(
                        content=(
                            f"工具执行异常：{type(outcome).__name__}: {outcome}"
                        ),
                        tool_call_id=call_id,
                        name=name,
                        artifact=None,
                    )
                else:
                    parallel_messages[call_id] = outcome

        results: list[ToolMessage] = []
        for call in calls:
            name = str(call.get("name", ""))
            call_id = str(call.get("id", ""))
            args = dict(call.get("args", {}) or {})

            decision = approvals.get(call_id)
            if decision not in _EXECUTABLE:
                if decision == DENIED:
                    content, recorded = _DENIED_TEXT, DENIED
                elif decision == DENY:
                    content, recorded = _POLICY_DENIED_TEXT, DENY
                else:
                    content, recorded = _NOT_APPROVED_TEXT, "missing"
                artifact = _denied_artifact(name, args, recorded)
                results.append(
                    ToolMessage(
                        content=pack(content, artifact),
                        tool_call_id=call_id,
                        name=name,
                        artifact=artifact,
                    )
                )
                continue

            if name in CWD_INJECTED_TOOLS and args.get("cwd") is None and state.get("cwd"):
                args["cwd"] = state["cwd"]

            precomputed = parallel_messages.get(call_id)
            if precomputed is not None:
                results.append(precomputed)
                continue

            # 子智能体：由宿主执行，不走普通工具的 invoke 路径。
            # 它同样是 tools 节点的子分支，审批与预算检查已经在上面完成。
            # 已调度的调用直接取并发批次的结果；剩下的是未启用/超预算/内部异常。
            if name == DELEGATE_EXPLORE:
                precomputed = delegate_messages.get(call_id)
                if precomputed is not None:
                    results.append(precomputed)
                    continue
                if delegate_runner is None:
                    content = "探索子智能体未启用（AGENT_EXPLORE_ENABLED=false）。"
                    results.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=call_id,
                            name=name,
                            artifact=None,
                        )
                    )
                    continue
                if not delegation_allowed:
                    content = (
                        "当前任务未启用探索委派（仓库规模较小或委派策略关闭）；"
                        "请直接使用 file_read / search_code / find_files。"
                    )
                    results.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=call_id,
                            name=name,
                            artifact=None,
                        )
                    )
                    continue
                if not on_explore_step:
                    content = (
                        "只有探索步（kind=explore）可以委派；"
                        "当前步骤请自己完成，不要调用 delegate_explore。"
                    )
                    results.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=call_id,
                            name=name,
                            artifact=None,
                        )
                    )
                    continue
                if delegate_calls >= effective_max:
                    content = (
                        f"本任务最多委派 {effective_max} 次探索，"
                        "已达到上限，请自行使用只读工具或结束这一步。"
                    )
                    artifact = _denied_artifact(name, args, "rejected")
                    results.append(
                        ToolMessage(
                            content=pack(content, artifact),
                            tool_call_id=call_id,
                            name=name,
                            artifact=artifact,
                        )
                    )
                    continue
                # 正常不可达：预扫描只在「可执行 + 有预算」时入队。
                results.append(
                    ToolMessage(
                        content="探索子智能体调用未被调度（内部状态不一致）。",
                        tool_call_id=call_id,
                        name=name,
                        artifact=None,
                    )
                )
                continue

            tool = tool_map.get(name)
            if tool is None:
                content = f"错误：不存在名为 {name} 的工具。可用工具：{sorted(tool_map)}"
                results.append(
                    ToolMessage(content=content, tool_call_id=call_id, name=name, artifact=None)
                )
                continue

            artifact = None
            try:
                # 现有工具/沙箱是同步的：放进线程池，避免阻塞事件循环。
                content, artifact = unpack(
                    await asyncio.to_thread(tool.invoke, args, config)
                )
            except Exception as exc:  # noqa: BLE001 - 工具失败要回灌给模型而非中断
                content = f"工具执行异常：{type(exc).__name__}: {exc}"

            # 执行过的调用补上审批结果，供审计区分 auto 与 approved
            if isinstance(artifact, dict):
                artifact["decision"] = decision
                if _mutates_workspace(name, args):
                    # "改动了工作区"与"这次调用成功了"是两件事，不能互相否决。
                    # shell 尤其如此：sed -i 改完了才失败、gcc 出了产物才报错，
                    # 非零退出不代表没动过文件 —— 那种情况下跳过验证最危险。
                    # 文件工具则相反：ok=False 就是没写成（路径越界、替换没命中…），
                    # 它的 ok 正好是"改动是否发生"，所以只有它才看 ok。
                    if name == SHELL_TOOL_NAME or artifact.get("ok"):
                        dirty = True
                        run_dirty = True
                        if name in _FILE_TOOLS:
                            changed = str(
                                artifact.get("path") or args.get("path") or ""
                            )
                            if changed and changed not in stale_paths:
                                stale_paths.append(changed)
                                del stale_paths[:-50]  # 只留最近 50 个
                    if name == SHELL_TOOL_NAME:
                        content += _SHELL_MUTATION_NOTE
                content = pack(content, artifact)

            results.append(
                ToolMessage(
                    content=content, tool_call_id=call_id, name=name, artifact=artifact
                )
            )

        # 审批结果是一次性的：用完即清，避免跨轮次误放行
        return {
            "messages": results,
            "approvals": {},
            "dirty": dirty,
            "run_dirty": run_dirty,
            "delegate_calls": delegate_calls,
            "stale_paths": stale_paths,
        }

    return run_tools
