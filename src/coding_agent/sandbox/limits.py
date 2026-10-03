"""沙箱资源限制。

两层防护：

1. **沙箱内 `ulimit`** —— 限制 CPU 时间、单文件大小、进程数、地址空间。
   在目标进程真正所在的命名空间里生效，`--` 之外的任何写法都绕不过去。
2. **沙箱内 `timeout`** —— 外层 subprocess 超时只能杀掉 `wsl.exe`，
   Linux 侧的子进程未必跟着走。在沙箱内再套一层 `timeout`，
   超时就能确定性地清理掉整条命令。

`timeout` 触发的超时返回退出码 124（约定值），`WslSandbox` 据此判定 timed_out。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# GNU coreutils 的 timeout 在超时时返回这个码
TIMEOUT_EXIT_CODE = 124
# 超时后留给进程收尾、再由 KILL 兜底的秒数
KILL_AFTER_SECONDS = 5


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """0 表示不限制。默认只开 CPU / 文件大小 / 进程数。

    内存（`ulimit -v`）默认关闭：JVM、Node、编译器动辄索取远超实际使用的
    虚拟地址空间，贸然开启会把正常构建直接打死。
    """

    cpu_seconds: int = 600
    memory_mb: int = 0
    max_file_mb: int = 512
    max_processes: int = 1024

    @property
    def enabled(self) -> bool:
        return any((self.cpu_seconds, self.memory_mb, self.max_file_mb, self.max_processes))

    def ulimit_preamble(self) -> str:
        """生成 ulimit 设置片段。

        单项设置失败（例如文件系统不支持）不应让整条命令失败，
        因此每项单独容错；但整体仍用 `|| true` 兜底。
        """
        if not self.enabled:
            return ""

        flags: list[str] = []
        if self.cpu_seconds > 0:
            flags.append(f"ulimit -t {self.cpu_seconds} 2>/dev/null")
        if self.memory_mb > 0:
            flags.append(f"ulimit -v {self.memory_mb * 1024} 2>/dev/null")
        if self.max_file_mb > 0:
            flags.append(f"ulimit -f {self.max_file_mb * 1024} 2>/dev/null")
        if self.max_processes > 0:
            flags.append(f"ulimit -u {self.max_processes} 2>/dev/null")

        return " ; ".join(flags)


def wrap_with_limits(
    script: str,
    *,
    limits: ResourceLimits,
    wall_seconds: int | None,
    quote: Callable[[str], str],
) -> str:
    """把命令包上资源限制与超时。

    quote 传入 `shlex.quote`，避免本模块自己拼 shell 引号。
    """
    body = script
    if wall_seconds and wall_seconds > 0:
        body = (
            f"timeout --kill-after={KILL_AFTER_SECONDS} {wall_seconds} "
            f"bash -c {quote(body)}"
        )

    preamble = limits.ulimit_preamble()
    return f"{preamble}\n{body}" if preamble else body
