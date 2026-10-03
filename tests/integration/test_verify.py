"""集成测试：验证命令探测、执行与 verify 节点的门控。

执行路径用 `AGENT_VERIFY_COMMAND` 注入确定性命令，不依赖沙箱里是否装了
pytest/cargo/make —— 那些属于工具链，不属于被测逻辑。
探测逻辑本身是看清单文件，可以独立测。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.graph.nodes.verify import make_verify_node
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.testrun import (
    VerifyResult,
    detect_test_command,
    run_verification,
)

pytestmark = pytest.mark.wsl

PASS_CMD = "python3 -c \"print('3 passed in 0.01s')\""
FAIL_CMD = (
    "python3 -c \"import sys;"
    "print('tests/test_x.py:5: error: boom');"
    "print('FAILED tests/test_x.py::test_y - assert 1 == 2');"
    "print('1 failed, 2 passed in 0.05s');"
    "sys.exit(1)\""
)


@pytest.fixture
def workdir(require_wsl: WslSandbox) -> str:
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-ver-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(root)}")
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


def _touch(require_wsl: WslSandbox, root: str, name: str, content: str = "") -> None:
    target = shlex.quote(f"{root}/{name}")
    parent = shlex.quote(f"{root}/{name}".rsplit("/", 1)[0])
    require_wsl.run(f"mkdir -p {parent} && printf %s {shlex.quote(content)} > {target}")


def _settings(settings, workdir: str, **overrides):
    return settings.model_copy(update={"wsl_workspace": workdir, **overrides})


# ---------------- 命令探测（按清单文件） ----------------

@pytest.mark.parametrize(
    ("manifest", "content", "expected_prefix"),
    [
        ("Cargo.toml", "", "cargo test"),
        ("go.mod", "", "go build"),
        ("Makefile", "test:\n\techo ok\n", "make test"),
    ],
)
def test_detects_by_manifest(require_wsl, workdir, manifest, content, expected_prefix) -> None:
    _touch(require_wsl, workdir, manifest, content)
    assert detect_test_command(require_wsl, workdir).startswith(expected_prefix)


def test_detects_npm_when_test_script_present(require_wsl, workdir) -> None:
    _touch(require_wsl, workdir, "package.json", '{"scripts": {"test": "node -e 1"}}')
    assert detect_test_command(require_wsl, workdir) == "npm test --silent"


def test_package_json_without_test_script_is_not_a_test_project(require_wsl, workdir) -> None:
    _touch(require_wsl, workdir, "package.json", '{"name": "x"}')
    assert detect_test_command(require_wsl, workdir) == ""


def test_makefile_without_test_target_is_ignored(require_wsl, workdir) -> None:
    _touch(require_wsl, workdir, "Makefile", "build:\n\techo hi\n")
    assert detect_test_command(require_wsl, workdir) == ""


def test_lockfile_style_ordering_prefers_more_specific(require_wsl, workdir) -> None:
    """polyglot 仓库要选更具体的那个，不能随手挑一个。"""
    _touch(require_wsl, workdir, "pyproject.toml", "[project]\nname='x'\n")
    _touch(require_wsl, workdir, "Cargo.toml", "")
    assert detect_test_command(require_wsl, workdir) == "cargo test"


def test_empty_workspace_has_nothing_to_verify(require_wsl, workdir) -> None:
    assert detect_test_command(require_wsl, workdir) == ""


def test_override_beats_detection(require_wsl, workdir) -> None:
    _touch(require_wsl, workdir, "Cargo.toml", "")
    assert detect_test_command(require_wsl, workdir, override="make check") == "make check"


# ---------------- 执行与解析 ----------------

def test_passing_command(require_wsl, workdir, settings) -> None:
    scoped = _settings(settings, workdir, verify_command=PASS_CMD)
    result = run_verification(WslSandbox(scoped), workdir)

    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.ok and result.ran
    assert "3 passed" in result.summary
    assert result.issues == []


def test_failing_command_parses_issues(require_wsl, workdir, settings) -> None:
    scoped = _settings(settings, workdir, verify_command=FAIL_CMD)
    result = run_verification(WslSandbox(scoped), workdir)

    assert result.status == "failed"
    assert result.exit_code == 1
    assert not result.ok
    locations = {i.location for i in result.issues}
    assert "tests/test_x.py:5" in locations
    assert "1 failed" in result.summary
    assert result.output_tail  # 原始输出保留，供模型兜底排查


def test_not_configured_when_nothing_detected(require_wsl, workdir, settings) -> None:
    scoped = _settings(settings, workdir, verify_command="")
    result = run_verification(WslSandbox(scoped), workdir)
    assert result.status == "not_configured"
    assert result.ok  # 「没得验证」不该被当成失败
    assert not result.ran


def test_timeout_is_reported(require_wsl, workdir, settings) -> None:
    scoped = _settings(settings, workdir, verify_command="sleep 30", shell_timeout=2)
    require_wsl.run("true", timeout=60)  # 预热，避免冷启动耗时被算进超时
    result = run_verification(WslSandbox(scoped), workdir)

    assert result.status == "failed"
    assert result.timed_out is True
    assert result.summary == "超时"


# ---------------- verify 节点门控 ----------------

def _run_node(scoped, state: dict) -> dict:
    node = make_verify_node(WslSandbox(scoped))
    return node(state, None)


def test_node_skips_when_step_changed_nothing(require_wsl, workdir, settings) -> None:
    """只读探索的步骤不该跑测试 —— 一次 pytest 可能几十秒。"""
    scoped = _settings(settings, workdir, verify_command=PASS_CMD)
    assert _run_node(scoped, {"dirty": False}) == {"verification": {}}


def test_node_runs_when_step_was_dirty(require_wsl, workdir, settings) -> None:
    scoped = _settings(settings, workdir, verify_command=FAIL_CMD)
    out = _run_node(scoped, {"dirty": True})

    dumped = out["verification"]
    assert dumped["status"] == "failed"
    # 必须是可 JSON 化的普通 dict，否则进不了 checkpoint
    assert isinstance(dumped, dict)
    assert VerifyResult.model_validate(dumped).status == "failed"


def test_node_respects_disable_switch(require_wsl, workdir, settings) -> None:
    scoped = _settings(settings, workdir, verify_command=PASS_CMD, verify_enabled=False)
    assert _run_node(scoped, {"dirty": True}) == {"verification": {}}


def test_node_missing_dirty_key_is_treated_as_clean(require_wsl, workdir, settings) -> None:
    scoped = _settings(settings, workdir, verify_command=PASS_CMD)
    assert _run_node(scoped, {}) == {"verification": {}}
