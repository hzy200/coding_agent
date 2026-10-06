"""组装 LangGraph。

拓扑：

    START → planner → act ─┬─→ approval_gate → tools ─→ act
                           └─→ verify ─┬─(通过)─→ review ─┬─(通过)─→ advance → replan
                                       │                 │            ├─→ act
                                       │                 │            └─→ respond → END
                                       │                 └─(阻断)─┐
                                       └─(失败)────────────────────┴─┬─(有预算)→ repair → act
                                                                     └─(用尽)──→ replan

act ⇄ tools 是执行内核，approval_gate 做审批。**两道质量关**：verify 在一步做完后
跑测试/构建（行为对不对），review 在验证通过后做确定性代码审查（干不干净）——
测试全绿也可以是坏的代码：调试残留、被改弱的断言、语法坏掉的分支。
任一关没过都进 repair 重试 —— 这就是「失败驱动的修复循环」。

planner 开局分解，**replan 在每次步进后重新审视剩余步骤** —— 计划可以在执行中
被修正（补一步、砍掉已经没必要的一步），否则就是跟着过时计划走到黑。
「修不了就改计划」是自主性的另一半：repair 修的是**这一步**，replan 修的是**后面**。

advance 步进，respond 收尾。
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
    make_replan_node,
    make_respond_node,
    make_review_node,
    make_tools_node,
    make_verify_node,
    nudge,
    repair,
)
from coding_agent.graph.routing import (
    ACT,
    ADVANCE,
    APPROVE,
    NUDGE,
    REPAIR,
    REPLAN,
    RESPOND,
    REVIEW,
    TOOLS,
    VERIFY,
    make_route_after_review,
    make_route_after_verify,
    route_after_act,
    route_after_replan,
)
from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import build_llm
from coding_agent.sandbox.policy import SessionPolicy
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.registry import build_tools

# planner + respond 各算 1 次，再留少量余量给中断/重入
_RECURSION_MARGIN = 10


def estimate_recursion_limit(
    max_plan_steps: int,
    max_tool_rounds: int,
    max_repair_rounds: int,
    max_replans: int,
) -> int:
    """按图拓扑估算 `recursion_limit`，避免把限额算小导致正常任务误报。

    一个周期（act 跑满本轮工具预算、直到不再产 tool_calls）最坏走过的超步：
    `act` 被调 (轮次+1) 次（末次超预算不产 tool_calls，直接去 verify）
    + `approval_gate` 与 `tools` 各 轮次 次 + `verify` 1 次 + `review` 1 次，
    即 `3×轮次 + 3`。

    这个公式已经**算歪过三次**，每次都是漏算了某个会「重新开始」的机制：

    1. `repair` 把工具轮次清零（见 nodes/repair.py），所以每一轮修复都重新吃满
       一个完整周期 —— 一步最多 (修复次数+1) 个周期。
    2. `replan` 在修复用尽后还能给新做法**重置修复预算与轮次**，所以「周期 × 修复」
       这一整块会再重复 (重规划次数+1) 遍。
    3. `replan` 的**两个入口各有一个额度**（见 nodes/replan.py），所以一步做完后
       除了失败重规划，还可能再走一次「步进微调」，两者会叠加。

    漏算任何一项，走了那条路径的任务都会撞上 GraphRecursionError，
    而拿不到「试过但没修好」的收尾。

    **新增会让计数器归零的节点时，必须回来同步这个函数。**

    关于 `review`：它每周期恰 1 次，且与 verify **共用** `retry` 修复预算
    （review 阻断走的还是同一条 REPAIR 边），所以阶段结构不变，只是每周期多 1 个
    超步。若哪天给 review 单开一个修复计数器，这里要多乘一层 `(审查修复次数+1)`。
    """
    per_cycle = (max_tool_rounds + 1) + 2 * max_tool_rounds + 2
    phase = (max_repair_rounds + 1) * per_cycle + max_repair_rounds
    # 最坏路径是「一路失败」：每个阶段（修复用尽）之后都进一次 replan，
    # **包括最后一个**——它负责放弃并把剩余步骤砍掉。所以是 (重规划次数+1) 个
    # 「阶段 + replan」。
    per_step = (max_replans + 1) * (phase + 1)
    # 一步若**一个改动都没产生**，会经 nudge 重做一轮（上限一次，见 nodes/nudge.py）：
    # nudge 节点 1 个超步 + 一个完整的执行周期。
    per_step += per_cycle + 1
    # 失败重规划若让本步通过，仍会走 advance → replan（步进微调），
    # 那两个超步落在相邻步骤之间，最多每步各一次。
    return max_plan_steps * (per_step + 2) + _RECURSION_MARGIN


def build_graph(
    settings: Settings | None = None,
    *,
    checkpointer=None,
    allow_write: bool = False,
    policy: SessionPolicy | None = None,
    sandbox: WslSandbox | None = None,
):
    # 一次运行只该有一份沙箱：环境探测（$HOME、bwrap、rg）的结果缓存在**实例**上，
    # 各建一份就等于各探一遍 —— 曾经这里是 runtime / build_tools / verify 三份，
    # 冷启动光探测就要 4 次 wsl.exe 进程启动。
    settings = settings or get_settings()
    policy = policy or SessionPolicy(allow_write=allow_write)
    sandbox = sandbox or WslSandbox(settings)
    # 工作区只解析一次，再往下传：verify 的沙箱是共享的，它那份 settings 未必带
    # `--workspace` 覆盖，让它自己推会退回 $HOME。
    root = resolve_workspace(settings, sandbox)
    tools = build_tools(settings, allow_write=allow_write, sandbox=sandbox)

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
    graph.add_node(VERIFY, make_verify_node(sandbox, root))
    graph.add_node(REVIEW, make_review_node(sandbox, root))
    graph.add_node(NUDGE, nudge)
    graph.add_node(REPAIR, repair)
    graph.add_node("advance", advance)
    graph.add_node(
        REPLAN,
        make_replan_node(
            llm,
            max_replans=settings.max_replans,
            max_plan_steps=settings.max_plan_steps,
        ),
    )
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
        {REPAIR: REPAIR, REPLAN: REPLAN, REVIEW: REVIEW, RESPOND: "respond"},
    )
    # 验证通过后过一遍代码审查，它决定是步进还是打回修复
    graph.add_conditional_edges(
        REVIEW,
        make_route_after_review(
            settings.max_repair_rounds, nudge_empty_steps=settings.nudge_empty_steps
        ),
        {
            NUDGE: NUDGE,
            REPAIR: REPAIR,
            REPLAN: REPLAN,
            ADVANCE: "advance",
            RESPOND: "respond",
        },
    )
    # 一步没产生任何改动 → 带提示重做一次（上限一次，见 nodes/nudge.py）
    graph.add_edge(NUDGE, "act")
    graph.add_edge(REPAIR, "act")
    # 步进之后必过 replan：它可能把剩余步骤重写（甚至清空，直接去收尾）
    graph.add_edge("advance", REPLAN)
    graph.add_conditional_edges(
        REPLAN,
        route_after_replan,
        {ACT: ACT, RESPOND: RESPOND},
    )
    graph.add_edge("respond", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())
