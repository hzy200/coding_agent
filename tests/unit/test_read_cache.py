"""run 级读缓存与 shell 变更的一致性。

`SandboxFs` 的缓存只会在文件工具写路径上失效；shell 绕过 SandboxFs，
宿主无法知道它改了哪些文件。因此非只读 shell 命令必须保守地清空缓存。
"""

from __future__ import annotations

from coding_agent.config import Settings
from coding_agent.read_cache import ReadCache
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.shell import build_shell_tool

ROOT = "/tmp/agent-read-cache"


class _FakeSandbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        capture_limit: int | None = None,
    ) -> ExecResult:
        return ExecResult(
            command=command, exit_code=0, stdout="", stderr="", duration_ms=0
        )


def _settings() -> Settings:
    return Settings(_env_file=None, deepseek_api_key="x", wsl_workspace=ROOT)


def test_read_only_shell_keeps_cache() -> None:
    settings = _settings()
    cache = {f"{ROOT}/a.py": b"old"}
    tool = build_shell_tool(
        settings, _FakeSandbox(settings), read_cache=cache  # type: ignore[arg-type]
    )

    tool.invoke({"command": "ls -la", "reason": "查看目录"})

    assert cache


def test_mutating_shell_clears_cache() -> None:
    settings = _settings()
    cache = {f"{ROOT}/a.py": b"old"}
    tool = build_shell_tool(
        settings, _FakeSandbox(settings), read_cache=cache  # type: ignore[arg-type]
    )

    tool.invoke({"command": "sed -i 's/old/new/' a.py", "reason": "修改文件"})

    assert cache == {}


# ---------------- 有界 LRU ----------------

def test_read_cache_evicts_least_recently_used_by_bytes() -> None:
    cache = ReadCache(max_bytes=10)
    cache["a"] = b"12345"
    cache["b"] = b"67890"

    cache.get("a")  # a 变成最近使用
    cache["c"] = b"abc"  # 需要腾出 3 字节，应淘汰 b

    assert "a" in cache
    assert "b" not in cache
    assert cache["c"] == b"abc"
    assert cache.evictions == 1
    assert cache.size_bytes <= 10


def test_read_cache_skips_values_larger_than_the_limit() -> None:
    cache = ReadCache(max_bytes=4)
    cache["big"] = b"12345"
    assert "big" not in cache
    assert cache.size_bytes == 0


def test_read_cache_clear_and_stats() -> None:
    cache = ReadCache(max_bytes=100)
    cache["a"] = b"1"
    cache.get("a")
    cache.get("missing")
    stats = cache.snapshot()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    cache.clear()
    assert cache.size_bytes == 0
    assert len(cache) == 0


def test_read_cache_is_thread_safe() -> None:
    """并发只读工具共享同一个缓存；混合读写/驱逐不能抛 KeyError。"""
    from concurrent.futures import ThreadPoolExecutor

    cache = ReadCache(max_bytes=1_024)

    def worker(index: int) -> int:
        for turn in range(500):
            key = f"{index}-{turn % 20}"
            cache[key] = b"x" * 40
            cache.get(key)
            if turn % 7 == 0:
                cache.pop(f"{index}-{turn % 3}", None)
        return index

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(pool.map(worker, range(8))) == list(range(8))

    assert cache.size_bytes <= cache.max_bytes
