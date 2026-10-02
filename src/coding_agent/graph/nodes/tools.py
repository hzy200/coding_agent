"""tools 节点：执行模型请求的工具调用，把结果包成 ToolMessage 回灌。

单个工具抛异常不会中断整张图 —— 错误信息会作为工具结果返回给模型，
让模型有机会自行纠正（这是失败驱动修复循环的基础）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from coding_agent.graph.state import AgentState
from coding_agent.tools.artifacts import unpack

# 这些工具接受 cwd 参数；模型没显式给出时，用图状态里的当前工作目录补上，
# 保证工具跟着会话走，而不是固定在构造时的默认值。
CWD_INJECTED_TOOLS = frozenset({"shell_exec"})


def make_tools_node(
    tools: list[BaseTool],
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    tool_map = {tool.name: tool for tool in tools}

    def run_tools(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []

        results: list[ToolMessage] = []
        for call in calls:
            name = call.get("name", "")
            args = dict(call.get("args", {}) or {})

            if name in CWD_INJECTED_TOOLS and args.get("cwd") is None and state.get("cwd"):
                args["cwd"] = state["cwd"]

            tool = tool_map.get(name)
            artifact: dict[str, Any] | None = None
            if tool is None:
                content = f"错误：不存在名为 {name} 的工具。可用工具：{sorted(tool_map)}"
            else:
                try:
                    content, artifact = unpack(tool.invoke(args, config))
                except Exception as exc:  # noqa: BLE001 - 工具失败要回灌给模型而非中断
                    content = f"工具执行异常：{type(exc).__name__}: {exc}"

            results.append(
                ToolMessage(
                    content=content,
                    tool_call_id=call.get("id", ""),
                    name=name,
                    artifact=artifact,
                )
            )

        return {"messages": results}

    return run_tools
