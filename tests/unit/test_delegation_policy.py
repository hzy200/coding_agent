"""委派的成本策略：仓库规模 → 是否允许、最多几次。

A/B 的教训是 38 个文件的任务也会拆 3–5 个子智能体。这里钉住分级规则与
「探测失败不误伤」的行为。
"""

from __future__ import annotations

from coding_agent.agents.policy import (
    RepoProfile,
    delegation_policy,
    profile_workspace,
)
from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import ExecResult


class _Sandbox:
    def __init__(self, stdout: str = "38 9", *, ok: bool = True, raises: bool = False):
        self.stdout = stdout
        self.ok = ok
        self.raises = raises

    def run(self, command: str, *, cwd: str | None = None) -> ExecResult:
        if self.raises:
            raise AssertionError("sandbox unavailable")
        return ExecResult(
            command=command,
            exit_code=0 if self.ok else 1,
            stdout=self.stdout,
            stderr="",
            duration_ms=1,
        )


def _settings(**overrides) -> Settings:
    values = {"explore_enabled": True}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_profile_parses_file_and_test_counts() -> None:
    profile = profile_workspace(_Sandbox("38 12"), "/ws")  # type: ignore[arg-type]
    assert profile == RepoProfile(file_count=38, test_file_count=12)


def test_profile_is_unknown_on_probe_failure() -> None:
    assert profile_workspace(_Sandbox(raises=True), "/ws") == RepoProfile()  # type: ignore[arg-type]
    assert profile_workspace(_Sandbox(ok=False), "/ws") == RepoProfile()  # type: ignore[arg-type]
    assert profile_workspace(_Sandbox("nonsense"), "/ws") == RepoProfile()  # type: ignore[arg-type]


def test_small_repo_disables_delegation() -> None:
    policy = delegation_policy(RepoProfile(file_count=10), _settings())
    assert policy.allowed is False
    assert policy.max_calls == 0


def test_medium_repo_caps_delegation_at_two() -> None:
    policy = delegation_policy(RepoProfile(file_count=50), _settings())
    assert policy.allowed is True
    assert policy.max_calls == 2


def test_large_repo_caps_delegation_at_four() -> None:
    policy = delegation_policy(RepoProfile(file_count=150), _settings())
    assert policy.allowed is True
    assert policy.max_calls == 4


def test_very_large_repo_uses_the_configured_maximum() -> None:
    policy = delegation_policy(RepoProfile(file_count=500), _settings())
    assert policy.allowed is True
    assert policy.max_calls == 6


def test_unknown_size_stays_enabled_to_avoid_false_negatives() -> None:
    policy = delegation_policy(RepoProfile(), _settings())
    assert policy.allowed is True
    assert policy.max_calls == 6


def test_explore_disabled_wins_over_size() -> None:
    policy = delegation_policy(
        RepoProfile(file_count=500), _settings(explore_enabled=False)
    )
    assert policy.allowed is False
    assert policy.max_calls == 0
