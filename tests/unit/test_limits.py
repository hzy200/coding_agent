from __future__ import annotations

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
    wrapped = wrap_with_limits("echo hi", limits=ResourceLimits(), wall_seconds=30)
    assert "timeout --kill-after=" in wrapped
    assert " 30 " in wrapped
    # 脚本体经 heredoc 走 stdin，不作为 argv 传给内层 bash
    assert "bash -s <<'__AGENT_SCRIPT_EOF__'" in wrapped
    assert wrapped.rstrip().endswith("__AGENT_SCRIPT_EOF__")
    assert "\necho hi\n" in wrapped


def test_no_timeout_when_wall_seconds_falsy() -> None:
    for wall in (None, 0):
        wrapped = wrap_with_limits("echo hi", limits=ResourceLimits(), wall_seconds=wall)
        assert "timeout" not in wrapped
        assert "echo hi" in wrapped


def test_preamble_precedes_the_command() -> None:
    wrapped = wrap_with_limits(
        "echo hi", limits=ResourceLimits(cpu_seconds=5), wall_seconds=None
    )
    assert wrapped.index("ulimit") < wrapped.index("echo hi")


def test_body_travels_over_stdin_not_argv() -> None:
    """脚本体不能出现在内层 bash 的 argv 里。

    单条 argv 有内核上限 MAX_ARG_STRLEN（128 KiB），而 `fs.write_text` 会把整份
    文件内容 base64 后内联进脚本（膨胀 4/3）—— 一旦走 argv，写入约 96 KB 以上
    必然 `Argument list too long`（exit 126），而读上限却宣称 2 MB。
    """
    body = "x = '" + "y" * 400_000 + "'"
    wrapped = wrap_with_limits(body, limits=ResourceLimits(), wall_seconds=10)

    header = next(line for line in wrapped.splitlines() if "bash -s" in line)
    assert body not in header  # 关键：大块内容不在命令行上
    assert body in wrapped  # 但它确实在脚本里（只是经 stdin 传入）


def test_heredoc_delimiter_moves_when_the_body_contains_it() -> None:
    """脚本体里出现结束标记时换一个，否则 heredoc 会提前收尾。"""
    body = "echo __AGENT_SCRIPT_EOF__\necho after"
    wrapped = wrap_with_limits(body, limits=ResourceLimits(), wall_seconds=10)
    assert "bash -s <<'__AGENT_SCRIPT_EOF___'" in wrapped
    assert wrapped.rstrip().endswith("__AGENT_SCRIPT_EOF___")


def test_body_is_not_reinterpreted_by_an_outer_shell() -> None:
    """模型拼出的第二条命令仍在脚本体里，不会变成宿主 shell 的第二条命令。"""
    wrapped = wrap_with_limits(
        "echo hi; rm -rf /", limits=ResourceLimits(), wall_seconds=10
    )
    assert "\necho hi; rm -rf /\n" in wrapped
