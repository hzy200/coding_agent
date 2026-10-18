"""独立 orchestrator 子图的等价迁移测试。"""

from __future__ import annotations

import asyncio
from typing import Any

from coding_agent.config import Settings
from coding_agent.orchestrator.contracts import (
    TaskGraph,
    TaskSpec,
    WorkerResult,
    WorkflowDecision,
)
from coding_agent.orchestrator.subgraph import build_orchestrator_subgraph
from coding_agent.orchestrator.workflow import WorkflowPlan
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.steering import SteeringController


class _Structured:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    def with_retry(self, **_kwargs: Any) -> _Structured:
        return self

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        self.calls += 1
        return self.result


class _TaskGraphLLM:
    """按 schema 返回结构化结果；规则命中时工作流选择不会真正调用它。"""

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> _Structured:
        tasks = [
            TaskSpec(id="t1", role="explore", goal="定位入口"),
            TaskSpec(id="t2", role="explore", goal="定位测试"),
        ]
        if schema is WorkflowPlan:
            return _Structured(
                WorkflowPlan(
                    workflow="parallel_readonly",
                    reason="规则命中",
                    tasks=tasks,
                )
            )
        if schema is WorkflowDecision:
            return _Structured(
                WorkflowDecision(workflow="parallel_readonly", reason="规则命中")
            )
        assert schema is TaskGraph
        return _Structured(TaskGraph(tasks=tasks))


class _FakeSandbox:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(
        self, command: str, *, cwd: str | None = None, timeout: int | None = None
    ) -> ExecResult:
        self.commands.append(command)
        stdout = ""
        if "AGENT_DIRS" in command:
            stdout = (
                "AGENT_DIRS\n./app\n"
                "AGENT_FILES\n./main.py\n"
                "AGENT_GIT\nINSIDE=1\nROOT=1\nCLEAN=1\n"
            )
        elif "mindepth 1 -type d" in command:
            stdout = "./app\n"
        elif "mindepth 1 -type f" in command:
            stdout = "./main.py\n"
        elif "command -v bwrap" in command:
            stdout = ""
        elif "for f in" in command:
            stdout = "FILE pyproject.toml\nDIR tests\nHAS pytest\nHAS python\n"
        elif "rev-parse --is-inside-work-tree" in command:
            stdout = "true\n"
        elif "rev-parse --show-toplevel" in command or "pwd -P" in command:
            stdout = "/mnt/d/proj\n"
        elif "git status --porcelain" in command:
            stdout = ""
        return ExecResult(
            command=command,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_ms=0,
        )

    def cached_probe(self, key: str, producer: Any) -> Any:
        return producer()

    def forget_probe(self, key: str) -> None:
        return None


async def _fake_worker(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
    return WorkerResult(
        task_id=str(task["id"]),
        role=str(task.get("role", "explore")),
        worker_id=f"{task.get('role', 'explore')}:{task['id']}",
        goal=str(task.get("goal", "")),
        status="success",
        summary="完成",
        files=["app/main.py"],
        input_tokens=10,
        output_tokens=2,
        duration_ms=1,
    )


def _settings(**overrides: Any) -> Settings:
    base = {
        "orchestrator_enabled": True,
        "orchestrator_subgraph_enabled": True,
        "orchestrator_min_files": 30,
        "orchestrator_max_tasks": 6,
        "orchestrator_max_concurrent_workers": 2,
        "orchestrator_max_rounds": 3,
        # 默认测等价迁移的旧子图；事件驱动用例显式覆盖。
        "orchestrator_event_driven": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def _run_subgraph(subgraph: Any) -> tuple[dict[str, Any], list[Any], list[str]]:
    states: dict[str, Any] = {}
    seen: list[Any] = []
    names: list[str] = []
    async for chunk in subgraph.astream(
        {
            "request": "同时改两个模块",
            "cwd": "/mnt/d/proj",
            "thread_id": "t-sub",
            "repo_profile": {"file_count": 50, "test_file_count": 3},
            "execution_mode": "orchestrated",
        },
        {"configurable": {"thread_id": "t-sub"}},
        stream_mode=["updates", "custom"],
        subgraphs=True,
    ):
        _namespace, mode, data = chunk
        if mode == "custom":
            seen.append(data)
        elif mode == "updates":
            names.extend(data)
            for value in data.values():
                if isinstance(value, dict):
                    states.update(value)
    return states, seen, names


def test_subgraph_runs_inner_nodes_and_emits_worker_events() -> None:
    subgraph = build_orchestrator_subgraph(
        llm=_TaskGraphLLM(),  # type: ignore[arg-type]
        settings=_settings(),
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        root="/mnt/d/proj",
        worker_runner=_fake_worker,
        steering=SteeringController(),
    )

    states, events, names = asyncio.run(_run_subgraph(subgraph))

    assert "worker_context" in states
    assert len(states["worker_results"]) == 2
    assert {"survey", "select_workflow", "plan_tasks", "dispatch"} <= set(names)
    assert any(event.get("type") == "worker_started" for event in events)
    assert any(event.get("type") == "worker_finished" for event in events)


def test_event_driven_subgraph_uses_scheduler_and_gate() -> None:
    subgraph = build_orchestrator_subgraph(
        llm=_TaskGraphLLM(),  # type: ignore[arg-type]
        settings=_settings(orchestrator_event_driven=True),
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        root="/mnt/d/proj",
        worker_runner=_fake_worker,
        steering=SteeringController(),
    )

    states, events, names = asyncio.run(_run_subgraph(subgraph))

    assert {"scheduler", "steering_dispatch", "synthesize"} <= set(names)
    assert "dispatch" not in names
    assert len(states["worker_results"]) == 2
    assert any(event.get("type") == "worker_started" for event in events)
    assert any(event.get("type") == "worker_finished" for event in events)
