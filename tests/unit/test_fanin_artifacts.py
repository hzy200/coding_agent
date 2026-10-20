"""fan-in 的排序、预算与 artifact store。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.config import Settings
from coding_agent.orchestrator import artifacts as artifacts_module
from coding_agent.orchestrator.artifacts import ArtifactStore
from coding_agent.orchestrator.fanin import make_fan_in_node
from coding_agent.orchestrator.nodes import make_synthesize_node


class _FakeFs:
    def __init__(self, sandbox: Any, root: str) -> None:
        self.root = root
        self.writes: dict[str, str] = {}

    def write_text(self, path: str, content: str) -> int:
        self.writes[path] = content
        return len(content)


def _settings(**overrides: Any) -> Settings:
    base = {"orchestrator_context_chars": 500}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _result(
    task_id: str,
    *,
    status: str,
    summary: str = "",
    priority: int = 0,
    depth: int = 0,
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "role": "explore",
        "worker_id": f"explore:{task_id}",
        "goal": task_id,
        "status": status,
        "summary": summary or status,
        "files": [],
        "findings": [],
        "attempt": attempt,
        "priority": priority,
        "depth": depth,
    }


def test_failed_tasks_are_placed_before_successes() -> None:
    node = make_synthesize_node(_settings())
    out = node(
        {
            "tasks": [],
            "task_status": {"bad": "failed", "good": "success"},
            "worker_results": [
                _result("good", status="success", summary="一切正常"),
                _result("bad", status="failed", summary="关键失败"),
            ],
        },
        None,
    )
    context = out["worker_context"]
    assert context.index("bad") < context.index("good")


def test_per_task_budget_truncates_but_keeps_every_task() -> None:
    node = make_synthesize_node(_settings(orchestrator_context_chars=600))
    results = [
        _result(f"t{i}", status="success", summary="x" * 1_000)
        for i in range(3)
    ]
    out = node(
        {
            "tasks": [],
            "task_status": {f"t{i}": "success" for i in range(3)},
            "worker_results": results,
        },
        None,
    )
    assert all(f"t{i}" in out["worker_context"] for i in range(3))
    assert "任务摘要已截断" in out["worker_context"]


def test_artifact_store_writes_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeFs(object(), "/ws")
    monkeypatch.setattr(
        artifacts_module, "SandboxFs", lambda sandbox, root: fake
    )
    store = ArtifactStore(object(), "/ws", "run-1")  # type: ignore[arg-type]
    rel = store.save_result({"task_id": "t1", "attempt": 2, "status": "success"})

    assert rel.startswith(".agent/artifacts/")
    assert "t1" in rel
    assert "attempt-2" in rel
    path = next(iter(fake.writes))
    payload = json.loads(fake.writes[path])
    assert payload["task_id"] == "t1"


def test_artifact_store_truncation_keeps_valid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeFs(object(), "/ws")
    monkeypatch.setattr(
        artifacts_module, "SandboxFs", lambda sandbox, root: fake
    )
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_CHARS", 400)
    store = ArtifactStore(object(), "/ws", "run-1")  # type: ignore[arg-type]

    rel = store.save_result(
        {
            "task_id": "t1",
            "attempt": 1,
            "status": "success",
            "summary": "x" * 5_000,
            "tool_calls": [{"name": "file_read", "args": {"path": "a"}}] * 100,
        }
    )

    assert rel
    payload = json.loads(next(iter(fake.writes.values())))
    assert payload["truncated"] is True
    assert payload["result"]["task_id"] == "t1"


class _FakeReducer:
    def __init__(self, *, text: str = "压缩结果", error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls = 0

    async def ainvoke(self, messages: Any, config: Any = None) -> AIMessage:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.text)


def _failed_state() -> dict[str, Any]:
    return {
        "tasks": [
            {"id": "t1", "depends_on": []},
            {"id": "t2", "depends_on": []},
        ],
        "task_status": {"t1": "failed", "t2": "failed"},
        "worker_results": [
            _result("t1", status="failed", summary="x" * 300),
            _result("t2", status="failed", summary="y" * 300),
        ],
    }


def test_fan_in_reducer_is_used_when_there_are_failures() -> None:
    reducer = _FakeReducer()
    node = make_fan_in_node(
        _settings(
            orchestrator_context_chars=500,
            orchestrator_fanin_reducer=True,
        ),
        llm=reducer,  # type: ignore[arg-type]
    )

    out = asyncio.run(node(_failed_state(), None))

    assert reducer.calls == 1
    assert out["worker_context"] == "压缩结果"
    assert out["worker_brief"] == "压缩结果"
    assert "已压缩" in out["orchestrator_summary"]


def test_fan_in_reducer_failure_falls_back_to_deterministic_output() -> None:
    reducer = _FakeReducer(error=RuntimeError("boom"))
    node = make_fan_in_node(
        _settings(
            orchestrator_context_chars=500,
            orchestrator_fanin_reducer=True,
        ),
        llm=reducer,  # type: ignore[arg-type]
    )

    out = asyncio.run(node(_failed_state(), None))

    assert reducer.calls == 1
    assert "t1" in out["worker_context"]
    assert out["worker_context"] != "压缩结果"
