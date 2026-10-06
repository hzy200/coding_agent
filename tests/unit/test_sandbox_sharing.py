"""一次运行只该有一份沙箱。

环境探测（$HOME、bwrap、rg）的结果缓存在**沙箱实例**上。每个持有者各建一份，
就等于每份都重新探一遍 —— 曾经 runtime / build_tools / verify 各有一份，
冷启动光探测就要 4 次 `wsl.exe` 进程启动。

这两条用例钉住「注入的实例被真的用上了」和「工作区只解析一次再往下传」。
"""

from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.graph.nodes import verify as verify_mod
from coding_agent.graph.nodes.verify import make_verify_node
from coding_agent.sandbox.wsl_exec import ExecResult, WslSandbox
from coding_agent.tools.registry import build_tools

ROOT = "/tmp/agent-eval"


class _CountingSandbox:
    """记录被调用次数的假沙箱：只要它被调用，就说明注入的实例真被用上了。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runs: list[str] = []

    def run(
        self, command: str, *, cwd: str | None = None, timeout: int | None = None
    ) -> ExecResult:
        self.runs.append(command)
        return ExecResult(command=command, exit_code=0, stdout="", stderr="", duration_ms=0)


def _settings() -> Settings:
    return Settings(_env_file=None, deepseek_api_key="x", wsl_workspace=ROOT)


def test_build_tools_uses_the_injected_sandbox() -> None:
    """注入的实例若被无视，这里一次调用都记不到。"""
    sandbox = _CountingSandbox(_settings())
    tools = build_tools(
        sandbox.settings,  # type: ignore[arg-type]
        allow_write=True,
        sandbox=sandbox,
    )

    assert tools
    # 检索工具建的时候要探测 rg；它走的是注入的这份沙箱
    assert any("command -v rg" in command for command in sandbox.runs)


def test_verify_node_uses_the_root_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """verify 与工具共用沙箱，但工作区由**本次运行的 settings** 决定。

    沙箱那份 settings 未必带 `--workspace` 覆盖 —— 让 verify 自己推会退回 $HOME，
    于是验证跑在了另一个目录里。
    """
    captured: dict[str, str] = {}

    class _Result:
        def model_dump(self) -> dict[str, object]:
            return {"status": "ok"}

    def _fake_run(_sandbox, root: str, **_kwargs):
        captured["root"] = root
        return _Result()

    monkeypatch.setattr(verify_mod, "run_verification", _fake_run)

    # 沙箱自己的 settings 指向别处，root 才是本次运行的工作区
    sandbox = WslSandbox(Settings(_env_file=None, verify_enabled=True, wsl_workspace="/home/x"))
    node = make_verify_node(sandbox, ROOT)
    node({"dirty": True}, {})

    assert captured["root"] == ROOT


def test_verify_node_falls_back_to_its_own_settings_without_a_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不传 root 时按沙箱的 settings 推 —— 保持旧调用方式可用。"""
    captured: dict[str, str] = {}

    class _Result:
        def model_dump(self) -> dict[str, object]:
            return {"status": "ok"}

    monkeypatch.setattr(
        verify_mod,
        "run_verification",
        lambda _sandbox, root, **_kwargs: captured.update(root=root) or _Result(),
    )

    sandbox = WslSandbox(Settings(_env_file=None, verify_enabled=True, wsl_workspace="/home/x"))
    make_verify_node(sandbox)({"dirty": True}, {})

    assert captured["root"] == "/home/x"


def test_auto_verify_disallows_manifest_derived_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动 verify 不过审批，所以不能执行「跑什么写在模型可写文件里」的命令。

    `make test` / `npm test` 的命令文本来自 Makefile 与 package.json，而这两个
    文件模型用 file_write 就写得到（`--write` 下 L1 自动放行）—— 不挡的话
    「写个恶意 Makefile → 下一次脏写自动执行」就是一条绕过命令分级的路径。
    """
    captured: dict[str, object] = {}

    class _Result:
        def model_dump(self) -> dict[str, object]:
            return {"status": "ok"}

    def _fake_run(_sandbox, root: str, **kwargs):
        captured.update(root=root, **kwargs)
        return _Result()

    monkeypatch.setattr(verify_mod, "run_verification", _fake_run)

    sandbox = WslSandbox(Settings(_env_file=None, verify_enabled=True, wsl_workspace=ROOT))
    make_verify_node(sandbox, ROOT)({"dirty": True}, {})

    assert captured["allow_manifest"] is False
