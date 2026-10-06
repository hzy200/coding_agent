"""runtime 的回滚 API：`list_snapshots` / `restore` / `diff_snapshot`。

`SnapshotStore` 本身有测试，但 runtime 这一层额外做了两件事：
**记审计**、以及**在多个入口间做目标解析**（快照 id / 路径 / 最近一次）。
这里覆盖的就是这层。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.audit import AuditLogger, read_records
from coding_agent.runtime import AgentRuntime
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import unpack
from coding_agent.tools.files import EDIT_TOOL_NAME, WRITE_TOOL_NAME, build_file_tools

pytestmark = pytest.mark.wsl


@pytest.fixture
def workdir(require_wsl: WslSandbox) -> str:
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-rt-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(root)}")
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


@pytest.fixture
def ctx(require_wsl, workdir, settings, tmp_path):
    scoped = settings.model_copy(update={"wsl_workspace": workdir})
    tools = {t.name: t for t in build_file_tools(scoped, require_wsl, allow_write=True)}
    runtime = AgentRuntime(
        scoped, workspace=workdir, audit=AuditLogger(tmp_path), allow_write=True
    )
    return {"sandbox": require_wsl, "tools": tools, "runtime": runtime, "ws": workdir}


def _write(ctx, name: str, content: str) -> None:
    ctx["tools"][WRITE_TOOL_NAME].invoke(
        {"path": name, "content": content, "reason": "准备"}
    )


def _edit(ctx, name: str, old: str, new: str) -> str:
    _, artifact = unpack(
        ctx["tools"][EDIT_TOOL_NAME].invoke(
            {"path": name, "old_string": old, "new_string": new, "reason": "改"}
        )
    )
    return artifact["snapshot_id"]


def _content(ctx, name: str) -> str:
    target = shlex.quote(f"{ctx['ws']}/{name}")
    return ctx["sandbox"].run(f"cat -- {target}").stdout


# ---------------- 列举 ----------------

def test_list_snapshots_is_empty_initially(ctx) -> None:
    assert ctx["runtime"].list_snapshots() == []


def test_list_snapshots_returns_recent_first(ctx) -> None:
    _write(ctx, "a.py", "v1\n")
    _edit(ctx, "a.py", "v1", "v2")
    _edit(ctx, "a.py", "v2", "v3")

    # 3 份：新建一份 + 两次改动各一份（新建那份记的是「当时不存在」）
    entries = ctx["runtime"].list_snapshots()
    assert len(entries) == 3
    assert entries[0].snapshot_id > entries[1].snapshot_id > entries[2].snapshot_id


def test_list_snapshots_honours_limit(ctx) -> None:
    _write(ctx, "a.py", "v1\n")
    _edit(ctx, "a.py", "v1", "v2")
    _edit(ctx, "a.py", "v2", "v3")
    assert len(ctx["runtime"].list_snapshots(limit=1)) == 1


# ---------------- 恢复 ----------------

def test_restore_latest_change(ctx) -> None:
    _write(ctx, "a.py", "v1\n")
    _edit(ctx, "a.py", "v1", "v2")
    assert _content(ctx, "a.py") == "v2\n"

    result = ctx["runtime"].restore()

    assert result.ok
    assert _content(ctx, "a.py") == "v1\n"


def test_restore_by_path(ctx) -> None:
    _write(ctx, "a.py", "a1")
    _write(ctx, "b.py", "b1")
    _edit(ctx, "a.py", "a1", "a2")
    _edit(ctx, "b.py", "b1", "b2")

    ctx["runtime"].restore(path="a.py")

    assert _content(ctx, "a.py") == "a1"
    assert _content(ctx, "b.py") == "b2"  # 另一个文件不受影响


def test_restore_by_snapshot_id(ctx) -> None:
    _write(ctx, "a.py", "v1\n")
    first = _edit(ctx, "a.py", "v1", "v2")
    _edit(ctx, "a.py", "v2", "v3")

    result = ctx["runtime"].restore(snapshot_id=first, path="a.py")

    assert result.ok
    assert _content(ctx, "a.py") == "v1\n"


def test_restore_unknown_snapshot_reports_failure(ctx) -> None:
    _write(ctx, "a.py", "v1")
    result = ctx["runtime"].restore(snapshot_id="20200101T000000-abcdef")
    assert result.ok is False
    assert "找不到快照" in result.message


def test_restore_missing_path_reports_failure(ctx) -> None:
    _write(ctx, "a.py", "v1")
    result = ctx["runtime"].restore(path="never.py")
    assert result.ok is False
    assert "没有任何留底" in result.message


def test_restore_without_any_snapshots_reports_failure(ctx) -> None:
    result = ctx["runtime"].restore()
    assert result.ok is False
    assert "还没有任何快照" in result.message


def test_restore_is_itself_undoable(ctx) -> None:
    _write(ctx, "a.py", "v1\n")
    _edit(ctx, "a.py", "v1", "v2")

    ctx["runtime"].restore()
    assert _content(ctx, "a.py") == "v1\n"

    ctx["runtime"].restore()  # 再滚一次 = 撤销上次回滚
    assert _content(ctx, "a.py") == "v2\n"


# ---------------- 回滚的审计 ----------------

def test_restore_is_audited(ctx) -> None:
    """用户主动发起的操作不走审批，但同样要记账。"""
    _write(ctx, "a.py", "v1\n")
    _edit(ctx, "a.py", "v1", "v2")

    ctx["runtime"].restore()

    records = [r for r in read_records(ctx["runtime"].audit_path) if r.kind == "rollback"]
    assert len(records) == 1
    record = records[0]
    assert record.path == "a.py"
    assert record.action == "restore"
    assert record.snapshot_id
    assert record.ok is True
    assert (record.added, record.removed) == (1, 1)


def test_failed_restore_is_also_audited(ctx) -> None:
    """失败也要留痕 —— 否则审计里会出现「用户以为回滚了但没回滚」的空白。"""
    ctx["runtime"].restore(path="never.py")

    records = [r for r in read_records(ctx["runtime"].audit_path) if r.kind == "rollback"]
    assert len(records) == 1
    assert records[0].ok is False


def test_rollback_audit_records_no_thread_id(ctx) -> None:
    """用户直接发起的操作没有会话上下文，不该硬塞一个。"""
    _write(ctx, "a.py", "v1\n")
    _edit(ctx, "a.py", "v1", "v2")
    ctx["runtime"].restore()

    record = next(r for r in read_records(ctx["runtime"].audit_path) if r.kind == "rollback")
    assert record.thread_id == ""
    assert record.workspace == ctx["ws"]


# ---------------- diff ----------------

def test_diff_shows_snapshot_versus_current(ctx) -> None:
    _write(ctx, "a.py", "alpha\nbeta\n")
    _edit(ctx, "a.py", "alpha", "ALPHA")

    diff = ctx["runtime"].diff_snapshot(path="a.py")

    assert "-alpha" in diff
    assert "+ALPHA" in diff
    assert "a.py" in diff


def test_diff_of_latest_change(ctx) -> None:
    _write(ctx, "a.py", "v1\n")
    _edit(ctx, "a.py", "v1", "v2")
    assert "-v1" in ctx["runtime"].diff_snapshot()


def test_diff_is_empty_when_nothing_recorded(ctx) -> None:
    assert ctx["runtime"].diff_snapshot() == ""


def test_diff_is_empty_when_content_matches(ctx) -> None:
    """回滚到某个快照之后，再拿那个快照跟当前内容比，应当没有差异。

    注意不能拿「最近一次快照」来比 —— 回滚会先给当前内容留底，
    所以最新的那条其实是**撤销快照**，它和回滚后的内容必然不同。
    """
    _write(ctx, "a.py", "v1\n")
    origin = _edit(ctx, "a.py", "v1", "v2")
    ctx["runtime"].restore(path="a.py")

    assert ctx["runtime"].diff_snapshot(snapshot_id=origin, path="a.py") == ""
    # 而撤销快照本身与回滚后的内容是有差异的
    assert ctx["runtime"].diff_snapshot(path="a.py") != ""


def test_diff_by_snapshot_id(ctx) -> None:
    _write(ctx, "a.py", "v1\n")
    first = _edit(ctx, "a.py", "v1", "v2")
    _edit(ctx, "a.py", "v2", "v3")

    diff = ctx["runtime"].diff_snapshot(snapshot_id=first, path="a.py")
    assert "-v1" in diff
    assert "+v3" in diff  # 与「当前」比较，不是与下一个快照


def test_diff_handles_deleted_file(ctx) -> None:
    """文件被删掉后，diff 应当显示为「全部删掉」，而不是报错。"""
    _write(ctx, "a.py", "内容\n")
    _edit(ctx, "a.py", "内容", "新内容")
    target = shlex.quote(f"{ctx['ws']}/a.py")
    ctx["sandbox"].run(f"rm -f {target}")

    diff = ctx["runtime"].diff_snapshot(path="a.py")
    assert "-内容" in diff


# ---------------- 快照与设置的关系 ----------------

def test_snapshots_use_the_active_workspace(ctx, tmp_path) -> None:
    """快照存储跟着工作区走，不是跟着进程目录。"""
    store = ctx["runtime"].snapshots
    assert store.root == ctx["ws"]
    assert store.backup_root.startswith(ctx["ws"])


def test_runtime_settings_are_effective_settings(ctx, settings) -> None:
    """workspace 覆盖必须写回 settings，否则工具的路径守卫会用错根目录。"""
    assert ctx["runtime"].effective_settings.wsl_workspace == ctx["ws"]


def test_cleanup_does_not_leave_snapshots_outside(ctx) -> None:
    """反证：所有快照都在工作区内。"""
    _write(ctx, "a.py", "v1\n")
    _edit(ctx, "a.py", "v1", "v2")
    for entry in ctx["runtime"].list_snapshots():
        assert not entry.path.startswith("/")
