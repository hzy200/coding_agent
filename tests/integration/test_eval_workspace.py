"""评测工作区的物化契约：种子的内容与 **git 仓库状态**。

为什么要测 git：实测里 `git_status` / `git_diff` 在长程任务中全部失败 ——
工作区压根不是仓库。于是「交付与合并」那一环（`git_add` / `git_commit`）在长程档
**根本测不到**，而它是六阶段需求里的最后一段。

这条容易悄悄坏掉：git 初始化失败时 `git_*` 工具会**静默地**失效，任务看起来像
"模型不会用 git"，而不是"环境没准备好"。所以这里正面钉住。
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace

# `tests/eval` 不是包（刻意：它是独立脚本目录），按路径挂进来
_EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from harness import EvalTask, clean_workspace, eval_root, materialize  # noqa: E402

pytestmark = pytest.mark.wsl

TASK = EvalTask(
    id="_git_probe",
    category="single_file",
    prompt="（工作区自检用，不会交给智能体）",
    sources={"pkg/__init__.py": "", "pkg/mod.py": "def f():\n    return 1\n"},
    tests={
        "tests/__init__.py": "",
        "tests/test_mod.py": (
            "import unittest\n\nfrom pkg.mod import f\n\n\n"
            "class T(unittest.TestCase):\n    def test_f(self):\n        self.assertEqual(f(), 1)\n"
        ),
    },
)


@pytest.fixture
def workspace(require_wsl: WslSandbox):
    fs = SandboxFs(require_wsl, resolve_workspace(require_wsl.settings, require_wsl))
    root = f"{eval_root(require_wsl.settings, require_wsl)}/_git-{uuid4().hex[:8]}"
    clean_workspace(require_wsl, root, base=eval_root(require_wsl.settings, require_wsl))
    yield require_wsl, fs, root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


def test_materialize_leaves_a_clean_committed_repo(workspace) -> None:
    sandbox, fs, root = workspace
    materialize(TASK, sandbox, fs, root)

    assert sandbox.run(f"git -C {shlex.quote(root)} rev-parse --is-inside-work-tree").ok
    # 种子已提交，所以状态是干净的 —— 智能体一动手 `git_status` 就有东西可看
    assert sandbox.run(f"git -C {shlex.quote(root)} status --porcelain").stdout.strip() == ""
    assert sandbox.run(f"git -C {shlex.quote(root)} log --oneline").ok


def test_the_repo_is_deterministic(workspace) -> None:
    """同一份种子每次应得到同一个提交 —— 评测要可复现。"""
    sandbox, fs, root = workspace
    materialize(TASK, sandbox, fs, root)
    first = sandbox.run(f"git -C {shlex.quote(root)} rev-parse HEAD").stdout.strip()

    clean_workspace(sandbox, root, base=eval_root(sandbox.settings, sandbox))
    materialize(TASK, sandbox, fs, root)
    second = sandbox.run(f"git -C {shlex.quote(root)} rev-parse HEAD").stdout.strip()

    assert first and first == second, f"两次物化得到不同的提交：{first} / {second}"


def test_agent_edits_show_up_as_uncommitted_changes(workspace) -> None:
    """这是补 git 的**目的**：让"改了什么"有宿主之外的一个独立视角。"""
    sandbox, fs, root = workspace
    materialize(TASK, sandbox, fs, root)

    fs.write_text(f"{root}/pkg/mod.py", "def f():\n    return 2\n")

    status = sandbox.run(f"git -C {shlex.quote(root)} status --porcelain").stdout
    assert "pkg/mod.py" in status
    assert sandbox.run(f"git -C {shlex.quote(root)} diff --stat").ok
