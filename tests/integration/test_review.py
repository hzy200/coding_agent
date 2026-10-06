"""代码审查在**真实沙箱**里的行为。

单测（`tests/unit/test_review.py`）已经把规则逻辑钉住了；这里补的是那一层
测不到的东西：

1. 脚本要经 `sandbox.run` → `wrap_with_limits` 的内层 heredoc → 内层
   `python3 - <<'EOF'` 的**嵌套 heredoc** 才跑得起来 —— 这条链路只有真跑才知道。
2. 水位线跨"步骤"的行为：不重复报同一个问题（这是最容易写成噪声的地方）。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.review import run_review

pytestmark = pytest.mark.wsl


@pytest.fixture
def workdir(require_wsl: WslSandbox) -> str:
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-rev-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(root)}")
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


def _put(require_wsl: WslSandbox, target: str, content: str) -> None:
    """把内容写到沙箱里的某个绝对路径（自动建父目录）。"""
    parent = target.rsplit("/", 1)[0]
    require_wsl.run(
        f"mkdir -p {shlex.quote(parent)} && "
        f"printf %s {shlex.quote(content)} > {shlex.quote(target)}"
    )


def _snapshot(require_wsl: WslSandbox, root: str, sid: str, relpath: str, content: str) -> None:
    """按 SnapshotStore 的布局手工造一份留底。"""
    _put(require_wsl, f"{root}/.agent/backups/{sid}/{relpath}", content)


def _write(require_wsl: WslSandbox, root: str, relpath: str, content: str) -> None:
    _put(require_wsl, f"{root}/{relpath}", content)


SID_1 = "20260101T000000000000000-aaaaaa"
SID_2 = "20260102T000000000000000-bbbbbb"


def test_script_runs_through_the_nested_heredoc(require_wsl, workdir) -> None:
    """脚本从宿主经 heredoc 传给 bash，里面又有 heredoc 交给 python3。

    这条链路上任何一层出问题（引号、长度、结束标记），失败形态都是"审查静默不跑"。
    """
    _snapshot(require_wsl, workdir, SID_1, "a.py", "def f():\n    return 1\n")
    _write(require_wsl, workdir, "a.py", "def f():\n    breakpoint()\n    return 1\n")

    result = run_review(require_wsl, workdir, watermark="", enable_linters=False)

    assert result.checked_files == 1
    assert result.status == "blocked"
    assert any(f.rule == "debug-breakpoint" for f in result.blocking_findings)
    assert result.watermark == SID_1


def test_long_content_survives_the_transport(require_wsl, workdir) -> None:
    """内容一大就会撞上 argv / heredoc 的那些坑（本项目刚修过一例）。

    这里放一份远超命令行长度上限的内容，确认审查仍能跑完 —— 它是"写入不再受
    argv 上限卡住"那条修复的消费端。
    """
    body = "x = 1\n" * 40_000  # 约 320 KB
    _snapshot(require_wsl, workdir, SID_1, "big.py", body)
    _write(require_wsl, workdir, "big.py", body + "print('tail')\n")

    result = run_review(require_wsl, workdir, watermark="", enable_linters=False)

    assert result.status == "warned"
    assert any(f.rule == "debug-print" for f in result.findings)


def test_watermark_stops_the_same_findings_from_repeating(require_wsl, workdir) -> None:
    """跨"步骤"不重复告警 —— 这条写错就会变成纯粹的噪声源。

    留底记的是**写前内容**，所以上一步改过的文件会永远与自己的留底不同。
    水位线就是为这件事存在的：第二次审查必须看不到第一次已经报过的问题。
    """
    _snapshot(require_wsl, workdir, SID_1, "a.py", "def f():\n    return 1\n")
    _write(require_wsl, workdir, "a.py", "def f():\n    breakpoint()\n")

    first = run_review(require_wsl, workdir, watermark="", enable_linters=False)
    assert first.status == "blocked"

    # 第二步：又改了一个文件，但第一步的问题还在工作区里
    _write(require_wsl, workdir, "b.py", "print('new')\n")
    _snapshot(require_wsl, workdir, SID_2, "b.py.absent", "")

    second = run_review(require_wsl, workdir, watermark=first.watermark, enable_linters=False)

    # 只报 b.py 的新增，不重报 a.py 里那个已经报过的 breakpoint
    assert second.checked_files == 1
    assert not any(f.rule == "debug-breakpoint" for f in second.findings)
    assert any(f.rule == "debug-print" for f in second.findings)


def test_a_broken_workspace_does_not_crash_the_run(require_wsl, workdir) -> None:
    """审查目录不存在时也要给出结论，而不是抛异常打断整轮运行。"""
    result = run_review(require_wsl, workdir, watermark="", enable_linters=False)

    assert result.status in ("clean", "warned")
    assert result.checked_files == 0


def test_linters_are_probed_not_assumed(require_wsl, workdir) -> None:
    """沙箱里有没有 ruff/mypy 是**探测**出来的，不是假定的。

    本机实测两者都没有 —— 这时必须如实"没跑 linter"，而不是假装审过。
    """
    from coding_agent.tools.review import available_linters

    _snapshot(require_wsl, workdir, SID_1, "a.py", "x = 1\n")
    _write(require_wsl, workdir, "a.py", "x = 2\n")

    result = run_review(require_wsl, workdir, watermark="", enable_linters=True)

    assert result.linters == available_linters(require_wsl)
    assert result.status == "clean"  # 改个常量不该产生任何发现
