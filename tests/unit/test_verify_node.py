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
