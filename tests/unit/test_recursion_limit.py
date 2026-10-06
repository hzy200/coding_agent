"""recursion_limit 必须与图拓扑一致。

算小会在正常任务里误报 GraphRecursionError。踩过两次：
先是按"每轮 2 个超步"低估，后来又漏算了 repair 会重置工具预算、
让每一轮修复都重新吃满一个完整周期。
把推导写成一个有名字的函数，改拓扑时才有地方对照。
"""

from __future__ import annotations

from coding_agent.config import Settings
from coding_agent.graph.build import estimate_recursion_limit

MARGIN = 10  # planner + respond + 余量


def test_matches_worst_case_topology() -> None:
    rounds, steps, repairs, replans = 12, 5, 3, 2
    # 一个周期：act×(r+1) + approval_gate×r + tools×r + verify + review
    per_cycle = (rounds + 1) + 2 * rounds + 2
    assert per_cycle == 3 * rounds + 3
    # 一个阶段：修复把工具轮次清零，所以是 (修复次数+1) 个周期 + repair 自身
    phase = (repairs + 1) * per_cycle + repairs
    # 最坏路径是一路失败：每个阶段之后都进一次 replan（含最后一个，它负责放弃），
    # 而重规划会给新做法重置修复预算与轮次，所以整块「阶段 + replan」重复 (重规划+1) 遍
    per_step = (replans + 1) * (phase + 1)
    # 失败修订若让本步通过，仍会走 advance → replan（步进微调，独立额度），
    # 相邻步骤之间最多各多两个超步
    assert estimate_recursion_limit(steps, rounds, repairs, replans) == (
        steps * (per_step + 2) + MARGIN
    )


def test_monotonic_in_all_inputs() -> None:
    assert estimate_recursion_limit(5, 12, 3, 2) > estimate_recursion_limit(4, 12, 3, 2)
    assert estimate_recursion_limit(5, 12, 3, 2) > estimate_recursion_limit(5, 11, 3, 2)
    assert estimate_recursion_limit(5, 12, 3, 2) > estimate_recursion_limit(5, 12, 2, 2)
    assert estimate_recursion_limit(5, 12, 3, 2) > estimate_recursion_limit(5, 12, 3, 1)


def test_covers_a_maxed_out_default_plan() -> None:
    """默认上限下的最坏路径不能超过限额 —— 含把每一轮修复与每一次重规划都烧光。"""
    s = Settings(_env_file=None)
    # 周期里含 review：验证通过后必过一遍。它与 verify **共用**修复预算，
    # 所以阶段结构不变，只是每周期多一个超步。
    per_cycle = (s.max_tool_rounds + 1) + 2 * s.max_tool_rounds + 2
    phase = (s.max_repair_rounds + 1) * per_cycle + s.max_repair_rounds
    # 每步还可能在步进之后多一次「步进微调」（advance + replan）
    worst = s.max_plan_steps * ((s.max_replans + 1) * (phase + 1) + 2)
    assert (
        estimate_recursion_limit(
            s.max_plan_steps, s.max_tool_rounds, s.max_repair_rounds, s.max_replans
        )
        >= worst
    )
