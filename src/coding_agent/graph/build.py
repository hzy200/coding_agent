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

from coding_agent.agents.explore import make_explore_runner, make_review_runner
from coding_agent.config import Settings, get_settings
from coding_agent.graph.nodes import (
    advance,
    make_act_node,
    make_approval_gate_node,
    make_planner_node,
    make_replan_node,
    make_respond_node,
    make_review_node,
    make_steering_dispatch_node,
    make_steering_final_node,
    make_steering_plan_node,
    make_steering_step_node,
    make_tools_node,
    make_verify_node,
    nudge,
    repair,
)
from coding_agent.graph.routing import (
    ACT,
    ADVANCE,
    APPROVE,
    DISPATCH,
    EXTEND_TASKS,
    NUDGE,
    PLAN_TASKS,
    REPAIR,
    REPLAN,
    RESPOND,
    REVIEW,
    STEERING_DISPATCH,
    STEERING_FINAL,
    STEERING_PLAN,
    STEERING_STEP,
    SYNTHESIZE,
    TOOLS,
    VERIFY,
    make_route_after_review,
    make_route_after_verify,
    route_after_act,
    route_after_replan,
    route_after_steering_dispatch,
    route_after_steering_plan,
    route_after_steering_step,
    route_after_synthesize,
)
from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import build_llm
from coding_agent.orchestrator import (
    make_dispatch_node,
    make_extend_tasks_node,
    make_plan_tasks_node,
    make_select_workflow_node,
    make_survey_node,
    make_synthesize_node,
    make_worker_runner,
    route_after_select,
    route_entry,
)
from coding_agent.orchestrator.implement import make_implement_runner
from coding_agent.sandbox.policy import SessionPolicy
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.steering import SteeringController
from coding_agent.tools.delegate import EXPLORE_TOOL_NAMES, build_delegate_tool
from coding_agent.tools.registry import build_tools

# planner + respond 各算 1 次，再留少量余量给中断/重入
_RECURSION_MARGIN = 10


def estimate_recursion_limit(
    max_plan_steps: int,
    max_tool_rounds: int,
    max_repair_rounds: int,
    max_replans: int,
    *,
    orchestrator_enabled: bool = False,
    orchestrator_max_rounds: int = 0,
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
    # Phase 3D：advance 之后多一个 steering_step 消费点。
    per_step += 1
    # 失败重规划若让本步通过，仍会走 advance → steering_step → replan
    # （步进微调），这两个超步落在相邻步骤之间，最多每步各一次。
    base = max_plan_steps * (per_step + 2) + 1 + _RECURSION_MARGIN
    if not orchestrator_enabled:
        return base
    # survey + select_workflow + plan_tasks + steering_plan，以及每轮
    # dispatch + extend_tasks + steering_dispatch。用户重规划会让
    # orchestrator_round 归零，所以整块按 (max_replans + 1) 再重复。
    # steering_final 只出现一次，由上式的 +1 与余量覆盖。
    orchestrator_extra = (max_replans + 1) * (
        4 + max(int(orchestrator_max_rounds), 0) * 3
    )
    return base + orchestrator_extra


def build_graph(
    settings: Settings | None = None,
    *,
    checkpointer=None,
    allow_write: bool = False,
    policy: SessionPolicy | None = None,
    sandbox: WslSandbox | None = None,
    steering: SteeringController | None = None,
):
    # 一次运行只该有一份沙箱：环境探测（$HOME、bwrap、rg）的结果缓存在**实例**上，
    # 各建一份就等于各探一遍 —— 曾经这里是 runtime / build_tools / verify 三份，
    # 冷启动光探测就要 4 次 wsl.exe 进程启动。
    settings = settings or get_settings()
    policy = policy or SessionPolicy(allow_write=allow_write)
    sandbox = sandbox or WslSandbox(settings)
    # runtime 会把自己的 controller 传进来；直接 build_graph 的调用方
    # （测试、脚本）拿一个空队列即可。
    steering = steering or SteeringController()
    # 工作区只解析一次，再往下传：verify 的沙箱是共享的，它那份 settings 未必带
    # `--workspace` 覆盖，让它自己推会退回 $HOME。
    root = resolve_workspace(settings, sandbox)
    tools = build_tools(settings, allow_write=allow_write, sandbox=sandbox)

    # 不绑定工具的实例留给 planner / respond，避免它们在规划或收尾阶段发起动作
    llm = build_llm(settings, streaming=True)

    # 子智能体/编排 worker 共用同一份只读 Explore runner。
    # delegate 工具只在 explore_enabled 时绑定；orchestrator 只使用 runner。
    explore_runner = None
    review_runner = None
    delegate_runner = None
    if settings.explore_enabled or settings.orchestrator_enabled:
        explore_tools = [tool for tool in tools if tool.name in EXPLORE_TOOL_NAMES]
        explore_runner = make_explore_runner(
            llm=llm, tools=explore_tools, settings=settings
        )
    if settings.orchestrator_enabled:
        review_runner = make_review_runner(
            llm=llm, tools=explore_tools, settings=settings
        )
    implement_runner = None
    if settings.orchestrator_enabled and settings.orchestrator_write_enabled and allow_write:
        implement_runner = make_implement_runner(
            llm=llm, sandbox=sandbox, root=root, settings=settings
        )
    if settings.explore_enabled:
        delegate_runner = explore_runner
        tools = [*tools, build_delegate_tool()]

    llm_with_tools = llm.bind_tools(tools)
    # 被 nudge 的实现步强制出一次工具动作；provider 不支持时 act 会回退。
    llm_forced = llm.bind_tools(tools, tool_choice="required")

    graph = StateGraph(AgentState)
    graph.add_node(
        "planner",
        make_planner_node(
            llm,
            max_steps=settings.max_plan_steps,
            allow_write=allow_write,
            delegate_enabled=settings.explore_enabled,
        ),
    )
    graph.add_node(
        "act",
        make_act_node(
            llm_with_tools,
            max_tool_rounds=settings.max_tool_rounds,
            allow_write=allow_write,
            max_repair_rounds=settings.max_repair_rounds,
            budget=settings.context_budget,
            delegate_enabled=settings.explore_enabled,
            forced_llm=llm_forced,
        ),
    )
    graph.add_node(APPROVE, make_approval_gate_node(policy))
    graph.add_node(
        TOOLS,
        make_tools_node(
            tools,
            delegate_runner=delegate_runner,
            max_delegate_calls=settings.max_delegate_calls,
            max_concurrent_agents=settings.max_concurrent_agents,
        ),
    )
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

    # ---- Phase 3A orchestrator ----
    worker_runner = make_worker_runner(
        explore_runner=explore_runner,
        review_runner=review_runner,
        implement_runner=implement_runner,
        sandbox=sandbox,
        root=root,
    )
    graph.add_node("survey", make_survey_node(sandbox, root))
    graph.add_node(
        "select_workflow",
        make_select_workflow_node(llm, settings, allow_write=allow_write),
    )
    graph.add_node(
        "plan_tasks", make_plan_tasks_node(llm, settings, allow_write=allow_write)
    )
    graph.add_node(
        DISPATCH,
        make_dispatch_node(
            worker_runner=worker_runner,
            settings=settings,
            sandbox=sandbox,
            root=root,
            steering=steering,
        ),
    )
    # worker 提议的只读任务在这里进入任务图（host 校验/去重/总量上限）
    graph.add_node(EXTEND_TASKS, make_extend_tasks_node(settings))
    graph.add_node("synthesize", make_synthesize_node(settings))
    # Phase 3D：四个 steering 消费点（见 nodes/steering.py 顶部说明）
    graph.add_node(STEERING_PLAN, make_steering_plan_node(steering, settings))
    graph.add_node(
        STEERING_DISPATCH, make_steering_dispatch_node(steering, settings)
    )
    graph.add_node(STEERING_STEP, make_steering_step_node(steering, settings))
    graph.add_node(STEERING_FINAL, make_steering_final_node(steering, settings))

    # direct 走旧路径；orchestrated 先 survey/选择/调度，最后仍回到 planner。
    graph.add_conditional_edges(
        START,
        route_entry,
        {"planner": "planner", "survey": "survey"},
    )
    graph.add_edge("survey", "select_workflow")
    graph.add_conditional_edges(
        "select_workflow",
        route_after_select,
        {PLAN_TASKS: PLAN_TASKS, "planner": "planner"},
    )
    graph.add_edge(PLAN_TASKS, STEERING_PLAN)
    graph.add_conditional_edges(
        STEERING_PLAN,
        route_after_steering_plan,
        {DISPATCH: DISPATCH, SYNTHESIZE: SYNTHESIZE},
    )
    graph.add_edge(DISPATCH, EXTEND_TASKS)
    graph.add_edge(EXTEND_TASKS, STEERING_DISPATCH)
    graph.add_conditional_edges(
        STEERING_DISPATCH,
        route_after_steering_dispatch,
        {
            DISPATCH: DISPATCH,
            PLAN_TASKS: PLAN_TASKS,
            SYNTHESIZE: SYNTHESIZE,
        },
    )
    graph.add_conditional_edges(
        SYNTHESIZE,
        route_after_synthesize,
        {STEERING_FINAL: STEERING_FINAL, "planner": "planner"},
    )
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
        make_route_after_verify(
            settings.max_repair_rounds,
            max_same_failures=settings.max_same_failures,
        ),
        {
            REPAIR: REPAIR,
            REPLAN: REPLAN,
            REVIEW: REVIEW,
            RESPOND: STEERING_FINAL,
        },
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
            ADVANCE: ADVANCE,
            RESPOND: STEERING_FINAL,
        },
    )
    # 一步没产生任何改动 → 带提示重做一次（上限一次，见 nodes/nudge.py）
    graph.add_edge(NUDGE, "act")
    graph.add_edge(REPAIR, "act")
    # 步进之后先消费干预，再由 replan 重写剩余步骤（甚至清空，直接去收尾）。
    graph.add_edge(ADVANCE, STEERING_STEP)
    graph.add_conditional_edges(
        STEERING_STEP,
        route_after_steering_step,
        {REPLAN: REPLAN, RESPOND: RESPOND},
    )
    graph.add_conditional_edges(
        REPLAN,
        route_after_replan,
        {ACT: ACT, RESPOND: STEERING_FINAL},
    )
    # 所有进入 respond 的路径都先经过 final 消费点：单步任务也能被取消。
    graph.add_edge(STEERING_FINAL, RESPOND)
    graph.add_edge(RESPOND, END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())
