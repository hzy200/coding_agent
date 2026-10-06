"""集成测试：快照与回滚。

核心不变量：**回滚本身也是可回滚的** —— 恢复前会先给当前内容留底，
否则一次误回滚就把用户当下的改动直接抹掉了。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.snapshots import SnapshotStore
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.files import (
    EDIT_TOOL_NAME,
    RESTORE_TOOL_NAME,
    WRITE_TOOL_NAME,
    build_file_tools,
)

pytestmark = pytest.mark.wsl


@pytest.fixture
def workspace(require_wsl: WslSandbox) -> str:
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-snap-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(root)}")
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


@pytest.fixture
def ctx(require_wsl, workspace, settings):
    scoped = settings.model_copy(update={"wsl_workspace": workspace})
    fs = SandboxFs(require_wsl, workspace)
    store = SnapshotStore(require_wsl, fs, workspace)
    tools = {t.name: t for t in build_file_tools(scoped, require_wsl, allow_write=True)}
    return {"sandbox": require_wsl, "fs": fs, "store": store, "tools": tools, "ws": workspace}


def _call(ctx, tool_name: str, **args):
    from coding_agent.tools.artifacts import FileArtifact, unpack

    text, artifact = unpack(ctx["tools"][tool_name].invoke(args))
    assert artifact is not None
    return text, FileArtifact.model_validate(artifact)


def _content(sandbox, ws, name) -> str:
    return sandbox.run(f"cat -- {shlex.quote(f'{ws}/{name}')}").stdout


# ---------------- 保存与查询 ----------------

def test_save_writes_backup_under_agent_dir(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1\n", reason="建")
    sid = ctx["store"].save(f"{ctx['ws']}/a.py", "v1\n")

    backup = f"{ctx['ws']}/.agent/backups/{sid}/a.py"
    assert ctx["sandbox"].run(f"test -f {shlex.quote(backup)}").ok
    # 留底内容就是保存时的原文
    assert ctx["sandbox"].run(f"cat -- {shlex.quote(backup)}").stdout == "v1\n"


def test_list_returns_newest_first(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1", reason="建")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v1", new_string="v2", reason="改1")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v2", new_string="v3", reason="改2")

    # 3 份：两次改动各留一份，**新建也留一份**（记的是「当时不存在」，回滚据此删除）
    entries = ctx["store"].list()
    assert len(entries) == 3
    assert entries[0].snapshot_id > entries[1].snapshot_id  # 时间戳前缀可字典序比较
    assert entries[1].snapshot_id > entries[2].snapshot_id
    assert all(e.path == "a.py" for e in entries)


def test_latest_for_picks_most_recent(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1", reason="建")
    _, first = _call(
        ctx, EDIT_TOOL_NAME, path="a.py", old_string="v1", new_string="v2", reason="改"
    )
    _, second = _call(
        ctx, EDIT_TOOL_NAME, path="a.py", old_string="v2", new_string="v3", reason="再改"
    )

    latest = ctx["store"].latest_for(f"{ctx['ws']}/a.py")
    assert latest is not None
    assert latest.snapshot_id == second.snapshot_id
    assert latest.snapshot_id != first.snapshot_id


def test_latest_for_unknown_file(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1", reason="建")
    assert ctx["store"].latest_for(f"{ctx['ws']}/nope.py") is None


def test_list_is_empty_on_fresh_workspace(ctx) -> None:
    assert ctx["store"].list() == []


# ---------------- 回滚 ----------------

def test_restore_brings_back_previous_content(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1\n", reason="建")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v1", new_string="v2", reason="改")
    assert _content(ctx["sandbox"], ctx["ws"], "a.py") == "v2\n"

    entry = ctx["store"].latest_for(f"{ctx['ws']}/a.py")
    result = ctx["store"].restore(entry)

    assert result.ok
    assert _content(ctx["sandbox"], ctx["ws"], "a.py") == "v1\n"
    assert "-v2" in result.diff and "+v1" in result.diff


def test_restore_is_itself_undoable(ctx) -> None:
    """回滚前先留底，所以一次误回滚可以再滚回来。"""
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1\n", reason="建")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v1", new_string="v2", reason="改")

    first = ctx["store"].restore(ctx["store"].latest_for(f"{ctx['ws']}/a.py"))
    assert first.undo_snapshot_id is not None
    assert _content(ctx["sandbox"], ctx["ws"], "a.py") == "v1\n"

    # 撤销「刚才那次回滚」—— 最新的快照就是回滚前留的底
    undo_entry = ctx["store"].list()[0]
    assert undo_entry.snapshot_id == first.undo_snapshot_id
    ctx["store"].restore(undo_entry)
    assert _content(ctx["sandbox"], ctx["ws"], "a.py") == "v2\n"


def test_restore_when_already_matching_is_noop(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1\n", reason="建")
    sid = ctx["store"].save(f"{ctx['ws']}/a.py", "v1\n")
    entry = ctx["store"].find(sid)

    before = len(ctx["store"].list())
    result = ctx["store"].restore(entry)

    assert result.ok
    assert result.undo_snapshot_id is None
    assert "无需回滚" in result.message
    assert len(ctx["store"].list()) == before  # 没产生多余快照


def test_restore_across_multiple_edits(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1\n", reason="建")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v1", new_string="v2", reason="改1")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v2", new_string="v3", reason="改2")

    # 指定回滚到第一次改动前的版本。列表是新到旧：[-1] 是「新建前」那份
    # （回滚它等于删掉文件，见 test_restore_of_a_creation_deletes_the_file），
    # 所以第一次改动前的那份是 [-2]。
    before_first_edit = ctx["store"].list()[-2]
    ctx["store"].restore(before_first_edit)
    assert _content(ctx["sandbox"], ctx["ws"], "a.py") == "v1\n"


def test_snapshots_are_per_file(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="a1", reason="建 a")
    _call(ctx, WRITE_TOOL_NAME, path="b.py", content="b1", reason="建 b")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="a1", new_string="a2", reason="改 a")

    a_entry = ctx["store"].latest_for(f"{ctx['ws']}/a.py")
    assert a_entry.path == "a.py"

    ctx["store"].restore(a_entry)
    assert _content(ctx["sandbox"], ctx["ws"], "a.py") == "a1"
    assert _content(ctx["sandbox"], ctx["ws"], "b.py") == "b1"  # b 不受影响


def test_newly_created_file_has_a_snapshot(ctx) -> None:
    """新建也要留底 —— 否则「回滚最近一次改动」对新建的文件根本不成立。

    留的是「当时不存在」这个事实（`.absent` 标记），所以回滚结果是删掉它，
    而不是还原成空文件留下垃圾。
    """
    _call(ctx, WRITE_TOOL_NAME, path="fresh.py", content="hi", reason="新建")
    entry = ctx["store"].latest_for(f"{ctx['ws']}/fresh.py")
    assert entry is not None
    assert ctx["store"].was_absent(entry) is True


# ---------------- file_restore 工具 ----------------

def test_restore_tool_by_path(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1\n", reason="建")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v1", new_string="v2", reason="改")

    text, artifact = _call(ctx, RESTORE_TOOL_NAME, path="a.py", reason="改错了")
    assert artifact.ok
    assert artifact.action == "restore"
    assert artifact.added == 1 and artifact.removed == 1
    assert _content(ctx["sandbox"], ctx["ws"], "a.py") == "v1\n"


def test_restore_tool_with_no_args_undoes_last_change(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1\n", reason="建")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v1", new_string="v2", reason="改")

    _, artifact = _call(ctx, RESTORE_TOOL_NAME, reason="撤销")
    assert artifact.ok
    assert _content(ctx["sandbox"], ctx["ws"], "a.py") == "v1\n"


def test_restore_tool_by_snapshot_id(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1\n", reason="建")
    _, edit = _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v1", new_string="v2", reason="改")
    _call(ctx, EDIT_TOOL_NAME, path="a.py", old_string="v2", new_string="v3", reason="再改")

    _, artifact = _call(
        ctx, RESTORE_TOOL_NAME, snapshot_id=edit.snapshot_id, path="a.py", reason="回到 v1"
    )
    assert artifact.ok
    assert _content(ctx["sandbox"], ctx["ws"], "a.py") == "v1\n"


def test_restore_tool_deletes_a_created_file(ctx) -> None:
    """回滚一次新建 = 删掉它，而不是把它清空。"""
    _call(ctx, WRITE_TOOL_NAME, path="fresh.py", content="hi\n", reason="新建")

    text, artifact = _call(ctx, RESTORE_TOOL_NAME, path="fresh.py", reason="放错位置了")
    assert artifact.ok
    assert "已删除" in text
    probe = f"test -e {shlex.quote(ctx['ws'] + '/fresh.py')} && echo yes || echo no"
    assert ctx["sandbox"].run(probe).stdout.strip() == "no"


def test_deleting_rollback_can_be_undone(ctx) -> None:
    """删除前先留了底，所以「撤销这次撤销」能把文件找回来。"""
    _call(ctx, WRITE_TOOL_NAME, path="fresh.py", content="hi\n", reason="新建")
    _call(ctx, RESTORE_TOOL_NAME, path="fresh.py", reason="放错位置了")

    _, artifact = _call(ctx, RESTORE_TOOL_NAME, reason="撤销刚才的撤销")
    assert artifact.ok
    assert _content(ctx["sandbox"], ctx["ws"], "fresh.py") == "hi\n"


def test_restore_tool_reports_unknown_snapshot(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1", reason="建")
    text, artifact = _call(ctx, RESTORE_TOOL_NAME, snapshot_id="20200101T000000-abcdef", reason="x")
    assert artifact.ok is False
    assert "找不到快照" in text


def test_restore_tool_reports_missing_backup(ctx) -> None:
    _call(ctx, WRITE_TOOL_NAME, path="a.py", content="v1", reason="建")
    text, artifact = _call(ctx, RESTORE_TOOL_NAME, path="never.py", reason="x")
    assert artifact.ok is False
    assert "没有任何留底" in text


def test_restore_tool_without_any_snapshots(ctx) -> None:
    text, artifact = _call(ctx, RESTORE_TOOL_NAME, reason="撤销")
    assert artifact.ok is False
    assert "还没有任何快照" in text


def test_restore_tool_rejects_path_escape(ctx) -> None:
    _, artifact = _call(ctx, RESTORE_TOOL_NAME, path="/etc/passwd", reason="越界")
    assert artifact.rejected is True
