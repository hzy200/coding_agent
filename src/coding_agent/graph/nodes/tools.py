"""tools 节点：执行模型请求的工具调用，把结果包成 ToolMessage 回灌。

**这里是所有工具执行的唯一咽喉，也是安全策略的强制执行点。**

approval_gate 只负责"决定"（放行/询问/拒绝），真正拦住调用的是这里：
没有拿到 auto 或 approved 的调用一律不执行，只回一条说明给模型。
两者分开的好处是判定逻辑保持纯函数，而执行侧的检查无法被绕过。

单个工具抛异常不会中断整张图 —— 错误信息会作为工具结果返回给模型，
让模型有机会自行纠正（这是失败驱动修复循环的基础）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from coding_agent.graph.nodes.approve import tool_level
from coding_agent.graph.state import AgentState
from coding_agent.sandbox.policy import APPROVED, AUTO, DENIED, DENY
from coding_agent.tools.artifacts import FileArtifact, ShellArtifact, pack, unpack
from coding_agent.tools.deps import DEPS_INSTALL
from coding_agent.tools.files import EDIT_TOOL_NAME, RESTORE_TOOL_NAME, WRITE_TOOL_NAME
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

_DENIED_TEXT = (
    "用户拒绝了这条命令，未执行任何操作。\n"
    "请换一种风险更低的方式达成同样的目的，或者直接向用户说明你需要什么授权。"
)
_POLICY_DENIED_TEXT = (
    "该命令被会话策略拒绝，未执行任何操作。\n"
    "当前会话没有开放这个风险等级，请改用更低风险的方式。"
)
_NOT_APPROVED_TEXT = (
    "该调用没有获得审批记录，出于安全考虑未执行。\n"
    "这通常是编排层的问题，请换一种做法或告知用户。"
)


def _denied_artifact(name: str, args: dict[str, Any], decision: str, reason: str) -> dict[str, Any]:
    """拒绝也要产出结构化产物，否则事件层与审计层会缺一条记录。"""
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
    return FileArtifact(
        path=str(args.get("path", "")),
        action="deny",
        ok=False,
        rejected=True,
        decision=decision,
    ).model_dump()


def make_tools_node(
    tools: list[BaseTool],
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    tool_map = {tool.name: tool for tool in tools}

    def run_tools(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []
        approvals = state.get("approvals") or {}
        # 本步是否改过东西 —— 决定 verify 要不要跑
        dirty = bool(state.get("dirty"))

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
                artifact = _denied_artifact(name, args, recorded, "")
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

            tool = tool_map.get(name)
            if tool is None:
                content = f"错误：不存在名为 {name} 的工具。可用工具：{sorted(tool_map)}"
                results.append(
                    ToolMessage(content=content, tool_call_id=call_id, name=name, artifact=None)
                )
                continue

            artifact = None
            try:
                content, artifact = unpack(tool.invoke(args, config))
            except Exception as exc:  # noqa: BLE001 - 工具失败要回灌给模型而非中断
                content = f"工具执行异常：{type(exc).__name__}: {exc}"

            # 执行过的调用补上审批结果，供审计区分 auto 与 approved
            if isinstance(artifact, dict):
                artifact["decision"] = decision
                content = pack(content, artifact)
                if name in MUTATING_TOOLS and artifact.get("ok"):
                    dirty = True

            results.append(
                ToolMessage(
                    content=content, tool_call_id=call_id, name=name, artifact=artifact
                )
            )

        # 审批结果是一次性的：用完即清，避免跨轮次误放行
        return {"messages": results, "approvals": {}, "dirty": dirty}

    return run_tools
