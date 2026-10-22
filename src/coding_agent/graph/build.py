"""组装 LangGraph。

拓扑：

    START → planner → act → steering_cancel
              ┌─(有 tool_calls)─→ approval_gate → tools ─→ steering_cancel
              └─(无 tool_calls)─→ verify ─┬─(通过)─→ review ─┬─(通过)─→ advance → replan
                                          │                 ├─→ act
                                          │                 └─→ respond → END
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

import math
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from coding_agent.agents.explore import make_explore_runner, make_review_runner
from coding_agent.concurrency import ConcurrencyGovernor
from coding_agent.config import (
    PRODUCTION_PROFILES,
    Settings,
    apply_security_profile,
    get_settings,
)
from coding_agent.graph.nodes import (
    advance,
    make_act_node,
    make_approval_gate_node,
    make_planner_node,
    make_replan_node,
    make_respond_node,
    make_review_node,
    make_steering_cancel_node,
    make_steering_final_node,
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
    NUDGE,
    REPAIR,
    REPLAN,
    RESPOND,
    REVIEW,
    STEERING_CANCEL,
    STEERING_FINAL,
    STEERING_STEP,
    TOOLS,
    VERIFY,
    make_route_after_review,
    make_route_after_verify,
    route_after_replan,
    route_after_steering_cancel,
    route_after_steering_step,
    route_after_synthesize,
)
from coding_agent.graph.state import AgentState
from coding_agent.llm.deepseek import build_llm
from coding_agent.memory.longterm import resolve_memory_root
from coding_agent.orchestrator import make_worker_runner, route_entry
from coding_agent.orchestrator.implement import make_implement_runner
from coding_agent.orchestrator.subgraph import build_orchestrator_subgraph
from coding_agent.read_cache import ReadCache
from coding_agent.sandbox.policy import SessionPolicy, parse_allow_rules
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.steering import SteeringController
from coding_agent.tool_journal import ToolJournal
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
    orchestrator_max_tasks: int = 6,
    orchestrator_checkpoint_every: int = 4,
) -> int:
    """按图拓扑估算 `recursion_limit`，避免把限额算小导致正常任务误报。

    一个周期（act 跑满本轮工具预算、直到不再产 tool_calls）最坏走过的超步：
    `act` 被调 (轮次+1) 次（末次超预算不产 tool_calls，直接去 verify）
    + `steering_cancel` 每次 act 后 1 次、每次 tools 后 1 次（共 `2×轮次+1`）
    + `approval_gate` 与 `tools` 各 轮次 次 + `verify` 1 次 + `review` 1 次，
    即 `5×轮次 + 4`。

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
    per_cycle = (
        (max_tool_rounds + 1)          # act
        + (2 * max_tool_rounds + 1)    # steering_cancel（act 后与 tools 后）
        + 2 * max_tool_rounds          # approval_gate + tools
        + 2                            # verify + review
    )
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
    # 独立子图：survey + select_workflow + plan_tasks + steering_plan +
    # write_gate/fan_in，以及每个 checkpoint 边界的 scheduler + steering_dispatch。
    # 边界数由任务总数与 checkpoint_every 决定；动态扩展上限就是 max_tasks。
    # 用户重规划会让任务重新规划，所以整块按 (max_replans + 1) 再重复。
    boundaries = max(
        1,
        math.ceil(
            max(int(orchestrator_max_tasks), 1)
            / max(int(orchestrator_checkpoint_every), 1)
        ),
    )
    orchestrator_extra = (max_replans + 1) * (6 + boundaries * 2)
    # 父图里的 orchestrator 节点本身 + 子图入口/出口各一个超步。
    orchestrator_extra += 2
    return base + orchestrator_extra


def build_graph(
    settings: Settings | None = None,
    *,
    checkpointer=None,
    allow_write: bool = False,
    policy: SessionPolicy | None = None,
    sandbox: WslSandbox | None = None,
    steering: SteeringController | None = None,
    governor: ConcurrencyGovernor | None = None,
):
    # 一次运行只该有一份沙箱：环境探测（$HOME、bwrap、rg）的结果缓存在**实例**上，
    # 各建一份就等于各探一遍 —— 曾经这里是 runtime / build_tools / verify 三份，
    # 冷启动光探测就要 4 次 wsl.exe 进程启动。
    settings = apply_security_profile(settings or get_settings())
    if str(settings.security_profile or "").strip().lower() in PRODUCTION_PROFILES:
        allow_write = False
    policy = policy or SessionPolicy(
        allow_write=allow_write,
        approval_mode=settings.approval_mode,
        allow_rules=parse_allow_rules(settings.allow_rules),
        read_deny_globs=settings.read_deny_globs,
    )
    governor = governor or ConcurrencyGovernor(settings)
    sandbox = sandbox or WslSandbox(settings, slots=governor.wsl)
    # runtime 会把自己的 controller 传进来；直接 build_graph 的调用方
    # （测试、脚本）拿一个空队列即可。
    steering = steering or SteeringController()
    # 工作区只解析一次，再往下传：verify 的沙箱是共享的，它那份 settings 未必带
    # `--workspace` 覆盖，让它自己推会退回 $HOME。
    root = resolve_workspace(settings, sandbox)
    # bwrap 的 workdir/可写绑定读的是 sandbox.settings；配置留空时把推导值写回。
    if sandbox.settings.wsl_workspace != root:
        sandbox.settings = settings.model_copy(update={"wsl_workspace": root})
    # 不可信仓库的记忆在仓库外；bwrap 只绑定工作区，必须显式加入可写绑定。
    memory_root = resolve_memory_root(settings, sandbox, root)
    if memory_root != root:
        sandbox.ensure_directory(memory_root)
        sandbox.add_extra_bind(memory_root)
    # 同一次运行内只读 worker 共享内容缓存；写路径会主动失效。
    read_cache = ReadCache(settings.read_cache_max_bytes)
    tools = build_tools(
        settings,
        allow_write=allow_write,
        sandbox=sandbox,
        read_cache=read_cache,
    )
    # 有副作用工具的执行日志放在宿主审计目录，不依赖沙箱。
    journal = ToolJournal(
        Path(settings.resolved_audit_dir) / "tool_journal.jsonl",
        enabled=settings.tool_journal_enabled,
    )

    # 不绑定工具的实例留给 planner / respond，避免它们在规划或收尾阶段发起动作
    llm = build_llm(
        settings,
        streaming=True,
        task_type="agent",
        required_capabilities=("tools", "chat"),
        governor=governor,
    )
    # 模型分层：explore/review/implement worker 可独立使用更便宜的模型；
    # 空串表示沿用主模型。
    worker_llm = llm
    if settings.orchestrator_worker_model:
            worker_llm = build_llm(
                settings,
                streaming=True,
                model_override=settings.orchestrator_worker_model,
                task_type="worker",
                governor=governor,
            )

    # 子智能体/编排 worker 共用同一份只读 Explore runner。
    # delegate 工具只在 explore_enabled 时绑定；orchestrator 只使用 runner。
    explore_runner = None
    review_runner = None
    delegate_runner = None
    if settings.explore_enabled or settings.orchestrator_enabled:
        explore_tools = [tool for tool in tools if tool.name in EXPLORE_TOOL_NAMES]
        explore_runner = make_explore_runner(
            llm=worker_llm, tools=explore_tools, settings=settings
        )
    if settings.orchestrator_enabled:
        review_runner = make_review_runner(
            llm=worker_llm, tools=explore_tools, settings=settings
        )
    implement_runner = None
    if settings.orchestrator_enabled and settings.orchestrator_write_enabled and allow_write:
        implement_runner = make_implement_runner(
            llm=worker_llm, sandbox=sandbox, root=root, settings=settings
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
            retry_attempts=settings.retry_attempts,
            retry_base_delay=settings.retry_base_delay_s,
            retry_max_delay=settings.retry_max_delay_s,
            retry_max_total_wait=settings.retry_max_total_wait_s,
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
            retry_attempts=settings.retry_attempts,
            retry_base_delay=settings.retry_base_delay_s,
            retry_max_delay=settings.retry_max_delay_s,
            retry_max_total_wait=settings.retry_max_total_wait_s,
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
            max_concurrent_tools=settings.max_concurrent_tools,
            journal=journal,
            governor=governor,
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
            retry_attempts=settings.retry_attempts,
            retry_base_delay=settings.retry_base_delay_s,
            retry_max_delay=settings.retry_max_delay_s,
            retry_max_total_wait=settings.retry_max_total_wait_s,
        ),
    )

    # ---- Phase 3A orchestrator ----
    worker_runner = make_worker_runner(
        explore_runner=explore_runner,
        review_runner=review_runner,
        implement_runner=implement_runner,
        sandbox=sandbox,
        root=root,
        memory_top_k=settings.memory_top_k,
        settings=settings,
    )
    # 独立子图：父图只看到 orchestrator 一个节点；内层节点通过
    # `subgraphs=True` 的 stream 上报事件。
    graph.add_node(
        "orchestrator",
        build_orchestrator_subgraph(
            llm=llm,
            settings=settings,
            sandbox=sandbox,
            root=root,
            worker_runner=worker_runner,
            steering=steering,
            allow_write=allow_write,
            governor=governor,
        ),
    )
    graph.add_node(STEERING_STEP, make_steering_step_node(steering, settings))
    graph.add_node(
        STEERING_CANCEL, make_steering_cancel_node(steering, settings)
    )
    graph.add_node(STEERING_FINAL, make_steering_final_node(steering, settings))

    # direct 走单 agent；orchestrated 进独立子图，最后仍回到 planner。
    graph.add_conditional_edges(
        START,
        route_entry,
        {"planner": "planner", "survey": "orchestrator"},
    )
    graph.add_conditional_edges(
        "orchestrator",
        route_after_synthesize,
        {STEERING_FINAL: STEERING_FINAL, "planner": "planner"},
    )
    graph.add_edge("planner", "act")
    # act ⇄ tools 的每一段都先过取消检查点：cancel 不必等整个 step 跑完，
    # 且在真正执行工具之前就能截断。add/replan/pause 不在这里消费。
    graph.add_edge("act", STEERING_CANCEL)
    graph.add_edge(TOOLS, STEERING_CANCEL)
    graph.add_conditional_edges(
        STEERING_CANCEL,
        route_after_steering_cancel,
        {
            STEERING_FINAL: STEERING_FINAL,
            APPROVE: APPROVE,
            VERIFY: VERIFY,
            ACT: "act",
        },
    )
    graph.add_edge(APPROVE, TOOLS)
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
