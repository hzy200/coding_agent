"""verify 节点的短路契约（不依赖 WSL）。

verify 只在**本步改过东西**（`dirty`）时才跑测试：只读探索跑一遍 pytest 纯属浪费。
这条短路是 A4「shell 变更也要触发验证」的消费端 —— 这里把契约钉住。
"""

from __future__ import annotations

from coding_agent.config import Settings
from coding_agent.graph.nodes.verify import make_verify_node
from coding_agent.sandbox.wsl_exec import ExecResult


class _FakeSandbox:
    """记录被调用次数；任何 `run` 都返回空输出（探测不到测试命令）。"""

    def __init__(self) -> None:
        self.settings = Settings(_env_file=None, wsl_workspace="/ws")
        self.runs = 0

    def run(
        self, command: str, *, cwd: str | None = None, timeout: int | None = None
    ) -> ExecResult:
        self.runs += 1
        return ExecResult(command=command, exit_code=0, stdout="", stderr="", duration_ms=1)

    # 桩件刻意不做缓存：这里要数的就是 `run` 的调用次数。
    def cached_probe(self, key: str, producer):
        return producer()

    def forget_probe(self, key: str) -> None:
        return None


def test_clean_step_skips_verification() -> None:
    sandbox = _FakeSandbox()
    node = make_verify_node(sandbox)  # type: ignore[arg-type]
    out = node({"dirty": False}, None)  # type: ignore[arg-type]
    assert out == {"verification": {}}
    assert sandbox.runs == 0  # 一个字节的命令都没跑


def test_dirty_step_attempts_verification() -> None:
    sandbox = _FakeSandbox()
    node = make_verify_node(sandbox)  # type: ignore[arg-type]
    out = node({"dirty": True}, None)  # type: ignore[arg-type]
    assert sandbox.runs >= 1  # 确实去探测/执行了验证命令
    assert out["verification"]["status"] == "not_configured"


def test_verify_disabled_short_circuits() -> None:
    sandbox = _FakeSandbox()
    sandbox.settings = Settings(_env_file=None, wsl_workspace="/ws", verify_enabled=False)
    node = make_verify_node(sandbox)  # type: ignore[arg-type]
    out = node({"dirty": True}, None)  # type: ignore[arg-type]
    assert out == {"verification": {}}
    assert sandbox.runs == 0


# ---------------- 多步任务的验证范围 ----------------

def test_a_non_final_implement_step_skips_full_verification() -> None:
    """早期实现步只完成了一部分，全量套件必然被后续步骤卡住。"""
    sandbox = _FakeSandbox()
    node = make_verify_node(sandbox)  # type: ignore[arg-type]
    out = node(
        {
            "dirty": True,
            "plan": ["实现1", "实现2", "验证"],
            "plan_kinds": ["implement", "implement", "verify"],
            "step_idx": 0,
        },
        None,
    )
    assert out == {"verification": {}}
    assert sandbox.runs == 0


def test_the_final_implement_step_still_verifies() -> None:
    sandbox = _FakeSandbox()
    node = make_verify_node(sandbox)  # type: ignore[arg-type]
    out = node(
        {
            "dirty": True,
            "plan": ["实现1", "实现2"],
            "plan_kinds": ["implement", "implement"],
            "step_idx": 1,
        },
        None,
    )
    assert out["verification"]["status"] == "not_configured"
    assert sandbox.runs >= 1


def test_a_verify_step_runs_even_when_not_last() -> None:
    sandbox = _FakeSandbox()
    node = make_verify_node(sandbox)  # type: ignore[arg-type]
    out = node(
        {
            "dirty": True,
            "plan": ["验证", "收尾"],
            "plan_kinds": ["verify", "verify"],
            "step_idx": 0,
        },
        None,
    )
    assert out["verification"]["status"] == "not_configured"
    assert sandbox.runs >= 1


def test_a_verify_step_runs_without_new_changes() -> None:
    """最终验证步的职责就是跑全量，不该因为 dirty=False 被短路。"""
    sandbox = _FakeSandbox()
    node = make_verify_node(sandbox)  # type: ignore[arg-type]
    out = node(
        {
            "dirty": False,
            "plan": ["实现", "验证"],
            "plan_kinds": ["implement", "verify"],
            "step_idx": 1,
        },
        None,
    )
    assert out["verification"]["status"] == "not_configured"
    assert sandbox.runs >= 1


def test_verify_only_final_can_be_disabled() -> None:
    sandbox = _FakeSandbox()
    sandbox.settings = Settings(
        _env_file=None, wsl_workspace="/ws", verify_only_final=False
    )
    node = make_verify_node(sandbox)  # type: ignore[arg-type]
    out = node(
        {
            "dirty": True,
            "plan": ["实现1", "实现2"],
            "plan_kinds": ["implement", "implement"],
            "step_idx": 0,
        },
        None,
    )
    assert out["verification"]["status"] == "not_configured"
    assert sandbox.runs >= 1


# ---------------- 失败签名、停滞检测与测试选择 ----------------

_PROBE = "HAS pytest\nDIR tests\nHAS python\n"


class _ScriptedSandbox(_FakeSandbox):
    """探测返回 pytest；后续按脚本返回测试结果。"""

    def __init__(self, results: list[ExecResult]) -> None:
        super().__init__()
        self.results = list(results)
        self.commands: list[str] = []

    def run(
        self, command: str, *, cwd: str | None = None, timeout: int | None = None
    ) -> ExecResult:
        self.commands.append(command)
        if command.startswith("for f in"):
            return ExecResult(
                command=command, exit_code=0, stdout=_PROBE, stderr="", duration_ms=1
            )
        return self.results.pop(0)

    def pytest_commands(self) -> list[str]:
        return [c for c in self.commands if c.startswith("python3 -m pytest")]


def _failed(command: str, test_id: str, message: str = "AssertionError: 1 == 2") -> ExecResult:
    return ExecResult(
        command=command,
        exit_code=1,
        stdout=f"FAILED {test_id} - {message}\n1 failed in 0.01s\n",
        stderr="",
        duration_ms=1,
    )


def _passed(command: str) -> ExecResult:
    return ExecResult(
        command=command, exit_code=0, stdout="1 passed in 0.01s\n", stderr="", duration_ms=1
    )


def test_repair_reruns_only_the_failed_test_and_detects_stagnation() -> None:
    test_id = "tests/test_discount.py::test_net"
    sandbox = _ScriptedSandbox(
        [_failed("full", test_id), _failed("selected", test_id)]
    )
    node = make_verify_node(sandbox)  # type: ignore[arg-type]

    first = node({"dirty": True}, None)  # type: ignore[arg-type]
    assert first["verification"]["failed_tests"] == [test_id]
    assert first["same_failure_count"] == 0

    second = node(
        {
            "dirty": True,
            "retry": 1,
            "verification": first["verification"],
            "same_failure_count": 0,
        },
        None,
    )
    assert second["same_failure_count"] == 1
    assert sandbox.pytest_commands() == [
        "python3 -m pytest -q",
        f"python3 -m pytest -q {test_id}",
    ]


def test_selected_pass_is_confirmed_by_a_full_run() -> None:
    first_test = "tests/test_discount.py::test_net"
    other_test = "tests/test_core.py::test_other"
    sandbox = _ScriptedSandbox(
        [
            _failed("full", first_test),
            _passed("selected"),
            _failed("full-again", other_test, "KeyError: 'x'"),
        ]
    )
    node = make_verify_node(sandbox)  # type: ignore[arg-type]

    first = node({"dirty": True}, None)  # type: ignore[arg-type]
    second = node(
        {
            "dirty": True,
            "retry": 1,
            "verification": first["verification"],
            "same_failure_count": 0,
        },
        None,
    )

    commands = sandbox.pytest_commands()
    assert commands[1].endswith(first_test)
    assert commands[2] == "python3 -m pytest -q"
    assert second["verification"]["status"] == "failed"
    assert second["verification"]["failed_tests"] == [other_test]
    assert second["verification"]["scope"] == "full"


def test_stagnation_survives_a_replan_that_clears_the_verification() -> None:
    """replan 清空 verification 后，仍要能判断「还是同一个失败」。"""
    test_id = "tests/test_discount.py::test_net"
    sandbox = _ScriptedSandbox(
        [_failed("full", test_id), _failed("after-replan", test_id)]
    )
    node = make_verify_node(sandbox)  # type: ignore[arg-type]

    first = node({"dirty": True}, None)  # type: ignore[arg-type]
    # replan 只清 verification；last_failure_signature / same_failure_count 保留
    second = node(
        {
            "dirty": True,
            "retry": 1,
            "verification": {},
            "same_failure_count": 1,
            "last_failure_signature": first["last_failure_signature"],
        },
        None,
    )
    assert second["same_failure_count"] == 2
    assert second["last_failure_signature"] == first["last_failure_signature"]


def test_a_changed_failure_signature_resets_the_stagnation_counter() -> None:
    first_test = "tests/test_discount.py::test_net"
    other_test = "tests/test_core.py::test_other"
    sandbox = _ScriptedSandbox(
        [_failed("full", first_test), _failed("selected", other_test)]
    )
    node = make_verify_node(sandbox)  # type: ignore[arg-type]

    first = node({"dirty": True}, None)  # type: ignore[arg-type]
    second = node(
        {
            "dirty": True,
            "retry": 1,
            "verification": first["verification"],
            "same_failure_count": 1,
        },
        None,
    )
    assert second["same_failure_count"] == 0
