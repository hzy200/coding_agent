"""多补丁原子合并：任一失败则整批不落地。"""

from __future__ import annotations

import asyncio
from typing import Any

from coding_agent.orchestrator import merge as merge_module
from coding_agent.orchestrator.merge import merge_implement_patches_atomic
from coding_agent.sandbox.wsl_exec import ExecResult


class _FakeSandbox:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(
        self, command: str, *, cwd: str | None = None, timeout: int | None = None
    ) -> ExecResult:
        self.commands.append(command)
        ok = True
        if command.startswith("git apply /") and "patches/t2" in command:
            ok = False
        return ExecResult(
            command=command,
            exit_code=0 if ok else 1,
            stdout="",
            stderr="" if ok else "conflict",
            duration_ms=0,
        )


def _record(task_id: str, patch: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "role": "implement",
        "status": "success",
        "patch_path": f"/ws/.agent/patches/{patch}",
        "files": [f"app/{task_id}.py"],
    }


def test_second_patch_failure_rolls_back_the_first(monkeypatch) -> None:
    sandbox = _FakeSandbox()
    rolled_back: list[list[str]] = []

    monkeypatch.setattr(
        merge_module,
        "_patch_paths",
        lambda sandbox, patch, root: (
            [f"app/{patch.rsplit('/', 1)[-1].split('.')[0]}.py"],
            "",
        ),
    )
    monkeypatch.setattr(
        merge_module,
        "_snapshot_paths",
        lambda sandbox, root, paths: ([f"snap-{len(paths)}"], ""),
    )
    monkeypatch.setattr(
        merge_module,
        "_rollback_merged",
        lambda sandbox, root, applied, paths: rolled_back.append(
            [str(item["task_id"]) for item in applied]
        ),
    )

    outcomes = asyncio.run(
        merge_implement_patches_atomic(
            sandbox,  # type: ignore[arg-type]
            "/ws",
            [_record("t1", "t1.patch"), _record("t2", "t2.patch")],
        )
    )

    assert outcomes["t1"][0] is False
    assert "回滚" in outcomes["t1"][1]
    assert outcomes["t2"][0] is False
    assert outcomes["t2"][1]
    assert rolled_back == [["t1"]]


def test_overlapping_patches_use_three_way_validation(monkeypatch) -> None:
    sandbox = _FakeSandbox()
    monkeypatch.setattr(
        merge_module,
        "_patch_paths",
        lambda sandbox, patch, root: (["app/shared.py"], ""),
    )
    monkeypatch.setattr(
        merge_module,
        "_snapshot_paths",
        lambda sandbox, root, paths: (["snap"], ""),
    )

    outcomes = asyncio.run(
        merge_implement_patches_atomic(
            sandbox,  # type: ignore[arg-type]
            "/ws",
            [_record("t1", "t1.patch"), _record("t2", "t2.patch")],
            allow_overlap=True,
        )
    )

    assert outcomes["t1"][0] is True
    assert outcomes["t2"][0] is True
    assert any("--3way --cached" in command for command in sandbox.commands)
    assert any(
        command.startswith("git apply --3way")
        for command in sandbox.commands
    )
    assert "git reset -q" in sandbox.commands


def test_overlapping_patches_are_rejected_without_the_flag(monkeypatch) -> None:
    sandbox = _FakeSandbox()
    monkeypatch.setattr(
        merge_module,
        "_patch_paths",
        lambda sandbox, patch, root: (["app/shared.py"], ""),
    )

    outcomes = asyncio.run(
        merge_implement_patches_atomic(
            sandbox,  # type: ignore[arg-type]
            "/ws",
            [_record("t1", "t1.patch"), _record("t2", "t2.patch")],
        )
    )

    assert outcomes["t1"][0] is False
    assert outcomes["t2"][0] is False
    assert "3-way" in outcomes["t1"][1]
    assert not any(
        command.startswith("git apply /") for command in sandbox.commands
    )


def test_all_patches_merge_when_checks_pass(monkeypatch) -> None:
    sandbox = _FakeSandbox()
    monkeypatch.setattr(
        merge_module,
        "_patch_paths",
        lambda sandbox, patch, root: (["app/x.py"], ""),
    )
    monkeypatch.setattr(
        merge_module,
        "_snapshot_paths",
        lambda sandbox, root, paths: (["snap"], ""),
    )
    monkeypatch.setattr(
        merge_module,
        "_rollback_merged",
        lambda *args: None,
    )

    outcomes = asyncio.run(
        merge_implement_patches_atomic(
            sandbox,  # type: ignore[arg-type]
            "/ws",
            [_record("t1", "t1.patch")],
        )
    )

    assert outcomes["t1"][0] is True
    assert any("git apply /ws" in command for command in sandbox.commands)
