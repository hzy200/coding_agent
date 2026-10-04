"""recursion_limit 必须与图拓扑一致。

算小会在正常任务里误报 GraphRecursionError（曾按"每轮 2 个超步"低估过一次）；
把推导写成一个有名字的函数，改拓扑时才有地方对照。
"""

from __future__ import annotations

from coding_agent.config import Settings
from coding_agent.graph.build import estimate_recursion_limit

MARGIN = 10  # planner + respond + 余量


def test_matches_worst_case_topology() -> None:
    rounds, steps = 12, 5
    # 每步最坏超步：act×(r+1) + approval_gate×r + tools×r + verify + advance
    per_step = (rounds + 1) + 2 * rounds + 2
    assert per_step == 3 * rounds + 3
    assert estimate_recursion_limit(steps, rounds) == steps * per_step + MARGIN


def test_monotonic_in_both_inputs() -> None:
    assert estimate_recursion_limit(5, 12) > estimate_recursion_limit(4, 12)
    assert estimate_recursion_limit(5, 12) > estimate_recursion_limit(5, 11)


def test_covers_a_maxed_out_default_plan() -> None:
    """默认上限下的最坏路径不能超过限额。"""
    s = Settings(_env_file=None)
    worst = s.max_plan_steps * ((s.max_tool_rounds + 1) + 2 * s.max_tool_rounds + 2)
    assert estimate_recursion_limit(s.max_plan_steps, s.max_tool_rounds) >= worst
