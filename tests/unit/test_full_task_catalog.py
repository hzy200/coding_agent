"""LangSmith 全功能任务目录的结构约束。"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

_EVAL = Path(__file__).resolve().parents[2] / "tests" / "eval"
if str(_EVAL) not in sys.path:
    sys.path.insert(0, str(_EVAL))

from tasks_full import TASKS_FULL  # noqa: E402


def test_catalog_has_at_least_100_unique_tasks() -> None:
    ids = [task.id for task in TASKS_FULL]

    assert len(ids) >= 100
    assert len(ids) == len(set(ids))


def test_every_task_has_tests_reference_and_prompt() -> None:
    for task in TASKS_FULL:
        assert task.tests, task.id
        assert task.reference, task.id
        assert task.prompt.strip(), task.id


def test_catalog_covers_all_categories_and_tiers() -> None:
    categories = Counter(task.category for task in TASKS_FULL)
    tiers = Counter(task.tier for task in TASKS_FULL)

    assert set(categories) == {"single_file", "cross_file", "debug", "spec"}
    assert set(tiers) == {"basic", "deep", "long"}
    assert min(categories.values()) >= 8
