"""WSL2 沙箱执行器。

脚本通过 **stdin** 传给 `bash -l -s`，而不是走 argv —— 这样完全绕开
Windows 命令行层的引号/转义规则，命令内容不会被二次解释。
"""

from __future__ import annotations

import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar, cast
from uuid import uuid4

from coding_agent.config import Settings, get_settings
from coding_agent.sandbox.limits import (
    TIMEOUT_EXIT_CODE,
    ResourceLimits,
    wrap_with_limits,
)
from coding_agent.sandbox.pathguard import normalize_root

# 外层 subprocess 比沙箱内的 timeout 多留一点余量，正常由内层先触发
OUTER_TIMEOUT_GRACE = 10
# kill() 之后回收输出最多再等这么久：清理不该比一次命令本身更久
KILL_GRACE = 5

_T = TypeVar("_T")


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


class _SessionError(RuntimeError):
    pass


def _parse_session_output(
    raw: bytes, token: str
) -> tuple[str, str, int] | None:
    """从带 sentinel 的输出里拆出 stdout/stderr/exit_code。"""
    out_marker = f"\n__AGENT_OUT_{token}__\n".encode()
    err_marker = f"\n__AGENT_ERR_{token}__\n".encode()
    end_marker = f"\n__AGENT_END_{token}__:".encode()
    out_at = raw.find(out_marker)
    err_at = raw.find(err_marker)
    end_at = raw.find(end_marker)
    if out_at < 0 or err_at < 0 or end_at < 0 or not (out_at < err_at < end_at):
        return None
    stdout = raw[out_at + len(out_marker) : err_at]
    stderr = raw[err_at + len(err_marker) : end_at]
    rc_text = raw[end_at + len(end_marker) :].split(b"\n", 1)[0].strip()
    try:
        rc = int(rc_text)
    except ValueError:
        return None
    return (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        rc,
    )


class _ShellSession:
    """一个常驻的 WSL bash 进程；命令通过 stdin 发送并用 sentinel 分帧。"""

    def __init__(self, argv: list[str]) -> None:
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._lock = threading.Lock()

    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        try:
            self._proc.kill()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=KILL_GRACE)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def run(self, script: str, *, wall: int, command: str) -> ExecResult:
        token = uuid4().hex
        end_line = f"__AGENT_END_{token}__:".encode()
        framed = f"""\
__agent_out=$(mktemp)
__agent_err=$(mktemp)
(
{script}
) >"$__agent_out" 2>"$__agent_err"
__agent_rc=$?
printf '\\n__AGENT_OUT_{token}__\\n'
cat "$__agent_out"
printf '\\n__AGENT_ERR_{token}__\\n'
cat "$__agent_err"
printf '\\n__AGENT_END_{token}__:%s\\n' "$__agent_rc"
rm -f "$__agent_out" "$__agent_err"
"""
        started = time.perf_counter()
        with self._lock:
            if not self.alive():
                raise _SessionError("persistent shell 已退出")
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(framed.encode("utf-8"))
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise _SessionError(f"persistent shell 写入失败：{exc}") from exc

            timer = threading.Timer(
                wall + OUTER_TIMEOUT_GRACE, self._kill
            )
            timer.daemon = True
            timer.start()
            raw = bytearray()
            try:
                assert self._proc.stdout is not None
                while True:
                    line = self._proc.stdout.readline()
                    if not line:
                        break
                    raw += line
                    # readline 按行切分，结束标记行本身不带前导换行。
                    if end_line in line:
                        break
            finally:
                timer.cancel()

        duration_ms = int((time.perf_counter() - started) * 1000)
        parsed = _parse_session_output(bytes(raw), token)
        if parsed is None:
            return ExecResult(
                command=command,
                exit_code=TIMEOUT_EXIT_CODE,
                stdout="",
                stderr="persistent shell 未返回结束标记",
                duration_ms=duration_ms,
                timed_out=True,
            )
        stdout, stderr, rc = parsed
        timed_out = (
            rc == TIMEOUT_EXIT_CODE and duration_ms >= wall * 1000 * 0.9
        )
        return ExecResult(
            command=command,
            exit_code=rc,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    def _kill(self) -> None:
        try:
            self._proc.kill()
        except OSError:
            pass


class _ShellSessionPool:
    """小型会话池：保留并发能力，同时避免每条命令启动一次 wsl.exe。"""

    def __init__(
        self,
        argv: list[str],
        size: int,
        on_start: Callable[[], None] | None = None,
    ) -> None:
        self._argv = argv
        self._on_start = on_start
        self._semaphore = threading.BoundedSemaphore(max(1, size))
        self._lock = threading.Lock()
        self._idle: list[_ShellSession] = []

    def run(self, script: str, *, wall: int, command: str) -> ExecResult:
        with self._semaphore:
            session = self._take()
            try:
                return session.run(script, wall=wall, command=command)
            finally:
                if session.alive():
                    self._put(session)
                else:
                    session.close()

    def _take(self) -> _ShellSession:
        with self._lock:
            if self._idle:
                return self._idle.pop()
        session = _ShellSession(self._argv)
        if self._on_start is not None:
            self._on_start()
        return session

    def _put(self, session: _ShellSession) -> None:
        with self._lock:
            self._idle.append(session)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._idle)
            self._idle.clear()
        for session in sessions:
            session.close()


SHELL_SANDBOX_OFF = "off"
SHELL_SANDBOX_BWRAP = "bwrap"

# bubblewrap 的挂载布局：系统目录只读、`/home /root /tmp` 用空 tmpfs（真实家目录、
# 密钥、`/mnt` 下的 Windows 盘都不可见），只把工作区按原路径可写挂回来。
_BWRAP_RO_BINDS = (
    ("/usr", "/usr"),
    ("/bin", "/bin"),
    ("/lib", "/lib"),
    ("/etc", "/etc"),
)
_BWRAP_RO_BINDS_TRY = (("/lib64", "/lib64"), ("/sbin", "/sbin"))
_BWRAP_TMPFS = ("/tmp", "/home", "/root")


def build_bwrap_script(
    command: str,
    *,
    workdir: str,
    workspace: str = "",
    network: bool = True,
) -> str:
    """把一条命令包进 bubblewrap，返回可直接交给 bash 的脚本片段。

    工作区（及其工作目录）以原路径可写挂回；其余只读或隐藏。`workdir` 必须存在，
    否则 bwrap 会因为 `--chdir` 目标缺失而失败。`network=False` 时额外
    `--unshare-net`，命令看不到任何网络接口。
    """
    rw: list[str] = []
    for path in (workdir, workspace):
        if path and path not in rw:
            rw.append(path)

    args = ["bwrap", "--die-with-parent", "--unshare-user", "--unshare-pid"]
    if not network:
        args.append("--unshare-net")
    for src, dest in _BWRAP_RO_BINDS:
        args += ["--ro-bind", src, dest]
    for src, dest in _BWRAP_RO_BINDS_TRY:
        args += ["--ro-bind-try", src, dest]
    args += ["--proc", "/proc", "--dev", "/dev"]
    for path in _BWRAP_TMPFS:
        args += ["--tmpfs", path]
    for path in rw:
        args += ["--bind", path, path]
    args += ["--chdir", workdir, "--", "bash", "-lc", command]
    return " ".join(shlex.quote(part) for part in args)


class WslSandbox:
    """在指定 WSL 发行版内执行 bash 命令。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._bwrap_ok: bool | None = None
        # 探测类结果的缓存。每探测一次就要启动一次 wsl.exe（约 0.3s），而这些值
        # 在一次运行内不会变。挂在实例上而不是模块级，这样每个沙箱（也就是每次
        # 运行、每个测试）各记各的，不会跨运行串味。
        self._probe_cache: dict[str, object] = {}
        self._probe_lock = threading.Lock()
        # 并发 worker 会各自调沙箱；没有这层背压时 4 个 worker 的一次工具
        # 轮次就能同时拉起十几个 wsl.exe。0 表示不限制。
        limit = int(getattr(self.settings, "sandbox_max_concurrent_commands", 0) or 0)
        self._command_slots = (
            threading.BoundedSemaphore(limit) if limit > 0 else None
        )
        self._command_count = 0
        self._session_pool: _ShellSessionPool | None = None
        self._session_disabled = False

    @property
    def command_count(self) -> int:
        """本实例启动过多少次 wsl.exe（供评测/调试统计）。"""
        return self._command_count

    def _bump_command_count(self) -> None:
        self._command_count += 1

    def close(self) -> None:
        """关闭常驻会话；进程退出时也会随之结束。"""
        if self._session_pool is not None:
            self._session_pool.close()
            self._session_pool = None

    # ---------------- 探测缓存 ----------------

    def cached_probe(self, key: str, producer: Callable[[], _T]) -> _T:
        """记住一次探测的结果：`producer` 只在第一次调用时真正执行。

        `producer` 抛异常时什么都不记 —— 失败不该被缓存成"这个人没有 $HOME"。
        并发 worker 同时探测时用锁避免重复启动 wsl.exe。
        """
        with self._probe_lock:
            if key not in self._probe_cache:
                self._probe_cache[key] = producer()
            return cast(_T, self._probe_cache[key])

    def forget_probe(self, key: str) -> None:
        """丢掉一条探测缓存，用于「这次的结果不算数，下次重新探测」的情形。"""
        with self._probe_lock:
            self._probe_cache.pop(key, None)

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
        """沙箱内当前用户的 $HOME（探测结果缓存）。

        默认配置下（没显式设 `AGENT_WSL_WORKSPACE`）`resolve_workspace` 要走这里，
        而建图时每个工具构造器都会各调一次 —— 缓存之后这 9 次进程启动只剩 1 次。
        """
        return self.cached_probe("home", self._probe_home)

    def _probe_home(self) -> str:
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

    def bwrap_available(self) -> bool:
        """沙箱内是否可用 bubblewrap（探测结果缓存）。

        用 `_exec` 直接跑，避免 `run` 的隔离包装导致自举递归。
        """
        if self._bwrap_ok is None:
            probe = self._exec("command -v bwrap >/dev/null 2>&1", wall=self.settings.shell_timeout)
            self._bwrap_ok = probe.ok
        return self._bwrap_ok

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
    ) -> ExecResult:
        wall = timeout if timeout is not None else self.settings.shell_timeout
        if self.settings.shell_sandbox == SHELL_SANDBOX_BWRAP:
            if not self.bwrap_available():
                raise WslUnavailableError(
                    "AGENT_SHELL_SANDBOX=bwrap，但沙箱内 bubblewrap 不可用"
                    "（未安装或用户命名空间被禁用）。请安装 bubblewrap，"
                    "或改回 AGENT_SHELL_SANDBOX=off。"
                )
            workdir = cwd or self.settings.wsl_workspace
            if not workdir:
                raise WslUnavailableError(
                    "AGENT_SHELL_SANDBOX=bwrap 需要明确的工作区路径（AGENT_WSL_WORKSPACE 或 cwd）"
                )
            body = build_bwrap_script(
                command,
                workdir=workdir,
                workspace=self.settings.wsl_workspace,
                network=self.settings.shell_network,
            )
        else:
            body = command if cwd is None else f"cd {shlex.quote(cwd)} && {command}"

        # 沙箱内再套一层 timeout：外层的 proc.kill() 只能杀掉 wsl.exe，
        # Linux 侧的子进程要靠这一层才能确定性清理。
        script = wrap_with_limits(body, limits=self.limits, wall_seconds=wall)
        if self.settings.persistent_shell and not self._session_disabled:
            try:
                return self._run_in_session(
                    script, wall=wall, command=command
                )
            except Exception:  # noqa: BLE001 - 会话失败退回一次性执行
                self._session_disabled = True
                self.close()
        return self._exec(script, wall=wall, command=command)

    def _run_in_session(
        self, script: str, *, wall: int, command: str
    ) -> ExecResult:
        if self._session_pool is None:
            size = max(1, int(self.settings.persistent_shell_pool))
            self._session_pool = _ShellSessionPool(
                self._argv(),
                size,
                on_start=self._bump_command_count,
            )
        slots = self._command_slots
        if slots is None:
            return self._session_pool.run(script, wall=wall, command=command)
        with slots:
            return self._session_pool.run(script, wall=wall, command=command)

    def _exec(self, script: str, *, wall: int, command: str = "") -> ExecResult:
        """按实例并发上限执行；具体逻辑见 `_exec_locked`。"""
        slots = self._command_slots
        if slots is None:
            return self._exec_locked(script, wall=wall, command=command)
        with slots:
            return self._exec_locked(script, wall=wall, command=command)

    def _exec_locked(
        self, script: str, *, wall: int, command: str = ""
    ) -> ExecResult:
        """把脚本经 stdin 交给登录 bash 执行（不套隔离包装，供 run/探测复用）。"""
        self._command_count += 1
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
        except subprocess.TimeoutExpired:
            proc.kill()
            # 收尾这一次也必须有 timeout：kill 只保证 wsl.exe 收到信号，
            # 若它卡在不可中断状态（网络挂载、驱动），无参 communicate() 会永久挂住。
            try:
                out, err = proc.communicate(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                # 拖不住就放弃回收，照样按超时返回 —— 不能让清理拖垮整轮运行
                out, err = b"", b""
            exit_code = TIMEOUT_EXIT_CODE
            timed_out = True

        duration_ms = int((time.perf_counter() - started) * 1000)
        # 沙箱内 timeout 到点返回约定码 124；但命令自身也可能恰好以 124 退出，
        # 仅凭码会误报。真正的超时一定跑满了整段墙钟时间，据此区分。
        if not timed_out and exit_code == TIMEOUT_EXIT_CODE and duration_ms >= wall * 1000 * 0.9:
            timed_out = True

        return ExecResult(
            command=command,
            exit_code=exit_code,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
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
