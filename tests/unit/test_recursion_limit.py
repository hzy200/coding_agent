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
    # 一个周期：act×(r+1) + steering_cancel×(2r+1，act 后与 tools 后)
    # + approval_gate×r + tools×r + verify + review
    per_cycle = (rounds + 1) + (2 * rounds + 1) + 2 * rounds + 2
    assert per_cycle == 5 * rounds + 4
    # 一个阶段：修复把工具轮次清零，所以是 (修复次数+1) 个周期 + repair 自身
    phase = (repairs + 1) * per_cycle + repairs
    # 最坏路径是一路失败：每个阶段之后都进一次 replan（含最后一个，它负责放弃），
    # 而重规划会给新做法重置修复预算与轮次，所以整块「阶段 + replan」重复 (重规划+1) 遍
    per_step = (replans + 1) * (phase + 1)
    # 一步没产生任何改动时会经 nudge 重做一轮：nudge 节点 + 一个完整周期
    per_step += per_cycle + 1
    # Phase 3D：advance 之后多一个 steering_step
    per_step += 1
    # 失败修订若让本步通过，仍会走 advance → steering_step → replan
    # （步进微调，独立额度），相邻步骤之间最多各多两个超步；
    # 收尾多一个 steering_final。
    assert estimate_recursion_limit(steps, rounds, repairs, replans) == (
        steps * (per_step + 2) + 1 + MARGIN
    )


def test_monotonic_in_all_inputs() -> None:
    assert estimate_recursion_limit(5, 12, 3, 2) > estimate_recursion_limit(4, 12, 3, 2)
    assert estimate_recursion_limit(5, 12, 3, 2) > estimate_recursion_limit(5, 11, 3, 2)
    assert estimate_recursion_limit(5, 12, 3, 2) > estimate_recursion_limit(5, 12, 2, 2)
    assert estimate_recursion_limit(5, 12, 3, 2) > estimate_recursion_limit(5, 12, 3, 1)


def test_event_driven_scheduler_boundaries_are_counted() -> None:
    """事件驱动按 checkpoint 边界循环，不能再套旧 dispatch 的固定轮次。"""
    base = estimate_recursion_limit(5, 12, 3, 2)
    event = estimate_recursion_limit(
        5,
        12,
        3,
        2,
        orchestrator_enabled=True,
        orchestrator_max_tasks=6,
        orchestrator_checkpoint_every=1,
    )

    # 6 个任务、每个边界只完成 1 个 -> 6 个 scheduler + 6 个 steering 边界；
    # 子图固定节点 6 个（survey/select/plan/steering_plan/write_gate/fan_in）。
    assert event > base
    assert event == base + (2 + 1) * (6 + 6 * 2) + 2


def test_covers_a_maxed_out_default_plan() -> None:
    """默认上限下的最坏路径不能超过限额 —— 含把每一轮修复与每一次重规划都烧光。"""
    s = Settings(_env_file=None)
    # 周期里含 review（验证后必过）与 act/tools 后的 steering_cancel。
    # review 与 verify **共用**修复预算，所以阶段结构不变。
    per_cycle = (
        (s.max_tool_rounds + 1)
        + (2 * s.max_tool_rounds + 1)
        + 2 * s.max_tool_rounds
        + 2
    )
    phase = (s.max_repair_rounds + 1) * per_cycle + s.max_repair_rounds
    # 每步还可能在步进之后多一次「步进微调」（advance + steering_step + replan）
    worst = s.max_plan_steps * ((s.max_replans + 1) * (phase + 1) + 3)
    assert (
        estimate_recursion_limit(
            s.max_plan_steps, s.max_tool_rounds, s.max_repair_rounds, s.max_replans
        )
        >= worst
    )
