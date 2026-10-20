"""写计划审批：并发前一次性确认 write_scope。"""

from __future__ import annotations

from typing import Any

from coding_agent.config import Settings
from coding_agent.orchestrator import write_gate as write_gate_module
from coding_agent.orchestrator.write_gate import make_write_gate_node


def _settings(**overrides: Any) -> Settings:
    base = {"orchestrator_write_plan_approval": True}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _state() -> dict[str, Any]:
    return {
        "workflow": "orchestrated_implement",
        "tasks": [
            {
                "id": "t1",
                "role": "implement",
                "goal": "改 a",
                "write_scope": ["app/a.py"],
            },
            {
                "id": "t2",
                "role": "implement",
                "goal": "改 b",
                "write_scope": ["app/b.py"],
            },
        ],
        "task_status": {"t1": "pending", "t2": "pending"},
    }


def test_disabled_is_a_noop(monkeypatch) -> None:
    called = False

    def _interrupt(payload: Any) -> dict[str, bool]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(write_gate_module, "interrupt", _interrupt)
    node = make_write_gate_node(
        _settings(orchestrator_write_plan_approval=False)
    )

    assert node(_state(), None) == {}
    assert called is False


def test_approved_scopes_leave_tasks_pending(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []

    def _interrupt(payload: dict[str, Any]) -> dict[str, bool]:
        seen.extend(payload["requests"])
        return {"plan:t1": True, "plan:t2": True}

    monkeypatch.setattr(write_gate_module, "interrupt", _interrupt)
    out = make_write_gate_node(_settings())(_state(), None)

    assert [item["call_id"] for item in seen] == ["plan:t1", "plan:t2"]
    assert out == {"task_status": {"t1": "pending", "t2": "pending"}}


def test_denied_scope_is_cancelled_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        write_gate_module,
        "interrupt",
        lambda payload: {"plan:t1": True},
    )

    out = make_write_gate_node(_settings())(_state(), None)

    assert out["task_status"]["t1"] == "pending"
    assert out["task_status"]["t2"] == "cancelled"
