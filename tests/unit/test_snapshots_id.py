"""快照 id 的可排序性（不依赖 WSL）。

`SnapshotStore.list` 靠 id 的字典序判断新旧；若时间戳只到**秒**，
同一秒内的多次留底会退化成按随机 uuid 排序，「回滚最近一次改动」就会选错版本。
"""

from __future__ import annotations

from coding_agent.sandbox.snapshots import _new_snapshot_id


def test_snapshot_ids_are_monotonic() -> None:
    """背靠背生成的 id 必须严格递增（同一秒内也是）。"""
    ids = [_new_snapshot_id() for _ in range(50)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)  # 无重复


def test_snapshot_id_has_microsecond_timestamp_and_sequence() -> None:
    timestamp, sep, suffix = _new_snapshot_id().partition("-")
    assert sep == "-"
    # YYYYmmdd(8) + T(1) + HHMMSS(6) + ffffff(6) + seq(3) = 24
    assert len(timestamp) == 24
    assert len(suffix) == 6 and all(c in "0123456789abcdef" for c in suffix)
