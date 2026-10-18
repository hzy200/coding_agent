"""Phase 3D：steering 队列、节点与编排器取消。

这些用例不碰 WSL、不调模型：节点是纯函数，暂停用 LangGraph 的 MemorySaver
走真实的 interrupt/resume 语义。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from coding_agent.config import Settings
from coding_agent.graph.nodes.steering import (
    make_steering_dispatch_node,
    make_steering_final_node,
    make_steering_plan_node,
    make_steering_step_node,
)
from coding_agent.graph.state import AgentState
from coding_agent.orchestrator.contracts import TaskGraph, TaskSpec, WorkerResult
from coding_agent.orchestrator.nodes import make_dispatch_node, make_plan_tasks_node
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.steering import SteeringCommand, SteeringController


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [HumanMessage(content="做事")],
        "execution_mode": "orchestrated",
        "tasks": [{"id": "t1", "role": "explore", "goal": "定位", "depends_on": []}],
        "task_status": {"t1": "pending"},
        "plan": [],
        "plan_kinds": [],
        "steering_replans": 0,
        "task_attempts": {},
        "worker_results": [],
        "orchestrator_round": 0,
    }
    state.update(overrides)
    return state


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------

def test_controller_consume_and_requeue_preserve_order() -> None:
    controller = SteeringController()
    first = SteeringCommand("add", text="甲")
    second = SteeringCommand("replan")
    controller.submit("t", first)
    controller.submit("t", second)

    assert controller.peek("t") == [first, second]
    assert controller.cancel_requested("t") is False

    controller.requeue("t", [first])
    assert controller.consume("t") == [first, first, second]
    assert controller.consume("t") == []


def test_controller_cancel_is_non_destructive() -> None:
    controller = SteeringController()
    controller.submit("t", SteeringCommand("cancel"))

    assert controller.cancel_requested("t") is True
    assert controller.cancel_requested("t") is True
    assert len(controller.peek("t")) == 1


def test_add_rejects_write_roles() -> None:
    with pytest.raises(ValueError, match="只读角色"):
        SteeringCommand("add", text="改文件", role="implement")
    with pytest.raises(ValueError, match="不支持"):
        SteeringCommand("resume")


def test_pause_flag_is_cleared_by_controller_clear() -> None:
    controller = SteeringController()
    controller.mark_paused("t")
    assert controller.is_paused("t") is True

    controller.clear("t")
    assert controller.is_paused("t") is False
    assert controller.peek("t") == []


# --------------------------------------------------------------------------
# 节点：add / cancel / replan
# --------------------------------------------------------------------------

def test_dispatch_node_appends_a_read_only_task() -> None:
    controller = SteeringController()
    controller.submit("", SteeringCommand("add", text="检查错误处理", role="review"))
    node = make_steering_dispatch_node(controller, _settings())

    out = node(_state(), None)

    assert out["tasks"][-1]["id"] == "user-1"
    assert out["tasks"][-1]["role"] == "review"
    assert out["task_status"]["user-1"] == "pending"
    assert out["steering_applied"][0]["action"] == "add"
    assert controller.peek("") == []


def test_dispatch_node_cancel_marks_pending_tasks_cancelled() -> None:
    controller = SteeringController()
    controller.submit("", SteeringCommand("cancel"))
    node = make_steering_dispatch_node(controller, _settings())

    out = node(_state(), None)

    assert out["steering_cancelled"] is True
    assert out["task_status"]["t1"] == "cancelled"
    assert out["steering_applied"][0]["action"] == "cancel"


def test_dispatch_node_replan_sets_the_flag_and_counter() -> None:
    controller = SteeringController()
    controller.submit("", SteeringCommand("replan"))
    node = make_steering_dispatch_node(controller, _settings(max_replans=2))

    out = node(_state(), None)

    assert out["steering_replan"] is True
    assert out["steering_replans"] == 1


def test_dispatch_node_replan_stops_at_the_limit() -> None:
    controller = SteeringController()
    controller.submit("", SteeringCommand("replan"))
    node = make_steering_dispatch_node(controller, _settings(max_replans=2))

    out = node(_state(steering_replans=2), None)

    assert "steering_replan" not in out
    assert "上限" in out["steering_applied"][0]["detail"]


def test_step_node_add_appends_a_plan_step_in_direct_mode() -> None:
    controller = SteeringController()
    controller.submit("", SteeringCommand("add", text="补一个只读检查"))
    node = make_steering_step_node(controller, _settings())

    out = node(
        _state(execution_mode="direct", plan=["做 A"], plan_kinds=["explore"]), None
    )

    assert out["plan"] == ["做 A", "补一个只读检查"]
    assert out["plan_kinds"] == ["explore", "explore"]


def test_final_node_consumes_late_cancel_and_ignores_replan() -> None:
    controller = SteeringController()
    controller.submit("", SteeringCommand("cancel"))
    controller.submit("", SteeringCommand("replan"))
    node = make_steering_final_node(controller, _settings())

    out = node(_state(), None)

    assert out["steering_cancelled"] is True
    actions = [item["action"] for item in out["steering_applied"]]
    assert actions == ["cancel", "replan"]
    assert "收尾" in out["steering_applied"][1]["detail"]


# --------------------------------------------------------------------------
# pause：真实 interrupt/resume
# --------------------------------------------------------------------------

def _pause_graph(controller: SteeringController):
    graph = StateGraph(AgentState)
    graph.add_node("steering", make_steering_plan_node(controller, _settings()))
    graph.add_edge(START, "steering")
    graph.add_edge("steering", END)
    return graph.compile(checkpointer=MemorySaver())


def _collect(graph, payload, config) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        # 单 stream_mode 时 astream 直接产出 update 字典，不再包 (mode, data)
        return [
            chunk
            async for chunk in graph.astream(
                payload, config, stream_mode="updates"
            )
        ]

    return _run(run())


def test_pause_interrupts_and_requeues_sibling_commands() -> None:
    controller = SteeringController()
    controller.submit("t", SteeringCommand("pause"))
    controller.submit("t", SteeringCommand("add", text="暂停后追加"))
    graph = _pause_graph(controller)
    config = {"configurable": {"thread_id": "t"}}

    chunks = _collect(graph, {"messages": []}, config)

    assert any("__interrupt__" in chunk for chunk in chunks)
    # pause 已消费；同批的 add 被回填，恢复后才应用
    assert [c.action for c in controller.peek("t")] == ["add"]
    assert controller.is_paused("t") is True

    resumed = _collect(graph, Command(resume={"steering": "resume"}), config)

    applied = [
        item
        for chunk in resumed
        for item in (chunk.get("steering") or {}).get("steering_applied") or []
    ]
    assert applied and applied[0]["action"] == "add"
    assert controller.is_paused("t") is False
    snapshot = graph.get_state(config)
    assert snapshot.values["plan"] == ["暂停后追加"]


# --------------------------------------------------------------------------
# 编排器：取消派发与跳过未合并 patch
# --------------------------------------------------------------------------

def test_dispatch_does_not_start_workers_after_cancel() -> None:
    controller = SteeringController()
    controller.submit("", SteeringCommand("cancel"))
    called: list[str] = []

    async def runner(task, **_kwargs):
        called.append(task["id"])
        return WorkerResult(task_id=task["id"], role=task["role"], status="success")

    node = make_dispatch_node(
        worker_runner=runner,
        settings=_settings(),
        steering=controller,
    )

    out = _run(node(_state(), None))

    assert called == []
    assert out["task_status"]["t1"] == "cancelled"


class _MergeSandbox:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: str | None = None, timeout: int | None = None):
        self.commands.append(command)
        return ExecResult(command=command, exit_code=0, stdout="", stderr="", duration_ms=1)


def test_dispatch_discards_patches_when_cancel_arrives_mid_batch() -> None:
    controller = SteeringController()

    async def runner(task, **_kwargs):
        controller.submit("", SteeringCommand("cancel"))
        return WorkerResult(
            task_id=task["id"],
            role="implement",
            status="success",
            summary="做完了",
            patch_path="/ws/.agent/patches/t1.patch",
        )

    sandbox = _MergeSandbox()
    node = make_dispatch_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=0),
        sandbox=sandbox,  # type: ignore[arg-type]
        root="/ws",
        steering=controller,
    )

    out = _run(
        node(
            _state(
                tasks=[
                    {
                        "id": "t1",
                        "role": "implement",
                        "goal": "改",
                        "write_scope": ["app/a.py"],
                        "depends_on": [],
                    }
                ],
                task_status={"t1": "pending"},
            ),
            None,
        )
    )

    record = out["worker_results"][0]
    assert record["status"] == "cancelled"
    assert record["merged"] is False
    assert sandbox.commands == []


# --------------------------------------------------------------------------
# 编排器：用户重规划保留已完成任务
# --------------------------------------------------------------------------

class _Structured:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    def with_retry(self, **_kwargs: Any) -> _Structured:
        return self

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        self.calls += 1
        return self.result


class _StructuredLLM:
    def __init__(self, structured: _Structured) -> None:
        self.structured = structured

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> _Structured:
        return self.structured


def test_replan_keeps_completed_tasks_and_prefixes_new_ids() -> None:
    graph = TaskGraph(
        tasks=[TaskSpec(id="t1", role="explore", goal="检查剩余边界")]
    )
    node = make_plan_tasks_node(
        _StructuredLLM(_Structured(graph)), _settings()
    )
    state = _state(
        workflow="parallel_readonly",
        repo_summary={"file_count": 50},
        tasks=[{"id": "done", "role": "explore", "goal": "已完成", "depends_on": []}],
        task_status={"done": "success"},
        worker_results=[
            {
                "task_id": "done",
                "role": "explore",
                "attempt": 1,
                "status": "success",
                "summary": "结论",
            }
        ],
        steering_replan=True,
        steering_replans=1,
    )

    out = _run(node(state, None))

    assert out["tasks"][0]["id"] == "done"
    assert out["task_status"]["done"] == "success"
    assert out["tasks"][1]["id"] == "r1-t1"
    assert out["task_status"]["r1-t1"] == "pending"
    assert out["steering_replan"] is False
    assert "保留 1 个已完成任务" in out["workflow_revised_reason"]


def test_replan_after_a_merged_patch_downgrades_new_write_tasks() -> None:
    graph = TaskGraph(
        tasks=[
            TaskSpec(
                id="t1",
                role="implement",
                goal="再改一处",
                write_scope=["app/a.py"],
            )
        ]
    )
    node = make_plan_tasks_node(
        _StructuredLLM(_Structured(graph)),
        _settings(orchestrator_write_enabled=True),
        allow_write=True,
    )
    state = _state(
        workflow="orchestrated_implement",
        repo_summary={"file_count": 50},
        tasks=[{"id": "done", "role": "implement", "goal": "已合并"}],
        task_status={"done": "success"},
        worker_results=[
            {
                "task_id": "done",
                "role": "implement",
                "attempt": 1,
                "status": "success",
                "merged": True,
                "summary": "已合并",
            }
        ],
        steering_replan=True,
        steering_replans=1,
    )

    out = _run(node(state, None))

    new_tasks = [task for task in out["tasks"] if task["id"] != "done"]
    assert new_tasks
    assert all(task["role"] != "implement" for task in new_tasks)
