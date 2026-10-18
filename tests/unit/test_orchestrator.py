"""Phase 3A orchestrator：任务图校验、工作流选择、并发 dispatch、fan-in。

用假 LLM / 假 sandbox / 假 worker runner 驱动，不碰 WSL、不碰真实模型。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from langchain_core.messages import HumanMessage

from coding_agent.config import Settings
from coding_agent.orchestrator import nodes as orchestrator_nodes
from coding_agent.orchestrator.contracts import (
    TaskGraph,
    TaskSpec,
    WorkerResult,
    WorkflowDecision,
)
from coding_agent.orchestrator.graph import (
    mark_blocked,
    ready_tasks,
    validate_proposals,
    validate_tasks,
)
from coding_agent.orchestrator.implement import _safe_id
from coding_agent.orchestrator.nodes import (
    _patch_paths,
    make_dispatch_node,
    make_extend_tasks_node,
    make_plan_tasks_node,
    make_route_after_dispatch,
    make_survey_node,
    make_synthesize_node,
    route_after_select,
    route_entry,
)
from coding_agent.orchestrator.workers import make_worker_runner
from coding_agent.orchestrator.workflow import (
    make_select_workflow_node,
    rule_workflow,
)
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.artifacts import SubAgentArtifact


def _settings(**overrides) -> Settings:
    values = {
        "orchestrator_enabled": True,
        "orchestrator_min_files": 30,
        "orchestrator_max_tasks": 6,
        "orchestrator_max_concurrent_workers": 4,
        "orchestrator_max_rounds": 3,
        "orchestrator_context_chars": 2_000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ---------------- 任务图校验 ----------------


def test_valid_task_graph_is_preserved() -> None:
    tasks, degraded, errors = validate_tasks(
        [
            TaskSpec(id="t1", role="explore", goal="定位实现"),
            TaskSpec(id="t2", role="test", goal="跑测试", depends_on=["t1"]),
            TaskSpec(id="t3", role="review", goal="审查风险", depends_on=["t1"]),
        ],
        request="改功能",
        max_tasks=6,
    )
    assert degraded is False and errors == []
    assert [t["id"] for t in tasks] == ["t1", "t2", "t3"]


def test_empty_or_invalid_graph_degrades_to_one_explore() -> None:
    tasks, degraded, errors = validate_tasks([], request="改功能", max_tasks=6)
    assert degraded is True and errors
    assert tasks[0]["role"] == "explore"
    assert tasks[0]["goal"] == "改功能"


def test_duplicate_ids_are_rejected() -> None:
    tasks, degraded, _ = validate_tasks(
        [
            TaskSpec(id="t1", role="explore", goal="a"),
            TaskSpec(id="t1", role="explore", goal="b"),
        ],
        request="r",
        max_tasks=6,
    )
    assert degraded is True
    assert len(tasks) == 1


def test_missing_dependency_is_rejected() -> None:
    _, degraded, errors = validate_tasks(
        [TaskSpec(id="t1", role="explore", goal="a", depends_on=["nope"])],
        request="r",
        max_tasks=6,
    )
    assert degraded is True
    assert any("不存在" in error for error in errors)


def test_cycle_is_rejected() -> None:
    _, degraded, errors = validate_tasks(
        [
            TaskSpec(id="t1", role="explore", goal="a", depends_on=["t2"]),
            TaskSpec(id="t2", role="explore", goal="b", depends_on=["t1"]),
        ],
        request="r",
        max_tasks=6,
    )
    assert degraded is True
    assert any("环" in error for error in errors)


def test_readonly_role_with_write_scope_is_rejected() -> None:
    _, degraded, errors = validate_tasks(
        [
            {
                "id": "t1",
                "role": "explore",
                "goal": "a",
                "write_scope": ["app/"],
            }
        ],
        request="r",
        max_tasks=6,
    )
    assert degraded is True
    assert any("write_scope" in error for error in errors)


def test_max_tasks_is_enforced() -> None:
    """超限不再静默截断：按“结构问题整图降级”的既有约定处理。"""
    tasks, degraded, errors = validate_tasks(
        [TaskSpec(id=f"t{i}", role="explore", goal=str(i)) for i in range(10)],
        request="r",
        max_tasks=3,
    )
    assert degraded is True
    assert any("超过上限" in error for error in errors)
    assert len(tasks) == 1
    assert tasks[0]["id"] == "explore-1"


def test_implement_requires_write_mode_and_scope() -> None:
    _, degraded, errors = validate_tasks(
        [TaskSpec(id="t1", role="implement", goal="改文件")],
        request="r",
        max_tasks=6,
    )
    assert degraded is True
    assert any("未启用写编排" in error or "write_scope" in error for error in errors)

    tasks, degraded, errors = validate_tasks(
        [
            TaskSpec(
                id="t1",
                role="implement",
                goal="改文件",
                write_scope=["app/a.py"],
            )
        ],
        request="r",
        max_tasks=6,
        allow_write=True,
    )
    assert degraded is False and errors == []
    assert tasks[0]["write_scope"] == ["app/a.py"]


def test_overlapping_write_scopes_are_rejected() -> None:
    _, degraded, errors = validate_tasks(
        [
            TaskSpec(id="t1", role="implement", goal="a", write_scope=["app/"]),
            TaskSpec(
                id="t2", role="implement", goal="b", write_scope=["app/a.py"]
            ),
        ],
        request="r",
        max_tasks=6,
        allow_write=True,
    )
    assert degraded is True
    assert any("写范围重叠" in error for error in errors)


def test_implement_tasks_cannot_depend_on_each_other() -> None:
    _, degraded, errors = validate_tasks(
        [
            TaskSpec(id="t1", role="implement", goal="a", write_scope=["a/"]),
            TaskSpec(
                id="t2",
                role="implement",
                goal="b",
                write_scope=["b/"],
                depends_on=["t1"],
            ),
        ],
        request="r",
        max_tasks=6,
        allow_write=True,
    )
    assert degraded is True
    assert any("不能互相依赖" in error for error in errors)


def test_readonly_role_cannot_declare_write_scope() -> None:
    _, degraded, errors = validate_tasks(
        [TaskSpec(id="t1", role="review", goal="看", write_scope=["app/"])],
        request="r",
        max_tasks=6,
    )
    assert degraded is True
    assert any("只读角色" in error for error in errors)


def test_validate_proposals_accepts_read_only_and_assigns_ids() -> None:
    existing = [{"id": "t1", "role": "explore", "goal": "定位"}]

    accepted, errors = validate_proposals(
        [
            {
                "role": "review",
                "goal": "检查错误处理",
                "inputs": ["app/a.py"],
                "write_scope": [],
            }
        ],
        existing=existing,
        max_total=6,
    )

    assert errors == []
    assert accepted[0]["id"] == "ext-1"
    assert accepted[0]["role"] == "review"
    assert accepted[0]["write_scope"] == []
    assert accepted[0]["depends_mode"] == "full"


def test_validate_proposals_rejects_unsafe_or_duplicate_items() -> None:
    existing = [{"id": "t1", "role": "explore", "goal": "定位入口"}]

    accepted, errors = validate_proposals(
        [
            {"role": "implement", "goal": "改文件"},
            {"role": "explore", "goal": "定位入口"},
            {"role": "explore", "goal": ""},
            {"role": "review", "goal": "检查", "write_scope": ["app/a.py"]},
            {"role": "review", "goal": "依赖不存在", "depends_on": ["nope"]},
        ],
        existing=existing,
        max_total=6,
    )

    assert accepted == []
    joined = "；".join(errors)
    assert "非只读" in joined
    assert "重复" in joined
    assert "空 goal" in joined
    assert "write_scope" in joined
    assert "不存在的 id" in joined


def test_validate_proposals_enforces_the_total_task_cap() -> None:
    existing = [{"id": "t1", "role": "explore", "goal": "一"}]

    accepted, errors = validate_proposals(
        [
            {"role": "explore", "goal": "二"},
            {"role": "explore", "goal": "三"},
        ],
        existing=existing,
        max_total=2,
    )

    assert len(accepted) == 1
    assert any("总上限" in error for error in errors)


def test_validate_proposals_can_depend_on_an_earlier_proposal() -> None:
    """依赖只能指向已存在的任务或本轮更早被接受的提议，天然排除环。"""
    existing = [{"id": "t1", "role": "explore", "goal": "一"}]

    accepted, errors = validate_proposals(
        [
            {"role": "explore", "goal": "甲"},
            {"role": "explore", "goal": "乙", "depends_on": ["ext-1"]},
        ],
        existing=existing,
        max_total=6,
    )

    assert errors == []
    assert [task["id"] for task in accepted] == ["ext-1", "ext-2"]
    assert accepted[1]["depends_on"] == ["ext-1"]


def test_extend_tasks_adds_valid_proposals_and_skips_failed_workers() -> None:
    node = make_extend_tasks_node(_settings())
    state = {
        "tasks": [{"id": "t1", "role": "explore", "goal": "定位"}],
        "task_status": {"t1": "success"},
        "worker_results": [
            {
                "task_id": "t1",
                "role": "explore",
                "status": "success",
                "proposed_tasks": [
                    {"role": "review", "goal": "检查边界"},
                    {"role": "implement", "goal": "改文件"},
                ],
            },
            {
                "task_id": "t2",
                "role": "explore",
                "status": "failed",
                "proposed_tasks": [{"role": "review", "goal": "不该被采纳"}],
            },
        ],
        "task_extensions_seen": 0,
    }

    out = node(state, None)

    assert [task["id"] for task in out["tasks"]] == ["t1", "ext-1"]
    assert out["task_status"]["ext-1"] == "pending"
    assert out["task_extensions_seen"] == 2
    assert any("非只读" in error for error in out["task_extension_errors"])
    assert all(
        task["goal"] != "不该被采纳" for task in out["tasks"]
    )

    # 没有新的 worker_results：不重复采纳
    state.update(out)
    again = node(state, None)
    assert [task["id"] for task in again["tasks"]] == ["t1", "ext-1"]


def test_ready_and_blocked_propagation() -> None:
    tasks = [
        {"id": "t1", "depends_on": []},
        {"id": "t2", "depends_on": ["t1"]},
        {"id": "t3", "depends_on": ["t2"]},
    ]
    status = {"t1": "failed", "t2": "pending", "t3": "pending"}
    assert ready_tasks(tasks, status) == []
    blocked = mark_blocked(tasks, status)
    assert blocked["t2"] == "blocked"
    assert blocked["t3"] == "blocked"


def test_partial_dependency_also_blocks_dependents() -> None:
    tasks = [
        {"id": "t1", "depends_on": []},
        {"id": "t2", "depends_on": ["t1"]},
    ]
    blocked = mark_blocked(tasks, {"t1": "partial", "t2": "pending"})
    assert blocked["t2"] == "blocked"


def test_partial_dependency_is_consumable_with_any_mode() -> None:
    """review/test 只需要依赖的产出，不该因为对方没通过完整验证就饿死。"""
    tasks = [
        {"id": "t1", "depends_on": []},
        {
            "id": "t2",
            "role": "review",
            "depends_on": ["t1"],
            "depends_mode": "any",
        },
    ]
    status = {"t1": "partial", "t2": "pending"}

    assert ready_tasks(tasks, status) == [tasks[1]]
    assert mark_blocked(tasks, status)["t2"] == "pending"


def test_any_mode_does_not_consume_a_failed_dependency() -> None:
    tasks = [
        {"id": "t1", "depends_on": []},
        {
            "id": "t2",
            "role": "review",
            "depends_on": ["t1"],
            "depends_mode": "any",
        },
    ]
    status = {"t1": "failed", "t2": "pending"}

    assert ready_tasks(tasks, status) == []
    assert mark_blocked(tasks, status)["t2"] == "blocked"


def test_validate_tasks_keeps_depends_mode_and_rejects_it_for_implement() -> None:
    tasks, degraded, errors = validate_tasks(
        [
            TaskSpec(
                id="r1",
                role="review",
                goal="审查",
                depends_mode="any",
            )
        ],
        request="做事",
        max_tasks=6,
    )
    assert degraded is False
    assert tasks[0]["depends_mode"] == "any"

    _, degraded, errors = validate_tasks(
        [
            TaskSpec(
                id="i1",
                role="implement",
                goal="改",
                write_scope=["app/a.py"],
                depends_mode="any",
            )
        ],
        request="做事",
        max_tasks=6,
        allow_write=True,
    )
    assert degraded is True
    assert any("depends_mode 必须是 full" in error for error in errors)


# ---------------- 工作流选择 ----------------


class _Structured:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def with_retry(self, **_kwargs: Any) -> _Structured:
        return self

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class _StructuredLLM:
    def __init__(self, structured: _Structured) -> None:
        self.structured = structured

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> _Structured:
        self.schema = schema
        return self.structured


def _run(coro):
    return asyncio.run(coro)


def test_rule_workflow_small_repo_is_direct() -> None:
    decision = rule_workflow("改一下", {"file_count": 10}, _settings())
    assert decision is not None
    assert decision.workflow == "direct"


def test_rule_workflow_multiple_deliverables_is_parallel() -> None:
    decision = rule_workflow(
        "同时修好折扣和报表，并分别补测试", {"file_count": 50}, _settings()
    )
    assert decision is not None
    assert decision.workflow == "parallel_readonly"


def test_rule_workflow_ambiguous_returns_none() -> None:
    assert rule_workflow("改一下", {"file_count": 50}, _settings()) is None


def test_rule_workflow_write_available_selects_implement_template() -> None:
    decision = rule_workflow(
        "同时改折扣和审计两个模块",
        {"file_count": 50},
        _settings(orchestrator_write_enabled=True),
        write_available=True,
    )
    assert decision is not None
    assert decision.workflow == "orchestrated_implement"


def test_write_workflow_is_downgraded_when_write_is_unavailable() -> None:
    structured = _Structured(
        WorkflowDecision(
            workflow="orchestrated_implement", reason="模型选择写模板"
        )
    )
    node = make_select_workflow_node(
        _StructuredLLM(structured), _settings(orchestrator_write_enabled=True)
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="把这个功能做完")],
                "repo_summary": {"file_count": 50, "git": False},
            },
            None,
        )
    )
    assert out["workflow"] == "parallel_readonly"


def test_select_workflow_uses_llm_when_rules_are_ambiguous() -> None:
    structured = _Structured(
        WorkflowDecision(workflow="parallel_readonly", reason="模型选择")
    )
    node = make_select_workflow_node(_StructuredLLM(structured), _settings())
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="把这个功能做完")],
                "repo_summary": {"file_count": 50},
            },
            None,
        )
    )
    assert out["workflow"] == "parallel_readonly"
    assert out["workflow_reason"] == "模型选择"
    assert structured.calls == 1


def test_select_workflow_falls_back_to_direct_on_llm_error() -> None:
    structured = _Structured(error=RuntimeError("boom"))
    node = make_select_workflow_node(_StructuredLLM(structured), _settings())
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="把这个功能做完")],
                "repo_summary": {"file_count": 50},
            },
            None,
        )
    )
    assert out["workflow"] == "direct"


# ---------------- survey / plan / dispatch / synthesize ----------------


class _SurveySandbox:
    def __init__(
        self,
        settings: Settings,
        *,
        toplevel: str = "/ws",
        physical: str = "/ws",
        dirs: str = "app\ntests\n",
        files: str = "main.py\npyproject.toml\n",
    ) -> None:
        self.settings = settings
        self.commands: list[str] = []
        self.toplevel = toplevel
        self.physical = physical
        self.dirs = dirs
        self.files = files

    def run(self, command: str, *, cwd: str | None = None, timeout: int | None = None):
        self.commands.append(command)
        if "AGENT_DIRS" in command:
            root = "1" if self.toplevel == self.physical else "0"
            return ExecResult(
                command=command,
                exit_code=0,
                stdout=(
                    f"AGENT_DIRS\n{self.dirs}"
                    f"AGENT_FILES\n{self.files}"
                    f"AGENT_GIT\nINSIDE=1\nROOT={root}\nCLEAN=1\n"
                ),
                stderr="",
                duration_ms=1,
            )
        if "rev-parse --is-inside-work-tree" in command:
            return ExecResult(
                command=command, exit_code=0, stdout="true\n", stderr="", duration_ms=1
            )
        if "rev-parse --show-toplevel" in command:
            return ExecResult(
                command=command,
                exit_code=0,
                stdout=f"{self.toplevel}\n",
                stderr="",
                duration_ms=1,
            )
        if command.strip().startswith("pwd"):
            return ExecResult(
                command=command,
                exit_code=0,
                stdout=f"{self.physical}\n",
                stderr="",
                duration_ms=1,
            )
        if "git status --porcelain" in command:
            return ExecResult(
                command=command, exit_code=0, stdout="", stderr="", duration_ms=1
            )
        if "-type f" in command:
            return ExecResult(
                command=command,
                exit_code=0,
                stdout=self.files,
                stderr="",
                duration_ms=1,
            )
        if "maxdepth 1" in command:
            return ExecResult(
                command=command,
                exit_code=0,
                stdout=self.dirs,
                stderr="",
                duration_ms=1,
            )
        return ExecResult(
            command=command,
            exit_code=0,
            stdout="HAS pytest\nDIR tests\nFILE pytest.ini\n",
            stderr="",
            duration_ms=1,
        )

    def cached_probe(self, key: str, producer):
        return producer()

    def forget_probe(self, key: str) -> None:
        return None


def test_survey_collects_top_dirs_and_test_command() -> None:
    sandbox = _SurveySandbox(_settings())
    node = make_survey_node(sandbox, "/ws")  # type: ignore[arg-type]
    out = node({"repo_profile": {"file_count": 50, "test_file_count": 4}}, None)

    summary = out["repo_summary"]
    assert summary["file_count"] == 50
    assert summary["top_dirs"] == ["app", "tests"]
    assert summary["top_files"] == ["main.py", "pyproject.toml"]
    assert summary["test_command"] == "python3 -m pytest -q"
    assert summary["git"] is True
    assert summary["git_root"] is True
    assert summary["git_clean"] is True


def test_survey_excludes_agent_state_from_the_dirty_check() -> None:
    """`.agent/` 是 agent 自己的产物；它不该让写编排在第二次运行时被关掉。"""
    sandbox = _SurveySandbox(_settings())
    node = make_survey_node(sandbox, "/ws")  # type: ignore[arg-type]

    node({"repo_profile": {"file_count": 50}}, None)

    status_commands = [
        command for command in sandbox.commands if "git status --porcelain" in command
    ]
    assert status_commands
    assert ":(exclude).agent" in status_commands[0]
    assert ":(exclude,glob)**/__pycache__/**" in status_commands[0]


def test_survey_marks_a_non_root_workspace() -> None:
    """工作区是仓库子目录时，patch 路径会错位；必须显式禁用写编排。"""
    sandbox = _SurveySandbox(_settings(), toplevel="/repo", physical="/repo/sub")
    node = make_survey_node(sandbox, "/repo/sub")  # type: ignore[arg-type]

    out = node({"repo_profile": {"file_count": 50}}, None)

    assert out["repo_summary"]["git_root"] is False


class _PatchPathSandbox:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def run(self, command: str, *, cwd: str | None = None, timeout: int | None = None):
        return ExecResult(
            command=command,
            exit_code=0,
            stdout=self.stdout,
            stderr="",
            duration_ms=1,
        )


def test_patch_paths_parses_numstat_output() -> None:
    sandbox = _PatchPathSandbox("1\t1\tapp/a.py\0")

    paths, error = _patch_paths(sandbox, "/ws/t.patch", "/ws")  # type: ignore[arg-type]

    assert error == ""
    assert paths == ["app/a.py"]


def test_patch_paths_rejects_paths_outside_the_workspace() -> None:
    sandbox = _PatchPathSandbox("1\t1\t../outside.py\0")

    paths, error = _patch_paths(sandbox, "/ws/t.patch", "/ws")  # type: ignore[arg-type]

    assert paths == []
    assert "不允许合并" in error


def test_safe_id_keeps_long_task_ids_distinct() -> None:
    prefix = "task-" + "x" * 60

    assert _safe_id(f"{prefix}-a") != _safe_id(f"{prefix}-b")


def test_plan_tasks_validates_and_initializes_status() -> None:
    graph = TaskGraph(
        tasks=[
            TaskSpec(id="t1", role="explore", goal="定位实现"),
            TaskSpec(id="t2", role="test", goal="跑测试"),
        ]
    )
    node = make_plan_tasks_node(_StructuredLLM(_Structured(graph)), _settings())
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="改功能")],
                "repo_summary": {"file_count": 50},
            },
            None,
        )
    )
    assert [t["id"] for t in out["tasks"]] == ["t1", "t2"]
    assert out["task_status"] == {"t1": "pending", "t2": "pending"}
    assert out["orchestrator_degraded"] is False


def test_plan_tasks_write_mode_falls_back_to_one_implement_task() -> None:
    node = make_plan_tasks_node(
        _StructuredLLM(_Structured(error=RuntimeError("boom"))),
        _settings(orchestrator_write_enabled=True),
        allow_write=True,
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="两件事一起做")],
                "workflow": "orchestrated_implement",
                "repo_summary": {
                    "file_count": 50,
                    "top_dirs": ["app", "tests"],
                },
            },
            None,
        )
    )
    assert out["orchestrator_degraded"] is True
    assert len(out["tasks"]) == 1
    assert out["tasks"][0]["role"] == "implement"
    assert out["tasks"][0]["write_scope"] == ["app"]


def test_plan_tasks_write_fallback_uses_top_level_files_in_a_flat_repo() -> None:
    """扁平仓库没有顶层目录；兜底应收敛到顶层文件，而不是让写路径消失。"""
    node = make_plan_tasks_node(
        _StructuredLLM(_Structured(error=RuntimeError("boom"))),
        _settings(orchestrator_write_enabled=True),
        allow_write=True,
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="修好 main.py")],
                "workflow": "orchestrated_implement",
                "repo_summary": {
                    "file_count": 50,
                    "top_dirs": [],
                    "top_files": ["main.py", "pyproject.toml"],
                },
            },
            None,
        )
    )

    assert out["orchestrator_degraded"] is True
    assert len(out["tasks"]) == 1
    assert out["tasks"][0]["role"] == "implement"
    assert out["tasks"][0]["write_scope"] == ["main.py", "pyproject.toml"]


def test_plan_tasks_write_fallback_reports_an_empty_workspace() -> None:
    """既没有目录也没有文件时，必须明确说明写编排降级。"""
    node = make_plan_tasks_node(
        _StructuredLLM(_Structured(error=RuntimeError("boom"))),
        _settings(orchestrator_write_enabled=True),
        allow_write=True,
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="改点什么")],
                "workflow": "orchestrated_implement",
                "repo_summary": {"file_count": 50, "top_dirs": [], "top_files": []},
            },
            None,
        )
    )

    assert out["orchestrator_degraded"] is True
    assert "没有可用的顶层目录或文件" in out["orchestrator_summary"]


def test_plan_tasks_falls_back_on_structured_failure() -> None:
    node = make_plan_tasks_node(
        _StructuredLLM(_Structured(error=RuntimeError("boom"))), _settings()
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="改功能")],
                "repo_summary": {"file_count": 50},
            },
            None,
        )
    )
    assert out["orchestrator_degraded"] is True
    assert len(out["tasks"]) == 1
    assert out["tasks"][0]["role"] == "explore"


def test_plan_tasks_adds_a_second_task_when_model_returns_one() -> None:
    graph = TaskGraph(tasks=[TaskSpec(id="t1", role="explore", goal="定位")])
    node = make_plan_tasks_node(
        _StructuredLLM(_Structured(graph)),
        _settings(orchestrator_add_explore_tests=True),
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="改功能")],
                "repo_summary": {"file_count": 50},
            },
            None,
        )
    )
    assert len(out["tasks"]) == 2
    assert out["tasks"][1]["id"] == "explore-tests"


def test_dispatch_runs_ready_tasks_concurrently() -> None:
    barrier = asyncio.Barrier(2)
    seen: list[str] = []

    async def runner(task, **_kwargs):
        seen.append(task["id"])
        await asyncio.wait_for(barrier.wait(), timeout=1)
        return WorkerResult(
            task_id=task["id"],
            role=task["role"],
            worker_id=f"{task['role']}:{task['id']}",
            goal=task["goal"],
            status="success",
            summary="ok",
        )

    node = make_dispatch_node(
        worker_runner=runner, settings=_settings(orchestrator_max_concurrent_workers=2)
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="r")],
                "cwd": "/ws",
                "tasks": [
                    {"id": "t1", "role": "explore", "goal": "a"},
                    {"id": "t2", "role": "explore", "goal": "b"},
                ],
                "task_status": {"t1": "pending", "t2": "pending"},
                "worker_results": [],
                "orchestrator_round": 0,
            },
            None,
        )
    )

    assert sorted(seen) == ["t1", "t2"]
    assert out["task_status"] == {"t1": "success", "t2": "success"}
    assert len(out["worker_results"]) == 2
    assert out["orchestrator_round"] == 1


def test_dispatch_isolates_a_failed_worker() -> None:
    async def runner(task, **_kwargs):
        if task["id"] == "t1":
            raise RuntimeError("boom")
        return WorkerResult(
            task_id=task["id"], role=task["role"], status="success", summary="ok"
        )

    node = make_dispatch_node(
        worker_runner=runner, settings=_settings(orchestrator_task_retries=0)
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="r")],
                "tasks": [
                    {"id": "t1", "role": "explore", "goal": "a"},
                    {"id": "t2", "role": "explore", "goal": "b"},
                ],
                "task_status": {"t1": "pending", "t2": "pending"},
            },
            None,
        )
    )
    assert out["task_status"]["t1"] == "failed"
    assert out["task_status"]["t2"] == "success"


def test_dispatch_retries_a_failed_task_before_blocking_dependents() -> None:
    calls: list[str] = []

    async def runner(task, **_kwargs):
        calls.append(task["id"])
        if len(calls) == 1:
            return WorkerResult(
                task_id=task["id"], role=task["role"], status="failed", summary="boom"
            )
        return WorkerResult(
            task_id=task["id"], role=task["role"], status="success", summary="ok"
        )

    node = make_dispatch_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=1),
    )
    state = {
        "messages": [HumanMessage(content="r")],
        "tasks": [{"id": "t1", "role": "explore", "goal": "a"}],
        "task_status": {"t1": "pending"},
        "task_attempts": {},
        "worker_results": [],
        "orchestrator_round": 0,
    }

    first = _run(node(state, None))
    assert first["task_status"]["t1"] == "pending"
    assert first["task_attempts"] == {"t1": 1}

    second = _run(node({**state, **first}, None))
    assert second["task_status"]["t1"] == "success"
    assert second["task_attempts"] == {"t1": 2}
    assert [r["attempt"] for r in second["worker_results"]] == [1, 2]


def test_failed_task_without_retries_blocks_dependents() -> None:
    async def runner(task, **_kwargs):
        return WorkerResult(
            task_id=task["id"], role=task["role"], status="failed", summary="boom"
        )

    node = make_dispatch_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=0),
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="r")],
                "tasks": [
                    {"id": "t1", "role": "explore", "goal": "a", "depends_on": []},
                    {"id": "t2", "role": "explore", "goal": "b", "depends_on": ["t1"]},
                ],
                "task_status": {"t1": "pending", "t2": "pending"},
                "task_attempts": {},
                "worker_results": [],
                "orchestrator_round": 0,
            },
            None,
        )
    )
    assert out["task_status"]["t1"] == "failed"

    synth = make_synthesize_node(_settings())(
        {
            "tasks": [
                {"id": "t1", "depends_on": []},
                {"id": "t2", "depends_on": ["t1"]},
            ],
            "task_status": out["task_status"],
            "worker_results": out["worker_results"],
        },
        None,
    )
    assert synth["task_status"]["t2"] == "blocked"


def test_review_role_uses_the_review_runner() -> None:
    async def review_runner(**_kwargs):
        return SubAgentArtifact(
            agent_id="review:r1",
            parent_call_id="r1",
            role="review",
            goal="审查",
            status="success",
            summary="发现一个风险",
            findings=["[warning] app/x.py 边界条件"],
        )

    runner = make_worker_runner(
        explore_runner=None,
        review_runner=review_runner,  # type: ignore[arg-type]
        sandbox=None,  # type: ignore[arg-type]
        root="/ws",
    )
    result = _run(
        runner(
            {"id": "r1", "role": "review", "goal": "审查"},
            parent_request="r",
            cwd="/ws",
        )
    )
    assert result.role == "review"
    assert result.summary == "发现一个风险"
    assert result.findings == ["[warning] app/x.py 边界条件"]


def test_synthesize_uses_latest_attempt_and_reports_file_overlap() -> None:
    node = make_synthesize_node(_settings())
    results = [
        WorkerResult(
            task_id="t1",
            role="explore",
            status="failed",
            summary="first",
            files=["app/x.py"],
            attempt=1,
        ).model_dump(),
        WorkerResult(
            task_id="t1",
            role="explore",
            status="success",
            summary="second",
            files=["app/x.py"],
            attempt=2,
        ).model_dump(),
        WorkerResult(
            task_id="t2",
            role="review",
            status="success",
            summary="review",
            files=["app/x.py"],
            findings=["[blocking] x"],
            attempt=1,
        ).model_dump(),
    ]
    out = node(
        {
            "tasks": [
                {"id": "t1", "depends_on": []},
                {"id": "t2", "depends_on": []},
            ],
            "task_status": {"t1": "success", "t2": "success"},
            "worker_results": results,
        },
        None,
    )
    context = out["worker_context"]
    assert "第 2 次尝试" in context
    assert "first" not in context
    assert "多 worker 关注同一文件" in context
    assert "app/x.py" in context
    assert "重叠文件 1" in out["orchestrator_summary"]


class _MergeSandbox:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: str | None = None, timeout: int | None = None):
        self.commands.append(command)
        return ExecResult(
            command=command,
            exit_code=0 if self.ok else 1,
            stdout="",
            stderr="" if self.ok else "patch conflict",
            duration_ms=1,
        )


def test_dispatch_runs_an_any_mode_review_after_a_partial_dependency() -> None:
    called: list[str] = []

    async def runner(task, **_kwargs):
        called.append(task["id"])
        return WorkerResult(
            task_id=task["id"],
            role=task["role"],
            status="success",
            summary="审查完成",
        )

    node = make_dispatch_node(worker_runner=runner, settings=_settings())
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="r")],
                "tasks": [
                    {"id": "t1", "role": "implement", "goal": "改"},
                    {
                        "id": "t2",
                        "role": "review",
                        "goal": "审查",
                        "depends_on": ["t1"],
                        "depends_mode": "any",
                    },
                ],
                "task_status": {"t1": "partial", "t2": "pending"},
                "task_attempts": {},
                "worker_results": [],
                "orchestrator_round": 0,
            },
            None,
        )
    )

    assert called == ["t2"]
    assert out["task_status"]["t2"] == "success"


def test_dispatch_streams_worker_started_before_the_worker_finishes(
    monkeypatch,
) -> None:
    """Phase 4：started 必须在 worker 运行期间发出，而不是整批结束后回放。"""
    emitted: list[tuple[float, dict[str, Any]]] = []
    runner_started = 0.0
    runner_ended = 0.0

    monkeypatch.setattr(
        orchestrator_nodes,
        "get_stream_writer",
        lambda: (lambda payload: emitted.append((time.perf_counter(), payload))),
    )

    async def runner(task, **_kwargs):
        nonlocal runner_started, runner_ended
        runner_started = time.perf_counter()
        await asyncio.sleep(0.02)
        runner_ended = time.perf_counter()
        return WorkerResult(
            task_id=task["id"],
            role=task["role"],
            worker_id=f"{task['role']}:{task['id']}",
            goal=task["goal"],
            status="success",
            summary="ok",
        )

    node = make_dispatch_node(worker_runner=runner, settings=_settings())
    _run(
        node(
            {
                "messages": [HumanMessage(content="r")],
                "tasks": [{"id": "t1", "role": "explore", "goal": "a"}],
                "task_status": {"t1": "pending"},
                "task_attempts": {},
                "worker_results": [],
                "orchestrator_round": 0,
            },
            None,
        )
    )

    starts = [(at, data) for at, data in emitted if data["type"] == "worker_started"]
    finishes = [(at, data) for at, data in emitted if data["type"] == "worker_finished"]
    assert starts and finishes
    assert starts[0][1]["attempt"] == 1
    assert starts[0][0] <= runner_started
    assert runner_ended <= finishes[0][0]


def test_dispatch_merges_implement_patches_after_the_batch() -> None:
    async def runner(task, **_kwargs):
        # 快照路径由真实 WSL 集成测试覆盖；这个假 sandbox 只验证合并顺序。
        return WorkerResult(
            task_id=task["id"],
            role="implement",
            status="success",
            summary="done",
            files=[],
            patch_path="/ws/.agent/patches/t1.patch",
        )

    sandbox = _MergeSandbox()
    node = make_dispatch_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=0),
        sandbox=sandbox,  # type: ignore[arg-type]
        root="/ws",
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="r")],
                "tasks": [
                    {
                        "id": "t1",
                        "role": "implement",
                        "goal": "改",
                        "write_scope": ["app/a.py"],
                    }
                ],
                "task_status": {"t1": "pending"},
                "task_attempts": {},
                "worker_results": [],
                "orchestrator_round": 0,
            },
            None,
        )
    )
    assert out["worker_results"][0]["merged"] is True
    assert out["dirty"] is True and out["run_dirty"] is True
    assert any(command.startswith("git apply --check") for command in sandbox.commands)


def test_dispatch_marks_a_conflicting_patch_failed() -> None:
    async def runner(task, **_kwargs):
        return WorkerResult(
            task_id=task["id"],
            role="implement",
            status="success",
            summary="done",
            patch_path="/ws/.agent/patches/t1.patch",
        )

    sandbox = _MergeSandbox(ok=False)
    node = make_dispatch_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=0),
        sandbox=sandbox,  # type: ignore[arg-type]
        root="/ws",
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="r")],
                "tasks": [
                    {
                        "id": "t1",
                        "role": "implement",
                        "goal": "改",
                        "write_scope": ["app/a.py"],
                    }
                ],
                "task_status": {"t1": "pending"},
            },
            None,
        )
    )
    assert out["worker_results"][0]["merged"] is False
    assert out["worker_results"][0]["merge_error"]
    assert out["task_status"]["t1"] == "failed"
    assert "dirty" not in out


def test_synthesize_marks_merged_implement_tasks() -> None:
    node = make_synthesize_node(_settings())
    out = node(
        {
            "tasks": [{"id": "t1", "depends_on": []}],
            "task_status": {"t1": "success"},
            "worker_results": [
                WorkerResult(
                    task_id="t1",
                    role="implement",
                    status="success",
                    summary="完成",
                    files=["app/a.py"],
                    patch_path="/ws/.agent/patches/t1.patch",
                    merged=True,
                ).model_dump()
            ],
        },
        None,
    )
    assert "已合并" in out["worker_context"]
    assert "不要重做" in out["worker_context"]
    assert "已合并 1" in out["orchestrator_summary"]


def test_implement_scope_helper() -> None:
    from coding_agent.orchestrator.implement import _within_scope

    assert _within_scope("app/x.py", ["app"]) is True
    assert _within_scope("app/x.py", ["app/x.py"]) is True
    assert _within_scope("tests/x.py", ["app"]) is False
    assert _within_scope("app2/x.py", ["app"]) is False


def test_dispatch_marks_pending_blocked_after_max_rounds() -> None:
    node = make_dispatch_node(
        worker_runner=None,  # type: ignore[arg-type]
        settings=_settings(orchestrator_max_rounds=1),
    )
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="r")],
                "tasks": [{"id": "t1", "role": "explore", "goal": "a"}],
                "task_status": {"t1": "pending"},
                "orchestrator_round": 1,
            },
            None,
        )
    )
    assert out["task_status"] == {"t1": "blocked"}


def test_synthesize_blocks_dependents_and_counts() -> None:
    node = make_synthesize_node(_settings())
    out = node(
        {
            "tasks": [
                {"id": "t1", "depends_on": []},
                {"id": "t2", "depends_on": ["t1"]},
            ],
            "task_status": {"t1": "failed", "t2": "pending"},
            "worker_results": [
                WorkerResult(
                    task_id="t1",
                    role="explore",
                    status="failed",
                    summary="没找到",
                ).model_dump()
            ],
        },
        None,
    )
    assert out["task_status"]["t2"] == "blocked"
    assert out["orchestrator_completed"] == 0
    assert out["orchestrator_failed"] == 2
    assert "t2" in out["worker_context"]


def test_synthesize_caps_the_worker_context() -> None:
    node = make_synthesize_node(_settings(orchestrator_context_chars=500))
    results = [
        WorkerResult(
            task_id=f"t{i}", role="explore", status="success", summary="x" * 500
        ).model_dump()
        for i in range(5)
    ]
    out = node(
        {"tasks": [], "task_status": {}, "worker_results": results}, None
    )
    assert len(out["worker_context"]) <= 520
    assert "汇总已截断" in out["worker_context"]


def test_orchestrator_routing() -> None:
    assert route_entry({"execution_mode": "orchestrated"}) == "survey"
    assert route_entry({"execution_mode": "direct"}) == "planner"
    assert route_after_select({"workflow": "parallel_readonly"}) == "plan_tasks"
    assert route_after_select({"workflow": "orchestrated_implement"}) == "plan_tasks"
    assert route_after_select({"workflow": "direct"}) == "planner"
    route = make_route_after_dispatch(_settings(orchestrator_max_rounds=2))
    assert (
        route(
            {
                "tasks": [{"id": "t1", "depends_on": []}],
                "task_status": {"t1": "pending"},
                "orchestrator_round": 0,
            }
        )
        == "dispatch"
    )
    assert (
        route(
            {
                "tasks": [{"id": "t1", "depends_on": []}],
                "task_status": {"t1": "success"},
                "orchestrator_round": 1,
            }
        )
        == "synthesize"
    )


def test_plan_tasks_consumes_prefetched_tasks_without_a_second_call() -> None:
    structured = _Structured(error=RuntimeError("不应再调用模型"))
    node = make_plan_tasks_node(_StructuredLLM(structured), _settings())
    out = _run(
        node(
            {
                "messages": [HumanMessage(content="做事")],
                "repo_summary": {"file_count": 50},
                "prefetched_tasks": [
                    {
                        "id": "t1",
                        "role": "explore",
                        "goal": "定位入口",
                        "depends_on": [],
                    }
                ],
                "prefetched_degraded": False,
            },
            None,
        )
    )

    assert structured.calls == 0
    assert out["tasks"][0]["id"] == "t1"
    assert out["prefetched_tasks"] == []
