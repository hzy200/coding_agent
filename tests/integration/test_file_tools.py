"""集成测试：文件工具的真实沙箱行为。

覆盖「精确替换 / 写前备份 / diff / 路径与符号链接守卫」——
这是选题四个创新点里「双工具架构」与「可回滚的修改流程」的落地验证。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import FileArtifact, unpack
from coding_agent.tools.files import (
    EDIT_TOOL_NAME,
    READ_TOOL_NAME,
    WRITE_TOOL_NAME,
    build_file_tools,
)

pytestmark = pytest.mark.wsl


@pytest.fixture
def workspace(require_wsl: WslSandbox) -> str:
    """每个用例一个干净的工作区，用完删掉。"""
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-test-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(root)}")
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


@pytest.fixture
def tools(require_wsl: WslSandbox, workspace: str, settings) -> dict:
    scoped = settings.model_copy(update={"wsl_workspace": workspace})
    built = build_file_tools(scoped, require_wsl, allow_write=True)
    return {tool.name: tool for tool in built}


def _invoke(tool, **args) -> tuple[str, FileArtifact]:
    text, artifact = unpack(tool.invoke(args))
    assert artifact is not None, "文件工具必须返回 artifact"
    return text, FileArtifact.model_validate(artifact)


def _read_raw(require_wsl: WslSandbox, path: str) -> str:
    result = require_wsl.run(f"cat -- {shlex.quote(path)}")
    assert result.ok, result.render(500)
    return result.stdout


# ---------------- 基础读写 ----------------

def test_write_creates_file_with_exact_bytes(require_wsl, tools, workspace) -> None:
    text, artifact = _invoke(
        tools[WRITE_TOOL_NAME],
        path="src/pkg/mod.py",
        content="# 注释带中文\nvalue = 1\n",
        reason="新建模块",
    )
    assert artifact.ok
    assert artifact.action == "create"
    assert artifact.snapshot_id is None  # 新建无需备份
    assert _read_raw(require_wsl, f"{workspace}/src/pkg/mod.py") == "# 注释带中文\nvalue = 1\n"
    assert "已创建" in text


def test_write_preserves_awkward_content(require_wsl, tools, workspace) -> None:
    """内容经 base64 落盘，引号/反斜杠/命令替换符都不应被 shell 解释。"""
    nasty = 'echo "$(whoami)" `id` \\n \'quoted\' && rm -rf /\n'
    _invoke(tools[WRITE_TOOL_NAME], path="nasty.sh", content=nasty, reason="验证转义")
    assert _read_raw(require_wsl, f"{workspace}/nasty.sh") == nasty


def test_read_returns_numbered_lines(tools) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="a.txt", content="alpha\nbeta\ngamma\n", reason="准备")
    text, artifact = _invoke(tools[READ_TOOL_NAME], path="a.txt", reason="查看")
    assert artifact.ok
    assert artifact.lines_total == 3
    assert artifact.lines_read == 3
    assert "1\talpha" in text
    assert "3\tgamma" in text


def test_read_offset_and_limit(tools) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="a.txt", content="1\n2\n3\n4\n5\n", reason="准备")
    text, artifact = _invoke(
        tools[READ_TOOL_NAME], path="a.txt", reason="分段读", offset=2, limit=2
    )
    assert artifact.lines_read == 2
    assert "2\t2" in text
    assert "3\t3" in text
    assert "显示第 2-3 行" in text


def test_read_missing_file(tools) -> None:
    _, artifact = _invoke(tools[READ_TOOL_NAME], path="nope.txt", reason="不存在")
    assert not artifact.ok
    assert not artifact.rejected


def test_read_rejects_binary(tools, require_wsl, workspace) -> None:
    require_wsl.run(f"printf 'a\\x00b' > {shlex.quote(workspace + '/bin.dat')}")
    text, artifact = _invoke(tools[READ_TOOL_NAME], path="bin.dat", reason="二进制")
    assert not artifact.ok
    assert "二进制" in text


# ---------------- 精确替换 ----------------

def test_edit_replaces_unique_occurrence(tools, require_wsl, workspace) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="x = 1\ny = 2\n", reason="准备")
    text, artifact = _invoke(
        tools[EDIT_TOOL_NAME],
        path="a.py",
        old_string="x = 1",
        new_string="x = 42",
        reason="改初值",
    )
    assert artifact.ok
    assert artifact.action == "edit"
    assert (artifact.added, artifact.removed) == (1, 1)
    assert artifact.snapshot_id  # 覆盖已有文件必须先备份
    assert _read_raw(require_wsl, f"{workspace}/a.py") == "x = 42\ny = 2\n"
    assert "-x = 1" in text and "+x = 42" in text


def test_edit_refuses_when_old_string_absent(tools, require_wsl, workspace) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="x = 1\n", reason="准备")
    text, artifact = _invoke(
        tools[EDIT_TOOL_NAME], path="a.py", old_string="nope", new_string="y", reason="找不到"
    )
    assert not artifact.ok
    assert artifact.action == "edit"
    assert "未出现" in text
    # 文件必须原样未动
    assert _read_raw(require_wsl, f"{workspace}/a.py") == "x = 1\n"


def test_edit_refuses_ambiguous_match(tools, require_wsl, workspace) -> None:
    """歧义替换是精确编辑最典型的翻车点，必须拒绝而不是猜。"""
    _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="v = 1\nv = 1\n", reason="准备")
    text, artifact = _invoke(
        tools[EDIT_TOOL_NAME], path="a.py", old_string="v = 1", new_string="v = 2", reason="歧义"
    )
    assert not artifact.ok
    assert "出现了 2 次" in text
    assert "replace_all" in text
    assert _read_raw(require_wsl, f"{workspace}/a.py") == "v = 1\nv = 1\n"


def test_edit_replace_all_when_explicit(tools, require_wsl, workspace) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="v = 1\nv = 1\n", reason="准备")
    _, artifact = _invoke(
        tools[EDIT_TOOL_NAME],
        path="a.py",
        old_string="v = 1",
        new_string="v = 2",
        reason="全部替换",
        replace_all=True,
    )
    assert artifact.ok
    assert _read_raw(require_wsl, f"{workspace}/a.py") == "v = 2\nv = 2\n"


def test_edit_noop_is_reported(tools) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="x = 1\n", reason="准备")
    _, artifact = _invoke(
        tools[EDIT_TOOL_NAME], path="a.py", old_string="x = 1", new_string="x = 1", reason="无变化"
    )
    assert not artifact.ok
    assert not artifact.rejected


def test_edit_detects_repeated_run_after_previous_edit(tools, require_wsl, workspace) -> None:
    """连续编辑：第二次应基于第一次的结果，且各自产生独立备份。"""
    _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="a = 1\nb = 1\n", reason="准备")
    _, first = _invoke(
        tools[EDIT_TOOL_NAME], path="a.py", old_string="a = 1", new_string="a = 2", reason="第一次"
    )
    _, second = _invoke(
        tools[EDIT_TOOL_NAME], path="a.py", old_string="b = 1", new_string="b = 2", reason="第二次"
    )
    assert first.ok and second.ok
    assert first.snapshot_id != second.snapshot_id
    assert _read_raw(require_wsl, f"{workspace}/a.py") == "a = 2\nb = 2\n"


# ---------------- 备份 ----------------

def test_backup_holds_original_content(tools, require_wsl, workspace) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="original\n", reason="准备")
    _, artifact = _invoke(
        tools[EDIT_TOOL_NAME], path="a.py", old_string="original", new_string="changed", reason="改"
    )
    backup = f"{workspace}/.agent/backups/{artifact.snapshot_id}/a.py"
    assert _read_raw(require_wsl, backup) == "original\n"


def test_overwrite_backs_up_previous_content(tools, require_wsl, workspace) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="v1\n", reason="准备")
    _, artifact = _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="v2\n", reason="覆盖")
    assert artifact.action == "overwrite"
    backup = f"{workspace}/.agent/backups/{artifact.snapshot_id}/a.py"
    assert _read_raw(require_wsl, backup) == "v1\n"


# ---------------- 路径守卫 ----------------

def test_relative_path_resolves_against_workspace(tools, require_wsl, workspace) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="sub/a.txt", content="hi", reason="相对路径")
    assert _read_raw(require_wsl, f"{workspace}/sub/a.txt") == "hi"


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "~/secret"])
def test_path_escape_is_rejected(tools, path: str) -> None:
    _, artifact = _invoke(tools[WRITE_TOOL_NAME], path=path, content="x", reason="越界")
    assert artifact.rejected
    assert not artifact.ok


def test_symlink_escape_is_rejected(tools, require_wsl, workspace) -> None:
    """词法校验挡不住的逃逸：工作区内的符号链接指向外部。

    这是 realpath 二次校验存在的唯一理由。
    """
    outside = f"/tmp/agent-outside-{uuid4().hex[:8]}"
    secret = shlex.quote(f"{outside}/s.txt")
    require_wsl.run(f"mkdir -p {shlex.quote(outside)} && echo secret > {secret}")
    require_wsl.run(f"ln -s {shlex.quote(outside)} {shlex.quote(workspace + '/link')}")
    try:
        text, artifact = _invoke(tools[READ_TOOL_NAME], path="link/s.txt", reason="穿透符号链接")
        assert artifact.rejected, text
        assert "符号链接" in text

        _, write_artifact = _invoke(
            tools[WRITE_TOOL_NAME], path="link/s.txt", content="pwned", reason="写入穿透"
        )
        assert write_artifact.rejected
        # 外部文件必须原封不动
        assert _read_raw(require_wsl, f"{outside}/s.txt") == "secret\n"
    finally:
        require_wsl.run(f"rm -rf {shlex.quote(outside)}")


def test_symlink_inside_workspace_is_allowed(tools, require_wsl, workspace) -> None:
    """只禁止逃逸，不禁止工作区内部的正常符号链接。"""
    _invoke(tools[WRITE_TOOL_NAME], path="real/a.txt", content="inside", reason="准备")
    require_wsl.run(f"ln -s {shlex.quote(workspace + '/real')} {shlex.quote(workspace + '/alias')}")
    text, artifact = _invoke(tools[READ_TOOL_NAME], path="alias/a.txt", reason="内部链接")
    assert artifact.ok, text
    assert "inside" in text


# ---------------- 只读模式 ----------------

def test_write_tools_absent_in_read_only_mode(require_wsl, workspace, settings) -> None:
    scoped = settings.model_copy(update={"wsl_workspace": workspace})
    names = {tool.name for tool in build_file_tools(scoped, require_wsl, allow_write=False)}
    assert names == {READ_TOOL_NAME}


# ---------------- SandboxFs 单元 ----------------

def test_fs_stat_reports_nonexistent_path(require_wsl, workspace) -> None:
    fs = SandboxFs(require_wsl, workspace)
    info = fs.stat(f"{workspace}/ghost.txt")
    assert not info.exists
    assert info.real_path.endswith("ghost.txt")


def test_fs_read_respects_size_limit(require_wsl, tools, workspace) -> None:
    _invoke(tools[WRITE_TOOL_NAME], path="big.txt", content="x" * 500, reason="准备")
    fs = SandboxFs(require_wsl, workspace)
    with pytest.raises(Exception, match="文件过大"):
        fs.read_text(f"{workspace}/big.txt", max_bytes=100)
