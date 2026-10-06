"""集成测试：Git 工具在真实仓库上的行为。

重点是「参数不能逃逸」—— 提交信息与路径由宿主逐参数引用，
模型无法借它们拼出第二条命令。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import ShellArtifact, unpack
from coding_agent.tools.git import (
    GIT_ADD,
    GIT_COMMIT,
    GIT_DIFF,
    GIT_LOG,
    GIT_STATUS,
    build_git_tools,
)

pytestmark = pytest.mark.wsl


@pytest.fixture
def repo(require_wsl: WslSandbox) -> str:
    """一个已初始化的临时仓库，配好 user.name/email。"""
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-git-{uuid4().hex[:8]}"
    require_wsl.run(
        f"mkdir -p {shlex.quote(root)} && cd {shlex.quote(root)} && git init -q "
        f"&& git config user.email agent@example.com && git config user.name Agent"
    )
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


@pytest.fixture
def tools(require_wsl: WslSandbox, repo: str, settings) -> dict:
    scoped = settings.model_copy(update={"wsl_workspace": repo})
    return {tool.name: tool for tool in build_git_tools(scoped, require_wsl)}


def _invoke(tool, **args) -> tuple[str, ShellArtifact]:
    text, artifact = unpack(tool.invoke(args))
    assert artifact is not None
    return text, ShellArtifact.model_validate(artifact)


def _write(require_wsl: WslSandbox, repo: str, name: str, content: str) -> None:
    target = shlex.quote(f"{repo}/{name}")
    require_wsl.run(f"printf %s {shlex.quote(content)} > {target}")


def test_status_on_clean_repo(tools) -> None:
    text, artifact = _invoke(tools[GIT_STATUS], reason="看状态")
    assert artifact.ok
    assert "##" in text  # --branch 会带上分支行


def test_status_shows_untracked(tools, require_wsl, repo) -> None:
    _write(require_wsl, repo, "a.txt", "hello")
    text, _ = _invoke(tools[GIT_STATUS], reason="看状态")
    assert "a.txt" in text


def test_add_then_commit(tools, require_wsl, repo) -> None:
    _write(require_wsl, repo, "a.txt", "hello")

    _, added = _invoke(tools[GIT_ADD], reason="暂存", paths=["a.txt"])
    assert added.ok
    assert added.level_label == "L1 低风险写"

    text, committed = _invoke(tools[GIT_COMMIT], reason="提交", message="add a.txt")
    assert committed.ok, text
    assert committed.level_label == "L2 变更性"

    log, _ = _invoke(tools[GIT_LOG], reason="看历史", limit=5)
    assert "add a.txt" in log


def test_diff_shows_changes(tools, require_wsl, repo) -> None:
    _write(require_wsl, repo, "a.txt", "hello")
    _invoke(tools[GIT_ADD], reason="暂存", paths=["a.txt"])
    _invoke(tools[GIT_COMMIT], reason="提交", message="first")
    _write(require_wsl, repo, "a.txt", "changed")

    text, artifact = _invoke(tools[GIT_DIFF], reason="看改动")
    assert artifact.ok
    assert "-hello" in text and "+changed" in text


def test_diff_staged_only_shows_index(tools, require_wsl, repo) -> None:
    _write(require_wsl, repo, "a.txt", "v1")
    _invoke(tools[GIT_ADD], reason="暂存", paths=["a.txt"])
    _invoke(tools[GIT_COMMIT], reason="提交", message="v1")
    _write(require_wsl, repo, "a.txt", "v2")
    _invoke(tools[GIT_ADD], reason="暂存", paths=["a.txt"])
    _write(require_wsl, repo, "a.txt", "v3")

    staged, _ = _invoke(tools[GIT_DIFF], reason="看已暂存", staged=True)
    assert "+v2" in staged and "v3" not in staged

    working, _ = _invoke(tools[GIT_DIFF], reason="看工作区")
    assert "v3" in working


# ---------------- 参数不能逃逸 ----------------

def test_commit_message_is_not_shell_interpreted(tools, require_wsl, repo) -> None:
    """提交信息里的引号/分号/命令替换必须是字面量。"""
    _write(require_wsl, repo, "a.txt", "x")
    _invoke(tools[GIT_ADD], reason="暂存", paths=["a.txt"])

    sentinel = f"/tmp/agent-pwned-{uuid4().hex[:8]}"
    nasty = f'fix"; touch {sentinel}; echo "'
    text, artifact = _invoke(tools[GIT_COMMIT], reason="提交", message=nasty)
    assert artifact.ok, text

    # 注入的命令绝不能被执行
    probe = require_wsl.run(f"test -e {shlex.quote(sentinel)} && echo EXISTS || echo ABSENT")
    assert "ABSENT" in probe.stdout

    # 提交信息应当逐字保留
    shown = require_wsl.run(f"cd {shlex.quote(repo)} && git log -1 --pretty=%s")
    assert nasty in shown.stdout


def test_commit_message_with_newlines_is_preserved(tools, require_wsl, repo) -> None:
    _write(require_wsl, repo, "a.txt", "x")
    _invoke(tools[GIT_ADD], reason="暂存", paths=["a.txt"])
    _, artifact = _invoke(tools[GIT_COMMIT], reason="提交", message="标题\n\n正文说明")
    assert artifact.ok

    body = require_wsl.run(f"cd {shlex.quote(repo)} && git log -1 --pretty=%B")
    assert "标题" in body.stdout and "正文说明" in body.stdout


def test_paths_cannot_escape_workspace(tools, require_wsl, repo) -> None:
    """git add 的路径也走路径守卫。"""
    outside = f"/tmp/agent-outside-git-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(outside)} && touch {shlex.quote(outside + '/x.txt')}")
    try:
        text, artifact = _invoke(
            tools[GIT_ADD], reason="越界", paths=[f"{outside}/x.txt"]
        )
        assert artifact.rejected or "路径非法" in text
    finally:
        require_wsl.run(f"rm -rf {shlex.quote(outside)}")


def test_add_rejects_empty_paths(tools) -> None:
    text, artifact = _invoke(tools[GIT_ADD], reason="空的", paths=[])
    assert artifact.ok is False
    assert "不能为空" in text


def test_commit_rejects_blank_message(tools) -> None:
    text, artifact = _invoke(tools[GIT_COMMIT], reason="空消息", message="   ")
    assert artifact.rejected is True
    assert "不能为空" in text


# ---------------- agent 自己的工作目录不许进版本控制 ----------------

def test_add_refuses_agent_state_directory(tools, require_wsl, repo) -> None:
    require_wsl.run(f"mkdir -p {shlex.quote(repo + '/.agent/backups/x')}")
    require_wsl.run(f"touch {shlex.quote(repo + '/.agent/backups/x/a.py')}")

    text, artifact = _invoke(tools[GIT_ADD], reason="别提交这个", paths=[".agent/backups"])
    assert artifact.rejected is True
    assert "agent 自己的工作目录" in text

    # 也没被暂存
    status = require_wsl.run(f"cd {shlex.quote(repo)} && git diff --cached --name-only")
    assert ".agent" not in status.stdout


def test_commit_refuses_when_agent_state_already_staged(tools, require_wsl, repo) -> None:
    """用 shell 的 git add -A 绕过 add 工具时，提交这道闸仍然拦得住。"""
    require_wsl.run(f"mkdir -p {shlex.quote(repo + '/.agent/backups')}")
    require_wsl.run(f"touch {shlex.quote(repo + '/.agent/backups/a.py')}")
    _write(require_wsl, repo, "real.txt", "content")
    require_wsl.run(f"cd {shlex.quote(repo)} && git add -A")

    text, artifact = _invoke(tools[GIT_COMMIT], reason="提交", message="should be blocked")
    assert artifact.rejected is True
    assert "暂存区里有 agent 自己的工作目录文件" in text

    log = require_wsl.run(f"cd {shlex.quote(repo)} && git log --oneline")
    assert "should be blocked" not in log.stdout


def test_commit_proceeds_when_agent_state_is_untracked(tools, require_wsl, repo) -> None:
    """只是未跟踪、没被暂存时不应妨碍正常提交。"""
    require_wsl.run(f"mkdir -p {shlex.quote(repo + '/.agent/backups')}")
    _write(require_wsl, repo, "real.txt", "content")
    _invoke(tools[GIT_ADD], reason="暂存", paths=["real.txt"])

    _, artifact = _invoke(tools[GIT_COMMIT], reason="提交", message="normal commit")
    assert artifact.ok, artifact


def test_cwd_outside_workspace_is_rejected(tools) -> None:
    text, artifact = _invoke(tools[GIT_STATUS], reason="越界目录", cwd="/etc")
    assert artifact.rejected is True
    assert "工作目录非法" in text


def test_git_failure_is_reported_not_raised(require_wsl, settings) -> None:
    """在非仓库的工作区里执行 git 应当失败但可读，而不是抛异常。"""
    plain = f"{resolve_workspace(settings, require_wsl)}-notrepo-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(plain)}")
    try:
        scoped = settings.model_copy(update={"wsl_workspace": plain})
        tools = {t.name: t for t in build_git_tools(scoped, require_wsl)}
        text, artifact = _invoke(tools[GIT_STATUS], reason="非仓库")
        assert artifact.ok is False
        assert "not a git repository" in text.lower()
    finally:
        require_wsl.run(f"rm -rf {shlex.quote(plain)}")


def test_add_refuses_dot_and_the_workspace_root(tools, require_wsl, repo) -> None:
    """A7：目标**是 `.agent/` 的祖先**时也要拒。

    `git_add(".")` 经 `ensure_inside(".")` 正好归一化成工作区根，而
    `git add -- <工作区根>` 会把 `.agent/` 里的备份与审计一并暂存 ——
    原先只判"路径在 `.agent/` 里"，`.` 与工作区根整个绕过了过滤。
    """
    _write(require_wsl, repo, "app.py", "x = 1\n")
    require_wsl.run(f"mkdir -p {shlex.quote(repo + '/.agent/backups')}")
    _write(require_wsl, repo, ".agent/backups/bak", "备份内容\n")

    for path in (".", repo):
        text, artifact = _invoke(tools[GIT_ADD], paths=[path], reason="整树暂存")
        assert not artifact.ok, f"{path!r} 应当被拒绝"
        assert "agent 自己的工作目录" in text

    # 明确列出文件仍然可以
    _, ok = _invoke(tools[GIT_ADD], paths=["app.py"], reason="只加源码")
    assert ok.ok
