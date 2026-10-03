"""集成测试：长期记忆文件。

记忆落在工作区内，所以走 SandboxFs（路径守卫同样生效）。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.memory.longterm import MAX_FACTS, LongTermMemory, MemoryError
from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace

pytestmark = pytest.mark.wsl


@pytest.fixture
def workdir(require_wsl: WslSandbox) -> str:
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-mem-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(root)}")
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


@pytest.fixture
def memory(require_wsl, workdir) -> LongTermMemory:
    return LongTermMemory(SandboxFs(require_wsl, workdir), workdir)


def _raw(require_wsl, workdir) -> str:
    return require_wsl.run(f"cat -- {shlex.quote(workdir + '/.agent/memory.md')}").stdout


# ---------------- 基本增删查 ----------------

def test_starts_empty(memory) -> None:
    assert memory.load() == []
    assert memory.render() == ""


def test_add_and_load(memory, require_wsl, workdir) -> None:
    memory.add("这个仓库用 pytest 跑测试")
    assert memory.load() == ["这个仓库用 pytest 跑测试"]
    assert "pytest" in _raw(require_wsl, workdir)


def test_add_is_idempotent(memory) -> None:
    """重复记同一条不产生第二行 —— 否则记忆会迅速被噪声填满。"""
    memory.add("约定 A")
    memory.add("约定 A")
    assert memory.load() == ["约定 A"]


def test_add_normalizes_to_single_line(memory) -> None:
    """多行事实会破坏「一行一条」的格式与人工编辑体验。"""
    memory.add("第一行\n\n第二行   带空格")
    assert memory.load() == ["第一行 第二行 带空格"]


def test_add_rejects_empty(memory) -> None:
    for bad in ("", "   ", "\n\n"):
        with pytest.raises(MemoryError):
            memory.add(bad)


def test_add_truncates_long_facts(memory) -> None:
    memory.add("x" * 1000)
    assert len(memory.load()[0]) <= 300


def test_facts_are_capped(memory, require_wsl, workdir) -> None:
    """上限来自「写满之后再加一条」这个判断，不必真的逐条写满。

    逐条 add 是 50 次「读+写」两趟进程启动 —— 一条用例就能跑几十秒。
    这里一次写入铺满，再把要测的那一步单独走一遍。
    """
    body = "\n".join(f"- 事实 {i}" for i in range(MAX_FACTS))
    target = shlex.quote(f"{workdir}/.agent/memory.md")
    require_wsl.run(f"mkdir -p {shlex.quote(workdir)}/.agent && printf %s "
                    f"{shlex.quote(body)} > {target}")

    assert len(memory.load()) == MAX_FACTS

    with pytest.raises(MemoryError, match="上限"):
        memory.add("再来一条")
    assert len(memory.load()) == MAX_FACTS


def test_remove_by_one_based_index(memory) -> None:
    memory.add("第一条")
    memory.add("第二条")
    memory.add("第三条")

    remaining = memory.remove(2)
    assert remaining == ["第一条", "第三条"]


@pytest.mark.parametrize("index", [0, -1, 5])
def test_remove_out_of_range(memory, index: int) -> None:
    memory.add("只有一条")
    with pytest.raises(MemoryError, match="序号超出范围"):
        memory.remove(index)


def test_clear(memory) -> None:
    memory.add("a")
    memory.add("b")
    assert memory.clear() == []
    assert memory.load() == []


# ---------------- 人工可编辑 ----------------

def test_handwritten_file_is_parsed(memory, require_wsl, workdir) -> None:
    """用户可以手工编辑这个文件，注释与空行要被忽略。"""
    content = (
        "# 项目记忆\n"
        "#\n"
        "# 随便写的说明\n"
        "\n"
        "- 这个仓库用 uv 管理依赖\n"
        "\n"
        "- 不要动 legacy/ 目录\n"
        "不是以短横线开头的行应被忽略\n"
    )
    path = shlex.quote(f"{workdir}/.agent/memory.md")
    require_wsl.run(f"mkdir -p {shlex.quote(workdir + '/.agent')} && printf %s "
                    f"{shlex.quote(content)} > {path}")

    assert memory.load() == ["这个仓库用 uv 管理依赖", "不要动 legacy/ 目录"]


def test_rewrite_preserves_header(memory, require_wsl, workdir) -> None:
    memory.add("第一条")
    raw = _raw(require_wsl, workdir)
    assert raw.startswith("# 项目记忆")
    assert "- 第一条" in raw


# ---------------- 提示词渲染 ----------------

def test_render_lists_facts(memory) -> None:
    memory.add("用 pytest")
    memory.add("别动 legacy/")
    rendered = memory.render()
    assert "- 用 pytest" in rendered
    assert "- 别动 legacy/" in rendered


def test_render_empty_when_no_facts(memory) -> None:
    assert memory.render() == ""
