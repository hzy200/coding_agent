"""Phase 3C：真实 WSL 下的 implement worker / worktree / patch。

用假 LLM 驱动真实文件工具：worker 在独立 worktree 中修改文件，生成 patch；
测试确认 patch 只包含源码、能 `git apply --check`，并且 worktree 被清理。
"""

from __future__ import annotations

import asyncio
import shlex

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.implement import make_implement_runner
from coding_agent.orchestrator.nodes import make_dispatch_node, make_survey_node
from coding_agent.orchestrator.workflow import rule_workflow
from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.snapshots import SnapshotStore


class _FakeLLM:
    def __init__(
        self,
        *,
        path: str = "app/sample.py",
        old: str = "VALUE = 1",
        new: str = "VALUE = 2",
        call_id: str = "c1",
    ) -> None:
        self.calls = 0
        self.path = path
        self.old = old
        self.new = new
        self.call_id = call_id

    def bind_tools(self, tools):  # noqa: ANN001
        self.tools = tools
        return self

    async def ainvoke(self, messages, config=None):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "file_edit",
                        "args": {
                            "path": self.path,
                            "old_string": self.old,
                            "new_string": self.new,
                            "reason": "修改示例值",
                        },
                        "id": self.call_id,
                    }
                ],
            )
        return AIMessage(content="完成")


@pytest.mark.wsl
def test_implement_worker_creates_a_clean_patch(require_wsl) -> None:  # noqa: ANN001
    sandbox = require_wsl
    root = sandbox.run("mktemp -d /tmp/agent-implement-XXXXXX").stdout.strip()
    assert root
    try:
        setup = sandbox.run(
            f"""
            cd {shlex.quote(root)}
            git init -q
            git config user.email eval@example.invalid
            git config user.name eval
            mkdir -p app
            printf 'VALUE = 1\\n' > app/sample.py
            git add -A
            git commit -qm init
            """,
            cwd=root,
        )
        assert setup.ok, setup.render(500)

        settings = sandbox.settings.model_copy(
            update={"orchestrator_write_enabled": True}
        )
        runner = make_implement_runner(
            llm=_FakeLLM(),  # type: ignore[arg-type]
            sandbox=sandbox,
            root=root,
            settings=settings,
        )
        result = asyncio.run(
            runner(
                {
                    "id": "implement-1",
                    "role": "implement",
                    "goal": "修改示例值",
                    "write_scope": ["app"],
                },
                parent_request="修改示例值",
                cwd=root,
            )
        )

        assert result.status == "success", result.model_dump()
        assert result.files == ["app/sample.py"]
        assert result.patch_path
        assert result.tool_calls

        check = sandbox.run(
            f"git apply --check {shlex.quote(result.patch_path)}", cwd=root
        )
        assert check.ok, check.render(500)

        patch = sandbox.run(f"cat {shlex.quote(result.patch_path)}")
        assert "app/sample.py" in patch.stdout
        assert ".agent" not in patch.stdout
        assert "__pycache__" not in patch.stdout
        # worktree 已清理
        leftovers = sandbox.run(f"ls -A {shlex.quote(root)}/.agent/worktrees")
        assert not leftovers.stdout.strip()
    finally:
        sandbox.run(f"rm -rf {shlex.quote(root)}")


@pytest.mark.wsl
def test_concurrent_implement_workers_ignore_agent_state(require_wsl) -> None:  # noqa: ANN001
    """两个写 worker 并发时，不能把对方刚建的 worktree 当成"用户工作区脏"。

    回归：whichever worker 先启动，都会在主工作区留下 `.agent/worktrees/...`；
    第二个 worker 的 `git status` 若不做 pathspec 排除，就会误判并拒绝执行。
    """
    sandbox = require_wsl
    root = sandbox.run("mktemp -d /tmp/agent-implement-pair-XXXXXX").stdout.strip()
    assert root
    try:
        setup = sandbox.run(
            f"""
            cd {shlex.quote(root)}
            git init -q
            git config user.email eval@example.invalid
            git config user.name eval
            mkdir -p app
            printf 'VALUE = 1\\n' > app/alpha.py
            printf 'VALUE = 10\\n' > app/beta.py
            git add -A
            git commit -qm init
            """,
            cwd=root,
        )
        assert setup.ok, setup.render(500)

        settings = sandbox.settings.model_copy(
            update={"orchestrator_write_enabled": True}
        )
        alpha = make_implement_runner(
            llm=_FakeLLM(
                path="app/alpha.py", old="VALUE = 1", new="VALUE = 2", call_id="a1"
            ),
            sandbox=sandbox,
            root=root,
            settings=settings,
        )
        beta = make_implement_runner(
            llm=_FakeLLM(
                path="app/beta.py",
                old="VALUE = 10",
                new="VALUE = 20",
                call_id="b1",
            ),
            sandbox=sandbox,
            root=root,
            settings=settings,
        )

        async def run_both():
            return await asyncio.gather(
                alpha(
                    {
                        "id": "alpha-task",
                        "role": "implement",
                        "goal": "改 alpha",
                        "write_scope": ["app/alpha.py"],
                    },
                    parent_request="改两个独立文件",
                    cwd=root,
                ),
                beta(
                    {
                        "id": "beta-task",
                        "role": "implement",
                        "goal": "改 beta",
                        "write_scope": ["app/beta.py"],
                    },
                    parent_request="改两个独立文件",
                    cwd=root,
                ),
            )

        results = asyncio.run(run_both())
        assert [result.status for result in results] == ["success", "success"], [
            result.model_dump() for result in results
        ]
        assert [result.files for result in results] == [
            ["app/alpha.py"],
            ["app/beta.py"],
        ]

        # 合并顺序与 dispatch 一致：逐个 `git apply --check` → apply。
        for result in results:
            check = sandbox.run(
                f"git apply --check {shlex.quote(result.patch_path)}", cwd=root
            )
            assert check.ok, check.render(500)
            applied = sandbox.run(
                f"git apply {shlex.quote(result.patch_path)}", cwd=root
            )
            assert applied.ok, applied.render(500)

        alpha_text = sandbox.run(f"cat {shlex.quote(root)}/app/alpha.py").stdout
        beta_text = sandbox.run(f"cat {shlex.quote(root)}/app/beta.py").stdout
        assert "VALUE = 2" in alpha_text
        assert "VALUE = 20" in beta_text
    finally:
        sandbox.run(f"rm -rf {shlex.quote(root)}")


@pytest.mark.wsl
def test_write_orchestration_smoke_with_agent_state_present(require_wsl) -> None:  # noqa: ANN001
    """end-to-end 节点链：带 `.agent/` 的仓库仍能选中写编排并合并补丁。"""
    sandbox = require_wsl
    root = sandbox.run("mktemp -d /tmp/agent-write-smoke-XXXXXX").stdout.strip()
    assert root
    try:
        setup = sandbox.run(
            f"""
            cd {shlex.quote(root)}
            git init -q
            git config user.email eval@example.invalid
            git config user.name eval
            mkdir -p app .agent/patches
            printf 'VALUE = 1\\n' > app/sample.py
            printf 'leftover\\n' > .agent/patches/old.patch
            git add app
            git commit -qm init
            printf 'VALUE = 2\\n' > app/sample.py
            git diff > .agent/patches/t1.patch
            git checkout -- app/sample.py
            """,
            cwd=root,
        )
        assert setup.ok, setup.render(800)
        settings = sandbox.settings.model_copy(
            update={
                "orchestrator_write_enabled": True,
                "orchestrator_min_files": 1,
            }
        )

        summary = make_survey_node(sandbox, root)(
            {"repo_profile": {"file_count": 5}}, None
        )["repo_summary"]
        write_available = bool(
            settings.orchestrator_write_enabled
            and summary["git"]
            and summary["git_root"]
            and summary["git_clean"]
        )
        assert write_available is True
        decision = rule_workflow(
            "同时修好两个模块",
            summary,
            settings,
            write_available=write_available,
        )
        assert decision is not None
        assert decision.workflow == "orchestrated_implement"

        patch_path = f"{root}/.agent/patches/t1.patch"

        async def runner(task, **_kwargs):
            return WorkerResult(
                task_id=str(task["id"]),
                role="implement",
                status="success",
                summary="done",
                files=["app/sample.py"],
                patch_path=patch_path,
            )

        out = asyncio.run(
            make_dispatch_node(
                worker_runner=runner,
                settings=settings,
                sandbox=sandbox,
                root=root,
            )(
                {
                    "messages": [HumanMessage(content="同时改两个模块")],
                    "tasks": [
                        {
                            "id": "t1",
                            "role": "implement",
                            "goal": "改值",
                            "write_scope": ["app/sample.py"],
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

        record = out["worker_results"][0]
        assert record["merged"] is True, record.get("merge_error")
        assert record["snapshot_ids"]
        assert "VALUE = 2" in sandbox.run(
            f"cat {shlex.quote(root)}/app/sample.py"
        ).stdout
    finally:
        sandbox.run(f"rm -rf {shlex.quote(root)}")


@pytest.mark.wsl
def test_survey_treats_agent_state_as_clean_but_real_changes_as_dirty(require_wsl) -> None:  # noqa: ANN001
    """`.agent/` 不能让写编排在第二次运行时被关掉；真实改动仍必须拦住。"""
    sandbox = require_wsl
    root = sandbox.run("mktemp -d /tmp/agent-survey-XXXXXX").stdout.strip()
    assert root
    try:
        setup = sandbox.run(
            f"""
            cd {shlex.quote(root)}
            git init -q
            git config user.email eval@example.invalid
            git config user.name eval
            mkdir -p app .agent/patches
            printf 'VALUE = 1\\n' > app/sample.py
            printf 'junk\\n' > .agent/patches/leftover.patch
            git add app
            git commit -qm init
            """,
            cwd=root,
        )
        assert setup.ok, setup.render(800)

        node = make_survey_node(sandbox, root)
        out = node({"repo_profile": {"file_count": 5}}, None)
        assert out["repo_summary"]["git_root"] is True
        assert out["repo_summary"]["git_clean"] is True

        sandbox.run(f"printf 'VALUE = 2\\n' > {shlex.quote(root)}/app/sample.py")
        out = node({"repo_profile": {"file_count": 5}}, None)
        assert out["repo_summary"]["git_clean"] is False
    finally:
        sandbox.run(f"rm -rf {shlex.quote(root)}")


@pytest.mark.wsl
def test_merged_patch_takes_snapshots_so_undo_can_restore(require_wsl) -> None:  # noqa: ANN001
    """合并前必须为主工作区留底，否则 `agent undo` 回滚不了编排写。"""
    sandbox = require_wsl
    root = sandbox.run("mktemp -d /tmp/agent-merge-snapshot-XXXXXX").stdout.strip()
    assert root
    try:
        setup = sandbox.run(
            f"""
            cd {shlex.quote(root)}
            git init -q
            git config user.email eval@example.invalid
            git config user.name eval
            mkdir -p app .agent/patches
            printf 'VALUE = 1\\n' > app/sample.py
            git add -A
            git commit -qm init
            printf 'VALUE = 2\\n' > app/sample.py
            git diff > .agent/patches/t1.patch
            git checkout -- app/sample.py
            """,
            cwd=root,
        )
        assert setup.ok, setup.render(800)
        patch_path = f"{root}/.agent/patches/t1.patch"

        async def runner(task, **_kwargs):
            return WorkerResult(
                task_id=str(task["id"]),
                role="implement",
                status="success",
                summary="done",
                files=["app/sample.py"],
                patch_path=patch_path,
            )

        node = make_dispatch_node(
            worker_runner=runner,
            settings=sandbox.settings.model_copy(
                update={"orchestrator_write_enabled": True}
            ),
            sandbox=sandbox,
            root=root,
        )
        out = asyncio.run(
            node(
                {
                    "messages": [HumanMessage(content="改值")],
                    "tasks": [
                        {
                            "id": "t1",
                            "role": "implement",
                            "goal": "改值",
                            "write_scope": ["app/sample.py"],
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

        record = out["worker_results"][0]
        assert record["merged"] is True, record.get("merge_error")
        assert record["snapshot_ids"]
        assert "VALUE = 2" in sandbox.run(
            f"cat {shlex.quote(root)}/app/sample.py"
        ).stdout

        store = SnapshotStore(sandbox, SandboxFs(sandbox, root), root)
        entry = store.find(record["snapshot_ids"][0], path="app/sample.py")
        assert entry is not None
        restored = store.restore(entry)
        assert restored.ok, restored.message
        assert "VALUE = 1" in sandbox.run(
            f"cat {shlex.quote(root)}/app/sample.py"
        ).stdout
    finally:
        sandbox.run(f"rm -rf {shlex.quote(root)}")


@pytest.mark.wsl
def test_cancelled_implement_worker_discards_the_worktree(require_wsl) -> None:  # noqa: ANN001
    """Phase 3D：取消后不产生 patch，主工作区保持原样。"""
    sandbox = require_wsl
    root = sandbox.run("mktemp -d /tmp/agent-implement-cancel-XXXXXX").stdout.strip()
    assert root
    try:
        setup = sandbox.run(
            f"""
            cd {shlex.quote(root)}
            git init -q
            git config user.email eval@example.invalid
            git config user.name eval
            mkdir -p app
            printf 'VALUE = 1\\n' > app/sample.py
            git add -A
            git commit -qm init
            """,
            cwd=root,
        )
        assert setup.ok, setup.render(500)

        settings = sandbox.settings.model_copy(
            update={"orchestrator_write_enabled": True}
        )
        runner = make_implement_runner(
            llm=_FakeLLM(),
            sandbox=sandbox,
            root=root,
            settings=settings,
        )
        result = asyncio.run(
            runner(
                {
                    "id": "implement-cancel",
                    "role": "implement",
                    "goal": "不该发生的修改",
                    "write_scope": ["app/sample.py"],
                },
                parent_request="取消这个写任务",
                cwd=root,
                cancel=lambda: True,
            )
        )

        assert result.status == "cancelled"
        assert result.patch_path == ""
        assert result.files == []
        # 主工作区没有被改动，worktree 也清理干净
        assert "VALUE = 1" in sandbox.run(
            f"cat {shlex.quote(root)}/app/sample.py"
        ).stdout
        leftovers = sandbox.run(f"ls -A {shlex.quote(root)}/.agent/worktrees")
        assert not leftovers.stdout.strip()
    finally:
        sandbox.run(f"rm -rf {shlex.quote(root)}")
