"""沙箱探测结果的缓存契约（不依赖 WSL）。

每次 `wsl.exe` 启动约 0.3s，而有些探测在一个运行里会被反复触发：
`$HOME` 被 `resolve_workspace` 每个工具构造器各调一次（建图时约 9 次），
测试命令探测被每次验证各调一次（一步最多 4 次）。

这些值在一次运行内不会变，所以缓存下来。这里钉住的是**缓存的边界**：
哪些该记、哪些不该记 —— 记错了比不记更糟。
"""

from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import (
    ExecResult,
    WslSandbox,
    WslUnavailableError,
    resolve_workspace,
)
from coding_agent.tools.testrun import detect_test_command, run_verification


def _sandbox_with_run(monkeypatch, stdout: str = "", exit_code: int = 0):
    """返回 (sandbox, calls)，把 `run` 换成不启动 wsl.exe 的记录桩。"""
    sandbox = WslSandbox(Settings(_env_file=None))
    calls: list[tuple[str, str | None]] = []

    def fake_run(command: str, *, cwd: str | None = None, timeout: int | None = None):
        calls.append((command, cwd))
        return ExecResult(
            command=command, exit_code=exit_code, stdout=stdout, stderr="", duration_ms=1
        )

    monkeypatch.setattr(sandbox, "run", fake_run)
    return sandbox, calls


# ---------------- $HOME ----------------


def test_home_is_probed_once_across_repeated_workspace_resolution(monkeypatch) -> None:
    """`resolve_workspace` 在建图时被各工具构造器反复调用 —— $HOME 只探一次。"""
    sandbox, calls = _sandbox_with_run(monkeypatch, stdout="/home/tester\n")

    assert sandbox.home() == "/home/tester"
    assert sandbox.home() == "/home/tester"
    assert resolve_workspace(sandbox.settings, sandbox) == "/home/tester/agent-ws"
    assert resolve_workspace(sandbox.settings, sandbox) == "/home/tester/agent-ws"

    assert len(calls) == 1


def test_a_failed_home_probe_is_not_cached(monkeypatch) -> None:
    """失败的探测不能记成「这个人没有 $HOME」—— 下次要能重试。"""
    sandbox, calls = _sandbox_with_run(monkeypatch, stdout="", exit_code=1)

    with pytest.raises(WslUnavailableError):
        sandbox.home()
    with pytest.raises(WslUnavailableError):
        sandbox.home()

    assert len(calls) == 2


def test_two_sandboxes_do_not_share_probe_results(monkeypatch) -> None:
    """缓存挂在实例上：每个沙箱（每次运行、每个测试）各记各的，不跨运行串味。"""
    first, first_calls = _sandbox_with_run(monkeypatch, stdout="/home/a\n")
    second, second_calls = _sandbox_with_run(monkeypatch, stdout="/home/b\n")

    assert first.home() == "/home/a"
    assert second.home() == "/home/b"
    assert len(first_calls) == 1
    assert len(second_calls) == 1


# ---------------- 测试命令 ----------------

_PYTEST_HINTS = "DIR tests\nHAS pytest\n"
_MAKE_HINTS = "FILE Makefile\nMAKE test\n"


def test_auto_verify_refuses_manifest_derived_commands(monkeypatch) -> None:
    """`make test` / `npm test` 跑什么写在模型可写的文件里，不能自动执行。"""
    sandbox, _ = _sandbox_with_run(monkeypatch, stdout=_MAKE_HINTS)

    # 走审批的 run_tests 可以用
    assert detect_test_command(sandbox, "/ws/m", allow_manifest=True) == "make test"
    # 不过审批的自动 verify 不行
    assert detect_test_command(sandbox, "/ws/m", allow_manifest=False) == ""

    npm, _ = _sandbox_with_run(monkeypatch, stdout='FILE package.json\nNPM test\n')
    assert detect_test_command(npm, "/ws/n", allow_manifest=True) == "npm test --silent"
    assert detect_test_command(npm, "/ws/n", allow_manifest=False) == ""


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (_PYTEST_HINTS, "python3 -m pytest -q"),
        ("FILE Cargo.toml\n", "cargo test"),
        ("FILE go.mod\n", "go build ./... && go test ./..."),
    ],
)
def test_auto_verify_keeps_commands_the_host_fixes_itself(
    monkeypatch, stdout: str, expected: str
) -> None:
    """命令文本由宿主写死的那几条不受影响：跑什么不是工作区文件说了算。"""
    sandbox, _ = _sandbox_with_run(monkeypatch, stdout=stdout)
    assert detect_test_command(sandbox, "/ws/x", allow_manifest=False) == expected


@pytest.mark.parametrize(("first", "second"), [(False, True), (True, False)])
def test_manifest_verdicts_do_not_share_a_cache_entry(monkeypatch, first, second) -> None:
    """两种口径答案不同，共用一个缓存键会把答案灌错方向。

    最坏的方向是审批口径的 `make test` 漏进无人审批的自动 verify ——
    先跑 run_tests 再跑 verify 就会命中。
    """
    sandbox, _ = _sandbox_with_run(monkeypatch, stdout=_MAKE_HINTS)

    detect_test_command(sandbox, "/ws/m", allow_manifest=first)
    assert detect_test_command(sandbox, "/ws/m", allow_manifest=second) == (
        "make test" if second else ""
    )


def test_test_command_is_probed_once_per_workspace(monkeypatch) -> None:
    sandbox, calls = _sandbox_with_run(monkeypatch, stdout=_PYTEST_HINTS)

    assert detect_test_command(sandbox, "/ws/a") == "python3 -m pytest -q"
    assert detect_test_command(sandbox, "/ws/a") == "python3 -m pytest -q"
    assert len(calls) == 1  # 同一个工作区只探一次

    # 换了工作区要重新探 —— 缓存键里带着 root
    detect_test_command(sandbox, "/ws/b")
    assert len(calls) == 2


def test_an_empty_probe_result_is_not_cached(monkeypatch) -> None:
    """「没探测到」不能记住：任务可能先建目录、后写 pyproject.toml。

    把空结果缓存下来，验证会一直停在 `not_configured`，而真实原因只是
    第一次探测发生在配置出现之前。
    """
    sandbox, calls = _sandbox_with_run(monkeypatch, stdout="")

    assert detect_test_command(sandbox, "/ws/a") == ""
    assert detect_test_command(sandbox, "/ws/a") == ""
    assert len(calls) == 2


def test_an_override_bypasses_the_probe_entirely(monkeypatch) -> None:
    """显式指定的命令不需要探测，也不进缓存。"""
    sandbox, calls = _sandbox_with_run(monkeypatch, stdout=_PYTEST_HINTS)

    assert detect_test_command(sandbox, "/ws/a", override="make check") == "make check"
    assert calls == []
    # 覆盖值不该污染自动探测的结果
    assert detect_test_command(sandbox, "/ws/a") == "python3 -m pytest -q"


def test_repeated_verification_probes_only_once(monkeypatch) -> None:
    """端到端：同一工作区连跑两遍验证，探测只发生在第一遍。"""
    sandbox, calls = _sandbox_with_run(monkeypatch, stdout=_PYTEST_HINTS)

    first = run_verification(sandbox, "/ws/a")
    second = run_verification(sandbox, "/ws/a")

    assert first.status == "ok"
    assert second.status == "ok"
    # 第一遍：探测 + 执行；第二遍：只执行
    assert [command for command, _ in calls] == [
        calls[0][0],
        "python3 -m pytest -q",
        "python3 -m pytest -q",
    ]
    assert len(calls) == 3
