from __future__ import annotations

import shlex

from coding_agent.sandbox.limits import ResourceLimits, wrap_with_limits


def test_defaults_limit_cpu_file_and_processes_but_not_memory() -> None:
    limits = ResourceLimits()
    assert limits.enabled
    assert limits.cpu_seconds > 0
    assert limits.max_file_mb > 0
    assert limits.max_processes > 0
    # 内存默认关闭：JVM/Node/编译器会索取远超实际使用的虚拟地址空间
    assert limits.memory_mb == 0


def test_all_zero_means_no_preamble() -> None:
    limits = ResourceLimits(cpu_seconds=0, memory_mb=0, max_file_mb=0, max_processes=0)
    assert not limits.enabled
    assert limits.ulimit_preamble() == ""


def test_preamble_sets_each_enabled_limit() -> None:
    limits = ResourceLimits(
        cpu_seconds=120, memory_mb=512, max_file_mb=64, max_processes=256
    )
    preamble = limits.ulimit_preamble()
    assert "ulimit -t 120" in preamble
    assert f"ulimit -v {512 * 1024}" in preamble
    assert f"ulimit -f {64 * 1024}" in preamble
    assert "ulimit -u 256" in preamble


def test_zero_values_are_omitted() -> None:
    limits = ResourceLimits(cpu_seconds=0, memory_mb=0, max_file_mb=10, max_processes=0)
    preamble = limits.ulimit_preamble()
    assert "ulimit -t" not in preamble
    assert "ulimit -v" not in preamble
    assert "ulimit -u" not in preamble
    assert "ulimit -f" in preamble


def test_individual_ulimit_failures_do_not_abort_the_command() -> None:
    """单项 ulimit 不被支持时不应该让整条命令失败。"""
    assert "2>/dev/null" in ResourceLimits().ulimit_preamble()


# ---------------- wrap_with_limits ----------------

def test_wraps_with_inner_timeout() -> None:
    wrapped = wrap_with_limits(
        "echo hi", limits=ResourceLimits(), wall_seconds=30, quote=shlex.quote
    )
    assert "timeout --kill-after=" in wrapped
    assert " 30 " in wrapped
    assert wrapped.rstrip().endswith("bash -c 'echo hi'")


def test_no_timeout_when_wall_seconds_falsy() -> None:
    for wall in (None, 0):
        wrapped = wrap_with_limits(
            "echo hi", limits=ResourceLimits(), wall_seconds=wall, quote=shlex.quote
        )
        assert "timeout" not in wrapped
        assert "echo hi" in wrapped


def test_preamble_precedes_the_command() -> None:
    wrapped = wrap_with_limits(
        "echo hi",
        limits=ResourceLimits(cpu_seconds=5),
        wall_seconds=None,
        quote=shlex.quote,
    )
    assert wrapped.index("ulimit") < wrapped.index("echo hi")


def test_command_is_quoted_so_it_cannot_escape() -> None:
    """命令整体交给 bash -c，由宿主负责引用，模型无法拼出第二条命令。"""
    wrapped = wrap_with_limits(
        "echo hi; rm -rf /", limits=ResourceLimits(), wall_seconds=10, quote=shlex.quote
    )
    assert "'echo hi; rm -rf /'" in wrapped
