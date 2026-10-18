"""独立 orchestrator 子图。

第一版是**行为等价迁移**：节点与旧编排路径相同，只是收进独立子图。
父图把 `request/cwd/repo_profile` 传进来，子图把编排字段合并回父状态。

事件层依赖内层节点名（`select_workflow` / `plan_tasks` / `dispatch` / ...），
所以运行父图时必须使用 `subgraphs=True`；否则 LangGraph 只会上报子图整体的
最终更新，内层事件会丢失。
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from coding_agent.config import Settings

# 与 graph/nodes/steering.py 的节点工厂同名，避免两套 steering 语义。
from coding_agent.graph.nodes.steering import (
    make_steering_dispatch_node,
    make_steering_plan_node,
)
from coding_agent.graph.routing import (
    DISPATCH,
    EXTEND_TASKS,
    PLAN_TASKS,
    STEERING_DISPATCH,
    STEERING_PLAN,
    SYNTHESIZE,
    route_after_steering_dispatch,
    route_after_steering_plan,
)
from coding_agent.orchestrator.nodes import (
    make_dispatch_node,
    make_extend_tasks_node,
    make_plan_tasks_node,
    make_survey_node,
    make_synthesize_node,
    route_after_select,
)
from coding_agent.orchestrator.scheduler import SCHEDULER, make_scheduler_node
from coding_agent.orchestrator.state import OrchestratorState
from coding_agent.orchestrator.workers import WorkerRunner
from coding_agent.orchestrator.workflow import make_select_workflow_node
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.steering import SteeringController


def _route_after_plan_event(state: OrchestratorState) -> str:
    return SYNTHESIZE if state.get("steering_cancelled") else SCHEDULER


def _route_after_scheduler_gate(state: OrchestratorState) -> str:
    if state.get("steering_cancelled"):
        return SYNTHESIZE
    if state.get("steering_replan"):
        return PLAN_TASKS
    if any(
        value == "pending"
        for value in (state.get("task_status") or {}).values()
    ):
        return SCHEDULER
    return SYNTHESIZE


def build_orchestrator_subgraph(
    *,
    llm: BaseChatModel,
    settings: Settings,
    sandbox: WslSandbox,
    root: str,
    worker_runner: WorkerRunner,
    steering: SteeringController,
    allow_write: bool = False,
):
    """组装并编译独立子图。checkpointer 由父图持有。"""
    graph = StateGraph(OrchestratorState)
    event_driven = bool(settings.orchestrator_event_driven)

    graph.add_node("survey", make_survey_node(sandbox, root))
    graph.add_node(
        "select_workflow",
        make_select_workflow_node(llm, settings, allow_write=allow_write),
    )
    graph.add_node(
        PLAN_TASKS,
        make_plan_tasks_node(llm, settings, allow_write=allow_write),
    )
    graph.add_node(
        STEERING_PLAN,
        make_steering_plan_node(steering, settings),
    )
    graph.add_node(
        STEERING_DISPATCH,
        make_steering_dispatch_node(steering, settings),
    )
    graph.add_node(
        SYNTHESIZE,
        make_synthesize_node(
            settings,
            sandbox=sandbox,
            root=root,
            artifacts=settings.orchestrator_fanout_artifacts,
        ),
    )

    if event_driven:
        graph.add_node(
            SCHEDULER,
            make_scheduler_node(
                worker_runner=worker_runner,
                settings=settings,
                sandbox=sandbox,
                root=root,
                steering=steering,
            ),
        )
    else:
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
        graph.add_node(EXTEND_TASKS, make_extend_tasks_node(settings))

    graph.add_edge(START, "survey")
    graph.add_edge("survey", "select_workflow")
    # 规则/模型选择 direct 时直接结束子图；父图会继续原来的 planner 路径。
    graph.add_conditional_edges(
        "select_workflow",
        route_after_select,
        {PLAN_TASKS: PLAN_TASKS, "planner": END},
    )
    graph.add_edge(PLAN_TASKS, STEERING_PLAN)

    if event_driven:
        graph.add_conditional_edges(
            STEERING_PLAN,
            _route_after_plan_event,
            {SCHEDULER: SCHEDULER, SYNTHESIZE: SYNTHESIZE},
        )
        graph.add_edge(SCHEDULER, STEERING_DISPATCH)
        graph.add_conditional_edges(
            STEERING_DISPATCH,
            _route_after_scheduler_gate,
            {
                SCHEDULER: SCHEDULER,
                PLAN_TASKS: PLAN_TASKS,
                SYNTHESIZE: SYNTHESIZE,
            },
        )
    else:
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

    graph.add_edge(SYNTHESIZE, END)

    return graph.compile()
