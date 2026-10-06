"""集成测试：文件工具的真实沙箱行为。

覆盖「精确替换 / 写前备份 / diff / 路径与符号链接守卫」——
这是选题四个创新点里「双工具架构」与「可回滚的修改流程」的落地验证。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest
from pydantic import ValidationError

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
    # 新建也要留底：否则「回滚最近一次改动」对新建的文件不成立（B3）
    assert artifact.snapshot_id
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


def test_read_offset_beyond_end_is_not_reported_as_empty(tools) -> None:
    """offset 越界时不能说「文件为空」—— 模型会据此以为可以整份覆盖写入。"""
    _invoke(tools[WRITE_TOOL_NAME], path="a.txt", content="1\n2\n3\n", reason="准备")
    text, artifact = _invoke(tools[READ_TOOL_NAME], path="a.txt", reason="越界读", offset=99)

    assert artifact.ok
    assert artifact.lines_total == 3
    assert artifact.lines_read == 0
    assert "文件为空" not in text
    assert "没有内容可显示" in text


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


def test_edit_rejects_empty_old_string(tools, require_wsl, workspace) -> None:
    """空 old_string 命中 `str.replace("", x)` 的逐字符插入语义，必须在 schema 挡住。"""
    _invoke(tools[WRITE_TOOL_NAME], path="a.py", content="value = 1\n", reason="准备")

    with pytest.raises(ValidationError):
        tools[EDIT_TOOL_NAME].invoke(
            {"path": "a.py", "old_string": "", "new_string": "#", "reason": "空串"}
        )

    assert _read_raw(require_wsl, f"{workspace}/a.py") == "value = 1\n"


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


def test_dangling_symlink_escape_is_rejected(tools, require_wsl, workspace) -> None:
    """悬空符号链接的逃逸：`-e` 对其为假。

    只按存在性判断会跳过 realpath 校验，再顺着链接把内容写到工作区外 ——
    这是「只做词法校验不够」的第二个变体，专门守住 `-e || -L` 这条判定。
    """
    outside = f"/tmp/agent-outside-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(outside)}")
    # 目标文件故意不创建，制造悬空链接
    require_wsl.run(
        f"ln -s {shlex.quote(outside + '/created.txt')} {shlex.quote(workspace + '/dangling')}"
    )
    try:
        text, artifact = _invoke(
            tools[WRITE_TOOL_NAME], path="dangling", content="pwned", reason="悬空链接穿透"
        )
        assert artifact.rejected, text
        # 工作区外必须没有被创建出文件
        leaked = require_wsl.run(
            f"test -e {shlex.quote(outside + '/created.txt')} && echo leaked || echo safe"
        )
        assert "leaked" not in leaked.stdout
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


def test_read_of_oversized_file_points_at_offset_limit(tools, settings) -> None:
    """file_read 才有 offset/limit，退路只能由它自己给。"""
    _invoke(
        tools[WRITE_TOOL_NAME],
        path="big.txt",
        content="x" * (settings.max_file_read_bytes + 10),
        reason="准备",
    )
    text, artifact = _invoke(tools[READ_TOOL_NAME], path="big.txt", reason="读")
    assert artifact.ok is False
    assert "offset/limit" in text


def test_edit_works_beyond_the_context_read_limit(tools, settings) -> None:
    """B4：file_edit 的读取不进上下文，不该被「喂给模型」的读限卡住。

    曾经 2MB 以上的文件根本改不了，而错误又让模型去用并不存在的 offset/limit，
    只能退回 shell —— 经 shell 的改动不留快照，绕开了可回滚这条底线。
    """
    body = "keep\n" + "y" * (settings.max_file_read_bytes + 1000) + "\nkeep\n"
    _invoke(tools[WRITE_TOOL_NAME], path="big.txt", content=body, reason="准备")

    text, artifact = _invoke(
        tools[EDIT_TOOL_NAME], path="big.txt", old_string="keep\n", new_string="KEEP\n",
        reason="改大文件", replace_all=True,
    )
    assert artifact.ok, text
    assert artifact.action == "edit"


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


def test_fs_oversized_file_is_not_read_in_shell(require_wsl, tools, workspace) -> None:
    """上限要在脚本内生效：超限时 stdout 里根本不能出现 DATA。

    读后拒是不够的 —— base64 已经把整份文件物化进内存了。
    """
    _invoke(tools[WRITE_TOOL_NAME], path="big.txt", content="x" * 500, reason="准备")
    fs = SandboxFs(require_wsl, workspace)
    path = f"{workspace}/big.txt"

    over = require_wsl.run(fs._stat_script(path, include_data=True, max_bytes=100))
    assert "DATA=" not in over.stdout

    under = require_wsl.run(fs._stat_script(path, include_data=True, max_bytes=10_000))
    assert "DATA=" in under.stdout


def test_fs_read_empty_file_returns_empty(require_wsl, tools, workspace) -> None:
    """空文件应当读成空内容，而不是被当成「没取到内容」的失败。

    `file_read` 的「（文件为空）」分支此前是死代码 —— 空文件在 SandboxFs 层就已报错。
    """
    require_wsl.run(f": > {shlex.quote(workspace + '/empty.txt')}")
    fs = SandboxFs(require_wsl, workspace)
    assert fs.read_text(f"{workspace}/empty.txt", max_bytes=1000) == ""

    text, artifact = _invoke(
        tools[READ_TOOL_NAME], path="empty.txt", reason="读空文件"
    )
    assert artifact.ok, text
    assert "文件为空" in text



# ---------------- 批量物化（write_many） ----------------
#
# 评测任务的种子有几十个文件，而 write_text 是"一个文件一次 wsl.exe"。
# 这里钉住两件事：**一次调用能写完一整批**，以及**越界时一个字节都不写**
# （逐个写做不到后者：写到第 7 个才发现越界时，前 6 个已经落盘了）。

def test_write_many_creates_a_whole_batch_in_one_call(
    require_wsl: WslSandbox, workspace: str
) -> None:
    fs = SandboxFs(require_wsl, workspace)
    files = {
        f"{workspace}/app/models.py": "class Item:\n    pass\n",
        f"{workspace}/app/handlers/orders.py": "def handle():\n    return 1\n",
        f"{workspace}/tests/__init__.py": "",
        f"{workspace}/deep/deeper/note.txt": "中文内容\n",
    }

    written = fs.write_many(files)

    assert written == sum(len(c.encode("utf-8")) for c in files.values())
    for path, content in files.items():
        assert fs.read_text(path, max_bytes=100_000) == content
    # 空文件确实被创建了，而不是被跳过
    assert require_wsl.run(f"[ -f {shlex.quote(workspace + '/tests/__init__.py')} ]").ok


def test_write_many_refuses_the_whole_batch_when_any_path_escapes(
    require_wsl: WslSandbox, workspace: str
) -> None:
    """全有或全无：越界时不能留下半成品工作区。"""
    from coding_agent.sandbox.pathguard import SandboxPathError

    fs = SandboxFs(require_wsl, workspace)

    with pytest.raises(SandboxPathError):
        fs.write_many({f"{workspace}/innocent.py": "x = 1\n", "/etc/agent-evil": "boom\n"})

    assert not require_wsl.run(f"[ -e {shlex.quote(workspace + '/innocent.py')} ]").ok


def test_write_many_rejects_a_symlinked_escape(
    require_wsl: WslSandbox, workspace: str
) -> None:
    """宿主侧的词法校验挡不住符号链接，必须由脚本内的 realpath 校验兜住。"""
    from coding_agent.sandbox.pathguard import SandboxPathError

    fs = SandboxFs(require_wsl, workspace)
    require_wsl.run(f"ln -sfn /etc {shlex.quote(workspace + '/link')}")

    with pytest.raises(SandboxPathError):
        fs.write_many({f"{workspace}/link/agent-evil.conf": "boom\n"})

    assert not require_wsl.run("[ -e /etc/agent-evil.conf ]").ok


def test_write_many_with_nothing_to_do_is_a_noop(
    require_wsl: WslSandbox, workspace: str
) -> None:
    fs = SandboxFs(require_wsl, workspace)
    assert fs.write_many({}) == 0
