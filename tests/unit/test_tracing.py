"""追踪元数据。

凭据有效与否是环境问题，但**元数据挂载**是代码问题：
它决定 trace 能不能按会话/工作区筛选，也决定有没有把敏感内容捎带出去。
"""

from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.runtime import AgentRuntime


def _runtime(**overrides) -> AgentRuntime:
    return AgentRuntime(
        Settings(_env_file=None, **overrides), workspace="/mnt/d/proj"
    )


def test_metadata_carries_run_context() -> None:
    meta = _runtime()._trace_metadata("abc123")
    assert meta["thread_id"] == "abc123"
    assert meta["workspace"] == "/mnt/d/proj"
    assert meta["allow_write"] is False
    assert meta["approval_mode"] == "ask"
    assert "model" in meta


def test_metadata_reflects_write_permission() -> None:
    assert _runtime()._trace_metadata("t")["allow_write"] is False
    assert AgentRuntime(
        Settings(_env_file=None), workspace="/mnt/d/proj", allow_write=True
    )._trace_metadata("t")["allow_write"] is True


def test_metadata_does_not_leak_prompts_or_file_contents() -> None:
    """追踪数据会离开本机，只放标识与开关，不放提示词或代码。"""
    meta = _runtime()._trace_metadata("abc")
    assert set(meta) == {
        "thread_id",
        "workspace",
        "model",
        "allow_write",
        "approval_mode",
        "verify",
        "persistent",
    }


def test_verify_metadata_shows_auto_when_not_overridden() -> None:
    assert _runtime()._trace_metadata("t")["verify"] == "auto"
    assert _runtime(verify_command="make test")._trace_metadata("t")["verify"] == "make test"


def test_persistent_flag_reflects_checkpoint_mode() -> None:
    assert _runtime()._trace_metadata("t")["persistent"] is True
    assert _runtime(checkpoint_path=":memory:")._trace_metadata("t")["persistent"] is False


@pytest.mark.parametrize("mode", ["ask", "approve", "deny"])
def test_approval_mode_is_propagated(mode: str) -> None:
    assert AgentRuntime(
        Settings(_env_file=None), workspace="/mnt/d/proj", approval_mode=mode
    )._trace_metadata("t")["approval_mode"] == mode
