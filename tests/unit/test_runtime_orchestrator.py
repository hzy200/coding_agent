"""runtime 对 orchestrator 事件的翻译与开关关闭时的回归。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent.agents.policy import RepoProfile
from coding_agent.audit import AuditLogger, AuditRecord, read_records
from coding_agent.audit import models as audit_models
from coding_agent.config import Settings
from coding_agent.events import (
    CostReport,
    OrchestrationFinished,
    RunFailed,
    RunFinished,
    TaskGraphCreated,
    TaskGraphExtended,
    WorkerFinished,
    WorkerStarted,
    WorkflowSelected,
)
from coding_agent.graph.build import estimate_recursion_limit
from coding_agent.llm.ratelimit import GLOBAL_LIMITER
from coding_agent.runtime import AgentRuntime
from coding_agent.sandbox.snapshots import RestoreResult, SnapshotEntry

THREAD = "t-orch"


class _NullMemory:
    def load(self) -> list[str]:
        return []

    def search(self, query: str = "", limit: int = 8) -> list[str]:
        return []


class FakeGraph:
    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = script
        self.payloads: list[Any] = []

    async def astream(self, payload, config=None, stream_mode=None, subgraphs=None):  # noqa: ANN001
        self.payloads.append(payload)
        for mode, data in self.script:
            yield mode, data


def _updates(**kwargs: Any) -> tuple[str, dict]:
    return ("updates", kwargs)


def _runtime(tmp_path, **overrides) -> AgentRuntime:
    settings = Settings(_env_file=None, audit_dir=str(tmp_path), **overrides)
    runtime = AgentRuntime(
        settings, workspace="/mnt/d/proj", audit=AuditLogger(tmp_path)
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    return runtime


def _worker_result() -> dict[str, Any]:
    return {
        "task_id": "t1",
        "role": "explore",
        "worker_id": "explore:t1",
        "goal": "定位实现",
        "status": "success",
        "summary": "入口在 main.py",
        "files": ["main.py"],
        "findings": [],
        "verification": {},
        "input_tokens": 100,
        "output_tokens": 10,
        "duration_ms": 50,
    }


def _collect(runtime: AgentRuntime) -> list[Any]:
    return asyncio.run(_collect_async(runtime))


async def _collect_async(runtime: AgentRuntime) -> list[Any]:
    return [event async for event in runtime.run("改功能", thread_id=THREAD)]


def test_orchestrator_events_are_translated_and_audited(tmp_path) -> None:
    runtime = _runtime(tmp_path, orchestrator_enabled=True, orchestrator_min_files=30)
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    graph = FakeGraph(
        [
            _updates(
                select_workflow={
                    "workflow": "parallel_readonly",
                    "workflow_reason": "多交付物",
                }
            ),
            _updates(
                plan_tasks={
                    "tasks": [
                        {
                            "id": "t1",
                            "role": "explore",
                            "goal": "定位实现",
                            "depends_on": [],
                        }
                    ],
                    "orchestrator_degraded": False,
                }
            ),
            _updates(
                scheduler={
                    "worker_results": [_worker_result()],
                    "task_status": {"t1": "success"},
                    "orchestrator_round": 1,
                }
            ),
            _updates(
                synthesize={
                    "orchestrator_completed": 1,
                    "orchestrator_failed": 0,
                    "orchestrator_summary": "成功 1，失败/阻塞 0",
                    "task_status": {"t1": "success"},
                }
            ),
            _updates(respond={"messages": [AIMessage(content="完成")]}),
        ]
    )
    runtime._graph = graph

    events = _collect(runtime)

    assert any(isinstance(e, WorkflowSelected) for e in events)
    assert any(isinstance(e, TaskGraphCreated) for e in events)
    assert any(isinstance(e, WorkerStarted) for e in events)
    assert any(isinstance(e, WorkerFinished) for e in events)
    assert any(isinstance(e, OrchestrationFinished) for e in events)
    assert isinstance(events[-1], RunFinished)

    kinds = [r.kind for r in read_records(runtime.audit_path)]
    assert {"workflow", "task_graph", "worker", "orchestration"} <= set(kinds)


def test_worker_tool_calls_and_merge_are_audited(tmp_path) -> None:
    runtime = _runtime(tmp_path, orchestrator_enabled=True, orchestrator_min_files=30)
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    result = _worker_result()
    result.update(
        {
            "role": "implement",
            "worker_id": "implement:t1",
            "status": "success",
            "merged": True,
            "tool_calls": [
                {
                    "call_id": "child-1",
                    "name": "file_edit",
                    "args": {"path": "app/a.py", "reason": "改"},
                    "ok": True,
                    "duration_ms": 3,
                }
            ],
        }
    )
    runtime._graph = FakeGraph(
        [
            _updates(
                select_workflow={
                    "workflow": "orchestrated_implement",
                    "workflow_reason": "多交付物",
                }
            ),
            _updates(
                plan_tasks={
                    "tasks": [
                        {
                            "id": "t1",
                            "role": "implement",
                            "goal": "改",
                            "write_scope": ["app/"],
                        }
                    ],
                    "orchestrator_degraded": False,
                }
            ),
            _updates(
                scheduler={
                    "worker_results": [result],
                    "task_status": {"t1": "success"},
                    "orchestrator_round": 1,
                }
            ),
            _updates(respond={"messages": [AIMessage(content="完成")]}),
        ]
    )

    events = _collect(runtime)
    finished = next(e for e in events if isinstance(e, WorkerFinished))
    assert finished.tool_calls == 1
    assert finished.merged is True

    records = read_records(runtime.audit_path)
    child = next(
        r for r in records if r.kind == "tool_call" and r.agent_id == "implement:t1"
    )
    assert child.task_id == "t1"
    assert child.tool == "file_edit"
    assert child.level == "L1 低风险写"
    assert child.args["path"] == "app/a.py"


def test_run_inputs_carry_orchestrator_mode_and_reset_state(tmp_path) -> None:
    runtime = _runtime(tmp_path, orchestrator_enabled=True, orchestrator_min_files=30)
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._graph = graph

    _collect(runtime)

    payload = graph.payloads[0]
    assert payload["execution_mode"] == "orchestrated"
    assert payload["tasks"] == []
    assert payload["worker_results"] == []
    assert payload["worker_context"] == ""


class _FakeCheckpointSaver:
    def __init__(
        self,
        messages: list[Any] | None = None,
        *,
        values: dict[str, Any] | None = None,
    ) -> None:
        self._values = (
            dict(values) if values is not None else {"messages": messages or []}
        )
        self.calls = 0

    async def aget_tuple(self, config):  # noqa: ANN001
        self.calls += 1
        return SimpleNamespace(checkpoint={"channel_values": self._values})


def test_session_brief_carries_prior_constraints_into_the_payload(tmp_path) -> None:
    saver = _FakeCheckpointSaver(
        [
            HumanMessage(content="不要改测试文件，沿用现有接口"),
            AIMessage(content="明白。"),
        ]
    )
    settings = Settings(
        _env_file=None,
        audit_dir=str(tmp_path),
        orchestrator_enabled=True,
        orchestrator_min_files=30,
    )
    runtime = AgentRuntime(
        settings,
        workspace="/mnt/d/proj",
        checkpointer=saver,
        audit=AuditLogger(tmp_path),
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._graph = graph

    _collect(runtime)

    brief = graph.payloads[0]["session_brief"]
    assert "不要改测试文件，沿用现有接口" in brief
    # 本轮 prompt 尚未写入 checkpoint，不能重复出现在摘要里。
    assert "改功能" not in brief
    assert saver.calls == 1


def test_session_brief_can_be_disabled(tmp_path) -> None:
    saver = _FakeCheckpointSaver([HumanMessage(content="旧约束")])
    settings = Settings(
        _env_file=None,
        audit_dir=str(tmp_path),
        orchestrator_enabled=True,
        orchestrator_min_files=30,
        session_brief_chars=0,
    )
    runtime = AgentRuntime(
        settings,
        workspace="/mnt/d/proj",
        checkpointer=saver,
        audit=AuditLogger(tmp_path),
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._graph = graph

    _collect(runtime)

    assert graph.payloads[0]["session_brief"] == ""
    # 摘要关闭后仍会读一次 checkpoint：悬空 tool_calls 的修复不能一起关掉。
    assert saver.calls == 1


def test_dangling_tool_calls_are_repaired_before_the_new_prompt(tmp_path) -> None:
    saver = _FakeCheckpointSaver(
        [
            HumanMessage(content="改文件"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "file_write",
                        "args": {"path": "a.py"},
                        "id": "call-1",
                    }
                ],
            ),
        ]
    )
    settings = Settings(
        _env_file=None,
        audit_dir=str(tmp_path),
        orchestrator_enabled=False,
    )
    runtime = AgentRuntime(
        settings,
        workspace="/mnt/d/proj",
        checkpointer=saver,
        audit=AuditLogger(tmp_path),
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._graph = graph

    _collect(runtime)

    messages = graph.payloads[0]["messages"]
    assert isinstance(messages[0], ToolMessage)
    assert messages[0].tool_call_id == "call-1"
    assert messages[0].artifact["interrupted"] is True
    assert isinstance(messages[-1], HumanMessage)


def test_interrupted_run_produces_a_recovery_notice(tmp_path) -> None:
    saver = _FakeCheckpointSaver(
        values={
            "messages": [HumanMessage(content="上次的请求")],
            "run_status": "running",
            "run_dirty": True,
            "stale_paths": ["app/a.py"],
            "last_verification": {"status": "failed"},
        }
    )
    runtime = AgentRuntime(
        Settings(_env_file=None, audit_dir=str(tmp_path), orchestrator_enabled=False),
        workspace="/mnt/d/proj",
        checkpointer=saver,
        audit=AuditLogger(tmp_path),
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._graph = graph

    _collect(runtime)

    notice = graph.payloads[0]["recovery_notice"]
    assert "上一次运行没有正常收尾" in notice
    assert "failed" in notice
    assert "app/a.py" in notice
    kinds = [record.kind for record in read_records(runtime.audit_path)]
    assert "recovery" in kinds


def test_finished_run_does_not_produce_a_recovery_notice(tmp_path) -> None:
    saver = _FakeCheckpointSaver(
        values={
            "messages": [HumanMessage(content="上次的请求")],
            "run_status": "finished",
        }
    )
    runtime = AgentRuntime(
        Settings(_env_file=None, audit_dir=str(tmp_path), orchestrator_enabled=False),
        workspace="/mnt/d/proj",
        checkpointer=saver,
        audit=AuditLogger(tmp_path),
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._graph = graph

    _collect(runtime)

    assert graph.payloads[0]["recovery_notice"] == ""


def test_workspace_lock_blocks_a_second_runtime(tmp_path) -> None:
    first = _runtime(tmp_path)
    second = _runtime(tmp_path)

    assert first._acquire_workspace_lock() is True
    try:
        assert second._acquire_workspace_lock() is False
    finally:
        first._release_workspace_lock()

    assert second._acquire_workspace_lock() is True
    second._release_workspace_lock()


def test_snapshot_prefix_is_comparable_with_snapshot_ids() -> None:
    assert (
        AgentRuntime._snapshot_prefix("2026-01-01T00:00:00+00:00")
        == "20260101T000000000000"
    )
    assert AgentRuntime._snapshot_prefix("not-a-time") == ""


def test_last_run_started_at_reads_the_audit(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime.audit.write(
        AuditRecord(
            ts="2026-01-01T00:00:00+00:00",
            kind=audit_models.RUN_START,
            thread_id=THREAD,
            workspace=runtime.workspace,
            detail="第一次",
        )
    )
    runtime.audit.write(
        AuditRecord(
            ts="2026-01-02T00:00:00+00:00",
            kind=audit_models.RUN_START,
            thread_id=THREAD,
            workspace=runtime.workspace,
            detail="第二次",
        )
    )

    assert (
        runtime.last_run_started_at(THREAD) == "2026-01-02T00:00:00+00:00"
    )


class _FakeSnapshots:
    def __init__(self) -> None:
        self.entries = [
            SnapshotEntry("20260102T000000000000-aaa", "new.py"),
            SnapshotEntry("20251231T000000000000-bbb", "old.py"),
        ]
        self.restored: list[str] = []

    def list(self):  # noqa: ANN201
        return list(self.entries)

    def restore(self, entry: SnapshotEntry) -> RestoreResult:
        self.restored.append(entry.path)
        return RestoreResult(
            ok=True, path=entry.path, snapshot_id=entry.snapshot_id
        )


def test_restore_since_only_touches_snapshots_after_the_prefix(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    fake = _FakeSnapshots()
    runtime._snapshots = fake  # type: ignore[assignment]

    results = runtime.restore_since("20260101T000000000000")

    assert [item.path for item in results] == ["new.py"]
    assert fake.restored == ["new.py"]


def test_restore_since_dry_run_does_not_write(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    fake = _FakeSnapshots()
    runtime._snapshots = fake  # type: ignore[assignment]

    results = runtime.restore_since(
        "20260101T000000000000", dry_run=True
    )

    assert results[0].message.startswith("dry-run")
    assert fake.restored == []


class _SlowGraph:
    async def astream(self, *args, **kwargs):  # noqa: ANN002, ANN003
        await asyncio.sleep(5)
        yield ("updates", {})


def test_run_deadline_stops_a_stuck_graph(tmp_path) -> None:
    runtime = _runtime(
        tmp_path, orchestrator_enabled=False, run_deadline_s=1
    )
    runtime._memory = _NullMemory()  # type: ignore[assignment]
    runtime._graph = _SlowGraph()

    events = _collect(runtime)

    failed = [event for event in events if isinstance(event, RunFailed)]
    assert failed
    assert "总时限" in failed[-1].message


def test_orchestrator_disabled_stays_direct(tmp_path) -> None:
    runtime = _runtime(tmp_path, orchestrator_enabled=False)
    graph = FakeGraph([_updates(respond={"messages": [AIMessage(content="完成")]})])
    runtime._graph = graph

    _collect(runtime)

    assert graph.payloads[0]["execution_mode"] == "direct"


def test_task_graph_extended_is_translated_and_audited(tmp_path) -> None:
    runtime = _runtime(
        tmp_path, orchestrator_enabled=True, orchestrator_min_files=30
    )
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    runtime._graph = FakeGraph(
        [
            _updates(
                scheduler={
                    "task_extensions_applied": [
                        {
                            "id": "ext-1",
                            "role": "review",
                            "goal": "检查边界",
                        }
                    ],
                    "task_extension_errors": ["重复任务提议已忽略：定位"],
                }
            ),
            _updates(respond={"messages": [AIMessage(content="完成")]}),
        ]
    )

    events = _collect(runtime)

    extended = [
        event for event in events if isinstance(event, TaskGraphExtended)
    ]
    assert extended
    assert extended[0].added[0]["id"] == "ext-1"
    assert extended[0].rejected
    kinds = [record.kind for record in read_records(runtime.audit_path)]
    assert "task_graph" in kinds


def test_custom_worker_events_are_not_duplicated_by_the_dispatch_update(
    tmp_path,
) -> None:
    """custom 流实时发过的事件，updates 分支不能再来一遍。"""
    result = {
        "task_id": "t1",
        "role": "explore",
        "worker_id": "explore:t1",
        "goal": "定位实现",
        "status": "success",
        "summary": "入口在 main.py",
        "input_tokens": 100,
        "output_tokens": 10,
        "duration_ms": 50,
        "attempt": 1,
    }
    runtime = _runtime(
        tmp_path, orchestrator_enabled=True, orchestrator_min_files=30
    )
    runtime._repo_profile_cache = RepoProfile(file_count=50, test_file_count=5)
    runtime._graph = FakeGraph(
        [
            (
                "custom",
                {
                    "type": "worker_started",
                    "worker_id": "explore:t1",
                    "task_id": "t1",
                    "role": "explore",
                    "goal": "定位实现",
                    "attempt": 1,
                    "queue_wait_ms": 37,
                },
            ),
            ("custom", {"type": "worker_finished", "result": result}),
            _updates(
                scheduler={
                    "worker_results": [result],
                    "task_status": {"t1": "success"},
                    "orchestrator_round": 1,
                }
            ),
            _updates(respond={"messages": [AIMessage(content="完成")]}),
        ]
    )

    events = _collect(runtime)

    started = [event for event in events if isinstance(event, WorkerStarted)]
    assert len(started) == 1
    assert started[0].queue_wait_ms == 37
    assert len([event for event in events if isinstance(event, WorkerFinished)]) == 1
    assert isinstance(events[-1], RunFinished)
    # 主 agent 的 token 与 worker token 分列，后者不会漏记
    assert events[-1].worker_input_tokens == 100
    assert events[-1].worker_output_tokens == 10


def test_estimate_recursion_limit_includes_the_orchestrator_path() -> None:
    base = estimate_recursion_limit(5, 12, 3, 2)
    orch = estimate_recursion_limit(
        5,
        12,
        3,
        2,
        orchestrator_enabled=True,
        orchestrator_max_tasks=6,
        orchestrator_checkpoint_every=4,
    )
    # 6 个任务 / 每边界 4 个 -> 2 个边界；每个边界 scheduler + gate。
    boundaries = 2
    assert orch == base + (2 + 1) * (6 + boundaries * 2) + 2


def test_cost_report_includes_billing_reconciliation(
    monkeypatch, tmp_path
) -> None:
    import coding_agent.runtime as runtime_module
    from coding_agent.llm.billing import BillingResult

    monkeypatch.setattr(
        runtime_module,
        "fetch_billing",
        lambda settings, start, end: [
            BillingResult(provider="provider-a", cost_usd=1.2)
        ],
    )
    runtime = _runtime(
        tmp_path,
        billing_enabled=True,
        rate_limit_state_path=":memory:",
    )
    GLOBAL_LIMITER.reset()
    GLOBAL_LIMITER.record_usage(
        "provider-a",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.5,
        run_id=THREAD,
    )
    runtime._graph = FakeGraph(
        [_updates(respond={"messages": [AIMessage(content="完成")]})]
    )

    events = _collect(runtime)

    report = next(event for event in events if isinstance(event, CostReport))
    assert report.billed_usd == 1.2
    assert report.billing_delta_usd == pytest.approx(0.7)
    assert report.billing_providers["provider-a"]["cost_usd"] == 1.2
    assert "provider 账单金额明显高于本地估算" in report.alerts


def test_run_emits_cost_report_and_budget_alert(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        run_cost_budget_usd=1.0,
        cost_alert_ratio=0.8,
        rate_limit_state_path=":memory:",
    )
    GLOBAL_LIMITER.reset()
    GLOBAL_LIMITER.record_usage(
        "provider-a",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.9,
        run_id=THREAD,
    )
    runtime._graph = FakeGraph(
        [_updates(respond={"messages": [AIMessage(content="完成")]})]
    )

    events = _collect(runtime)

    report = next(event for event in events if isinstance(event, CostReport))
    assert report.run_id == THREAD
    assert report.input_tokens == 100
    assert report.output_tokens == 50
    assert report.cost_usd == 0.9
    assert report.budget_usd == 1.0
    assert report.alerts
    assert "provider-a" in report.providers
