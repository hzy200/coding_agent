"""SandboxFs 的脚本契约与 DATA 语义（不依赖 WSL）。

大文件保护的关键不在 Python 侧的大小比较，而在**沙箱脚本是否读了内容**：
一旦 `_stat_script` 无条件 `base64`，整份文件就会先物化进内存，Python 再拒也没用。
所以这里直接钉住脚本契约与 `_parse_stat` 对「空内容 / 无内容」的区分。
"""

from __future__ import annotations

import base64

from coding_agent.sandbox.fs import SandboxFs


def _fs() -> SandboxFs:
    # _stat_script / _parse_stat 都不用沙箱，传个占位即可
    return SandboxFs(None, "/ws")  # type: ignore[arg-type]


# ---------------- 脚本契约：超限不读 ----------------

def test_stat_script_guards_data_by_size() -> None:
    """给了上限时，脚本必须带大小守卫，否则会把大文件整份读出来。"""
    script = _fs()._stat_script("/ws/big.bin", include_data=True, max_bytes=1000)
    assert '[ "$size" -le 1000 ]' in script
    assert "base64" in script


def test_stat_script_without_limit_reads_unconditionally() -> None:
    """没给上限时保持原行为：只要不是超大内容就直接读。"""
    script = _fs()._stat_script("/ws/small.txt", include_data=True)
    assert "base64" in script
    assert '[ "$size" -le' not in script


def test_stat_script_without_data_has_no_base64() -> None:
    script = _fs()._stat_script("/ws/x", include_data=False, max_bytes=1000)
    assert "base64" not in script


# ---------------- DATA 语义：空文件 ≠ 无内容 ----------------

def _stdout(*, size: int, data_line: str | None) -> str:
    lines = ["EXISTS=1", "FILE=1", f"SIZE={size}", "REAL=/ws/x"]
    if data_line is not None:
        lines.append(data_line)
    return "\n".join(lines) + "\n"


def test_parse_stat_empty_file_yields_empty_bytes() -> None:
    """空文件必须能被读成 b""，而不是被当成「读失败」。"""
    info, data = SandboxFs._parse_stat(_stdout(size=0, data_line="DATA="))
    assert info.size == 0
    assert data == b""


def test_parse_stat_missing_data_yields_none() -> None:
    """没有 DATA 行（文件不存在 / 非普通文件 / 超限未读）→ None。"""
    # 空文件之外，超限时脚本不输出 DATA，这里用 SIZE 大且无 DATA 行模拟
    info, data = SandboxFs._parse_stat(_stdout(size=10_000_000, data_line=None))
    assert info.size == 10_000_000
    assert data is None


def test_parse_stat_non_empty_decodes() -> None:
    payload = base64.b64encode("你好".encode()).decode()
    _, data = SandboxFs._parse_stat(_stdout(size=6, data_line=f"DATA={payload}"))
    assert data == "你好".encode()
