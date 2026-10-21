"""LangSmith 能力用例的隔离重跑。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from langsmith_suite import run_with_retries  # noqa: E402


def test_first_attempt_pass_does_not_retry() -> None:
    calls = 0

    def run_case():
        nonlocal calls
        calls += 1
        return {"passed": True}

    result = run_with_retries(run_case, retries=2)

    assert result["passed"] is True
    assert result["attempts"] == 1
    assert calls == 1


def test_failed_case_recovers_on_retry() -> None:
    calls = 0

    def run_case():
        nonlocal calls
        calls += 1
        return {"passed": calls >= 2}

    result = run_with_retries(run_case, retries=2)

    assert result["passed"] is True
    assert result["attempts"] == 2
    assert result["recovered_on_retry"] is True
    assert calls == 2


def test_retries_are_bounded() -> None:
    calls = 0

    def run_case():
        nonlocal calls
        calls += 1
        return {"passed": False}

    result = run_with_retries(run_case, retries=2)

    assert result["passed"] is False
    assert result["attempts"] == 3
    assert calls == 3
