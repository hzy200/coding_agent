"""组装 LangGraph。

拓扑（W10）：

    START → planner → act ─┬─→ approval_gate → tools ─→ act
                           └─→ verify ─┬─(通过)──→ advance ─→ act
                                       ├─(失败，有预算)→ repair ─→ act
                                       └─(失败，预算尽)→ respond → END

act ⇄ tools 是执行内核，approval_gate 做审批，verify 在一步做完后跑测试/构建，
失败则进 repair 重试 —— 这就是「失败驱动的修复循环」。
planner 开局分解，advance 步进，respond 收尾。
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from coding_agent.config import Settings, get_settings
from coding_agent.graph.nodes import (
    advance,
    make_act_node,
    make_approval_gate_node,
    make_planner_node,
    make_respond_node,
    make_tools_node,
    make_verify_node,
    repair,
)
from coding_agent.graph.routing import (
    ADVANCE,
    APPROVE,
    REPAIR,
    RESPOND,
    TOOLS,
    VERIFY,
    make_route_after_verify,
    route_after_act,
)
from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import build_llm
from coding_agent.sandbox.policy import SessionPolicy
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.tools.registry import build_tools

# planner + respond 各算 1 次，再留少量余量给中断/重入
_RECURSION_MARGIN = 10


def estimate_recursion_limit(max_plan_steps: int, max_tool_rounds: int) -> int:
    """按图拓扑估算 `recursion_limit`，避免把限额算小导致正常任务误报。

    每步最坏走过的超步：`act` 被调 (轮次+1) 次（末次超预算不产 tool_calls，直接去 verify）
    + `approval_gate` 与 `tools` 各 轮次 次 + `verify` / `advance` 各 1 次，
    即 `3×轮次 + 3`。再乘步数，加上 `planner`/`respond` 与少量余量。
    """
    per_step = (max_tool_rounds + 1) + 2 * max_tool_rounds + 2
    return max_plan_steps * per_step + _RECURSION_MARGIN


def build_graph(
    settings: Settings | None = None,
    *,
    checkpointer=None,
    allow_write: bool = False,
    policy: SessionPolicy | None = None,
):
    settings = settings or get_settings()
    policy = policy or SessionPolicy(allow_write=allow_write)
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
            max_repair_rounds=settings.max_repair_rounds,
            budget=settings.context_budget,
        ),
    )
    graph.add_node(APPROVE, make_approval_gate_node(policy))
    graph.add_node(TOOLS, make_tools_node(tools))
    graph.add_node(VERIFY, make_verify_node(WslSandbox(settings)))
    graph.add_node(REPAIR, repair)
    graph.add_node("advance", advance)
    graph.add_node(
        "respond",
        make_respond_node(
            llm,
            max_repair_rounds=settings.max_repair_rounds,
            budget=settings.context_budget,
        ),
    )

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "act")
    graph.add_conditional_edges(
        "act",
        route_after_act,
        {APPROVE: APPROVE, VERIFY: VERIFY},
    )
    graph.add_edge(APPROVE, TOOLS)
    graph.add_edge(TOOLS, "act")
    graph.add_conditional_edges(
        VERIFY,
        make_route_after_verify(settings.max_repair_rounds),
        {REPAIR: REPAIR, ADVANCE: "advance", RESPOND: "respond"},
    )
    graph.add_edge(REPAIR, "act")
    graph.add_edge("advance", "act")
    graph.add_edge("respond", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())
