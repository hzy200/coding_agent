"""WSL2 沙箱执行器。

脚本通过 **stdin** 传给 `bash -l -s`，而不是走 argv —— 这样完全绕开
Windows 命令行层的引号/转义规则，命令内容不会被二次解释。
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass

from coding_agent.config import Settings, get_settings
from coding_agent.sandbox.limits import (
    TIMEOUT_EXIT_CODE,
    ResourceLimits,
    wrap_with_limits,
)
from coding_agent.sandbox.pathguard import normalize_root

# 外层 subprocess 比沙箱内的 timeout 多留一点余量，正常由内层先触发
OUTER_TIMEOUT_GRACE = 10


class WslUnavailableError(RuntimeError):
    """本机找不到可用的 WSL 发行版。"""


@dataclass(slots=True)
class ExecResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def render(self, max_chars: int = 20_000) -> str:
        """渲染为回灌给模型的紧凑文本。"""
        parts = [f"exit_code={self.exit_code}"]
        if self.timed_out:
            parts.append("timed_out=true")
        stdout, truncated_out = _truncate(self.stdout.strip(), max_chars)
        stderr, truncated_err = _truncate(self.stderr.strip(), max_chars)
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if truncated_out or truncated_err:
            parts.append("(输出已截断)")
        if not stdout and not stderr:
            parts.append("(无输出)")
        return "\n".join(parts)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n...<省略 {len(text) - limit} 字符>...\n{text[-tail:]}", True


def decode_wsl_output(raw: bytes) -> str:
    """解码 wsl.exe 的输出。

    wsl.exe 的输出编码随调用环境变化：挂在控制台下时是 UTF-16LE，
    被重定向/管道捕获时是 UTF-8。UTF-16LE 的 ASCII 文本每隔一字节就是 NUL，
    据此判别，避免解码错乱导致发行版名匹配不上。
    """
    if b"\x00" in raw:
        text = raw.decode("utf-16-le", errors="ignore")
    else:
        text = raw.decode("utf-8", errors="replace")
    return text


def parse_distro_list(raw: bytes) -> list[str]:
    names = []
    for line in decode_wsl_output(raw).splitlines():
        name = line.strip().strip("\ufeff\x00").strip()
        if name:
            names.append(name)
    return names


class WslSandbox:
    """在指定 WSL 发行版内执行 bash 命令。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ---------------- 环境探测 ----------------

    @staticmethod
    def list_distros() -> list[str]:
        """返回已安装的发行版名；WSL 不可用时返回空列表。"""
        try:
            proc = subprocess.run(
                ["wsl.exe", "-l", "-q"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        return parse_distro_list(proc.stdout)

    @classmethod
    def available(cls, distro: str) -> bool:
        return distro in cls.list_distros()

    # ---------------- 执行 ----------------

    def home(self) -> str:
        """沙箱内当前用户的 $HOME。"""
        result = self.run('printf "%s" "$HOME"')
        home = result.stdout.strip()
        if not result.ok or not home:
            raise WslUnavailableError(
                f"无法获取 {self.settings.wsl_distro} 的 $HOME：{result.render(500)}"
            )
        return home

    def _argv(self) -> list[str]:
        return [
            "wsl.exe",
            "-d",
            self.settings.wsl_distro,
            "--",
            "/bin/bash",
            "-l",
            "-s",
        ]

    @property
    def limits(self) -> ResourceLimits:
        settings = self.settings
        return ResourceLimits(
            cpu_seconds=settings.shell_cpu_seconds,
            memory_mb=settings.shell_memory_mb,
            max_file_mb=settings.shell_max_file_mb,
            max_processes=settings.shell_max_processes,
        )

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
    ) -> ExecResult:
        body = command if cwd is None else f"cd {shlex.quote(cwd)} && {command}"
        wall = timeout if timeout is not None else self.settings.shell_timeout
        # 沙箱内再套一层 timeout：外层的 proc.kill() 只能杀掉 wsl.exe，
        # Linux 侧的子进程要靠这一层才能确定性清理。
        script = wrap_with_limits(body, limits=self.limits, wall_seconds=wall, quote=shlex.quote)
        limit = wall + OUTER_TIMEOUT_GRACE

        started = time.perf_counter()
        try:
            proc = subprocess.Popen(
                self._argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise WslUnavailableError("找不到 wsl.exe，请确认已启用 WSL2。") from exc

        timed_out = False
        try:
            out, err = proc.communicate(script.encode("utf-8"), timeout=limit)
            exit_code = proc.returncode
            # 沙箱内的 timeout 到点会返回约定码；外层超时只是兜底，正常不会触发
            if exit_code == TIMEOUT_EXIT_CODE:
                timed_out = True
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            exit_code = TIMEOUT_EXIT_CODE
            timed_out = True

        return ExecResult(
            command=command,
            exit_code=exit_code,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            duration_ms=int((time.perf_counter() - started) * 1000),
            timed_out=timed_out,
        )


DEFAULT_WORKSPACE_DIRNAME = "agent-ws"


def resolve_workspace(settings: Settings, sandbox: WslSandbox) -> str:
    """工作区绝对路径：显式配置优先，否则用沙箱内 $HOME/agent-ws。

    不硬编码 /home/<user>，因为 WSL 发行版的默认用户因人而异。
    配置值统一归一化，容忍写成 `D:\\proj` 这类 Windows 路径。
    """
    if settings.wsl_workspace:
        return normalize_root(settings.wsl_workspace)
    return f"{sandbox.home()}/{DEFAULT_WORKSPACE_DIRNAME}"
