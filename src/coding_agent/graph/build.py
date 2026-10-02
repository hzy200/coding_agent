"""组装 LangGraph。

拓扑（W2）：

    START → planner → act ⇄ tools ─┬─→ advance ─→ act     （还有子任务）
                                    └─→ respond → END      （计划走完）

act ⇄ tools 是执行内核；planner 负责开局分解，advance 负责步进，
respond 负责收尾。后续里程碑在 act 前插入 retrieve、在其后插入 verify/repair。
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from coding_agent.config import Settings, get_settings
from coding_agent.graph.nodes import (
    advance,
    make_act_node,
    make_planner_node,
    make_respond_node,
    make_tools_node,
)
from coding_agent.graph.routing import ADVANCE, RESPOND, TOOLS, route_after_act
from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import build_llm
from coding_agent.tools.registry import build_tools


def build_graph(settings: Settings | None = None, *, checkpointer=None, allow_write: bool = False):
    settings = settings or get_settings()
    tools = build_tools(settings, allow_write=allow_write)

    # 不绑定工具的实例留给 planner / respond，避免它们在规划或收尾阶段发起动作
    llm = build_llm(settings, streaming=True)
    llm_with_tools = llm.bind_tools(tools)

    graph = StateGraph(AgentState)
    graph.add_node(
        "planner",
        make_planner_node(llm, max_steps=settings.max_plan_steps, allow_write=allow_write),
    )
    graph.add_node(
        "act",
        make_act_node(
            llm_with_tools,
            max_tool_rounds=settings.max_tool_rounds,
            allow_write=allow_write,
        ),
    )
    graph.add_node("tools", make_tools_node(tools))
    graph.add_node("advance", advance)
    graph.add_node("respond", make_respond_node(llm))

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "act")
    graph.add_conditional_edges(
        "act",
        route_after_act,
        {TOOLS: "tools", ADVANCE: "advance", RESPOND: "respond"},
    )
    graph.add_edge("tools", "act")
    graph.add_edge("advance", "act")
    graph.add_edge("respond", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())
