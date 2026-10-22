"""WSL2 沙箱执行器。

脚本通过 **stdin** 传给 `bash -l -s`，而不是走 argv —— 这样完全绕开
Windows 命令行层的引号/转义规则，命令内容不会被二次解释。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar, cast
from uuid import uuid4

from coding_agent.concurrency import ResourceTimeout, SyncLeasePool
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

# `_argv()` 用 `env -i` 清空了宿主环境，HOME 也随之丢失。登录 bash 在
# 非交互模式下不会自动从 passwd 补 HOME，所以每次执行前补一次（只补一次，
# 常驻会话后续脚本会继承）。刻意不让 bash 在启动时读到 HOME，避免它
# source `~/.bash_profile` 再把宿主凭据导回来。
# 允许显式放行的构建变量名；即使误配，这些名字也不会带出去。
_SENSITIVE_ENV_NAME = re.compile(
    r"(?i)(?:^|_)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL"
    r"|AUTH|COOKIE|SESSION)(?:_|$)"
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_ENV_PREAMBLE = """\
if [ -z "${HOME:-}" ]; then
  HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)"
fi
if [ -z "${HOME:-}" ]; then
  HOME="$(eval echo "~$(id -un)" 2>/dev/null)"
fi
[ -n "${HOME:-}" ] && export HOME
"""


def _with_env_preamble(script: str) -> str:
    if script.startswith(_ENV_PREAMBLE):
        return script
    return _ENV_PREAMBLE + script

_T = TypeVar("_T")


class WslUnavailableError(RuntimeError):
    """本机找不到可用的 WSL 发行版。"""


# wsl.exe 自己的错误（不是工作区命令的输出）。只认这些前缀/短语，
# 避免把用户命令 stderr 里出现的同名文本误判成 WSL 故障。
_WSL_TRANSPORT_MARKERS = (
    "wsl_e_",
    "wslregisterdistribution failed",
    "the windows subsystem for linux has not been properly configured",
    "there is no distribution with the supplied name",
    "the system cannot find the file specified",
    "catastrophic failure",
    "error code: wsl/",
    "wsl.exe:",
)


def _is_wsl_transport_failure(result: ExecResult) -> bool:
    if result.timed_out or result.ok:
        return False
    text = (result.stderr or "").lower()
    return any(marker in text for marker in _WSL_TRANSPORT_MARKERS)


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


def _emit_function(capture_limit: int) -> str:
    """生成常驻会话里回传 stdout/stderr 的 bash 函数。

    `capture_limit<=0` 保持旧行为（完整 cat）；否则只回传 head+tail，
    中间用一行省略标记代替。这样大输出不会先进入宿主内存再被截断。
    """
    if capture_limit <= 0:
        return "__agent_emit() { printf '\\n%s\\n' \"$2\"; cat \"$1\"; }"
    half = max(1, capture_limit // 2)
    return (
        "__agent_emit() {\n"
        "  local f=\"$1\"; local marker=\"$2\"; local size\n"
        "  printf '\\n%s\\n' \"$marker\"\n"
        "  size=$(wc -c < \"$f\" | tr -d ' ')\n"
        f"  if [ \"$size\" -le {capture_limit} ]; then cat \"$f\"; else\n"
        f"    head -c {half} \"$f\"\n"
        f"    printf '\\n...<省略 %s 字节>...\\n' \"$((size - {capture_limit}))\"\n"
        f"    tail -c {half} \"$f\"\n"
        "  fi\n"
        "}"
    )


def _communicate_limited(
    proc: subprocess.Popen,
    input_bytes: bytes,
    *,
    timeout: float,
    limit: int,
) -> tuple[bytes, bytes, bool, bool, bool]:
    """带超时与字节上限的 communicate。

    返回 `(stdout, stderr, timed_out, stdout_truncated, stderr_truncated)`。
    超过上限后仍然继续读取并丢弃，避免子进程因管道写满而卡死。
    """
    out_parts: list[bytes] = []
    err_parts: list[bytes] = []
    truncated = [False, False]

    def reader(stream, parts: list[bytes], index: int) -> None:
        total = 0
        while True:
            try:
                chunk = stream.read(65_536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            if limit <= 0:
                parts.append(chunk)
            elif total < limit:
                take = min(len(chunk), limit - total)
                parts.append(chunk[:take])
                total += take
                if take < len(chunk):
                    truncated[index] = True
            else:
                truncated[index] = True

    def writer() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(input_bytes)
                proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    readers: list[threading.Thread] = []
    for stream, parts, index in (
        (proc.stdout, out_parts, 0),
        (proc.stderr, err_parts, 1),
    ):
        if stream is None:
            continue
        thread = threading.Thread(
            target=reader, args=(stream, parts, index), daemon=True
        )
        thread.start()
        readers.append(thread)
    writer_thread = threading.Thread(target=writer, daemon=True)
    writer_thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
        # 子进程被杀后，管道可能仍被后代进程持有；关闭读端让 reader 退出。
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    for thread in readers:
        thread.join(KILL_GRACE)
    writer_thread.join(KILL_GRACE)
    return (
        b"".join(out_parts),
        b"".join(err_parts),
        timed_out,
        truncated[0],
        truncated[1],
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
        # 句柄可能已经被 wait 关闭；阻止 Popen.__del__ 再去 poll 一个失效句柄。
        if self._proc.returncode is None:
            self._proc.returncode = -1
        # 显式关闭管道，避免 Windows 上 Popen.__del__ 在句柄失效后再 poll。
        for pipe in (
            self._proc.stdin,
            self._proc.stdout,
            self._proc.stderr,
        ):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    def run(
        self,
        script: str,
        *,
        wall: int,
        command: str,
        capture_limit: int = 0,
    ) -> ExecResult:
        token = uuid4().hex
        end_line = f"__AGENT_END_{token}__:".encode()
        # 在沙箱内就把输出截到上限：命令写入临时文件，回传时只 cat
        # head+tail。否则宿主必须先收完整份输出再在 render() 里截断。
        framed = f"""\
{_emit_function(capture_limit)}
__agent_out=$(mktemp)
__agent_err=$(mktemp)
(
{script}
) >"$__agent_out" 2>"$__agent_err"
__agent_rc=$?
__agent_emit "$__agent_out" "__AGENT_OUT_{token}__"
__agent_emit "$__agent_err" "__AGENT_ERR_{token}__"
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
    """小型会话池：保留并发能力，同时避免每条命令启动一次 wsl.exe。

    创建/关闭可能来自不同线程（一个 worker 的会话失败会触发整体回退），
    所以池本身也必须是有状态的：`closed` 之后不再接受新命令，`close()`
    同时清理 idle 与正在执行的会话。
    """

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
        self._active: set[_ShellSession] = set()
        self._closed = False

    def run(
        self,
        script: str,
        *,
        wall: int,
        command: str,
        capture_limit: int = 0,
    ) -> ExecResult:
        with self._semaphore:
            session = self._take()
            try:
                return session.run(
                    script,
                    wall=wall,
                    command=command,
                    capture_limit=capture_limit,
                )
            finally:
                if session.alive():
                    self._put(session)
                else:
                    with self._lock:
                        self._active.discard(session)
                    session.close()

    def _take(self) -> _ShellSession:
        with self._lock:
            if self._closed:
                raise _SessionError("persistent shell 池已关闭")
            if self._idle:
                session = self._idle.pop()
                self._active.add(session)
                return session
        session = _ShellSession(self._argv)
        with self._lock:
            if self._closed:
                session.close()
                raise _SessionError("persistent shell 池已关闭")
            self._active.add(session)
        if self._on_start is not None:
            self._on_start()
        return session

    def _put(self, session: _ShellSession) -> None:
        with self._lock:
            self._active.discard(session)
            if self._closed:
                session.close()
            else:
                self._idle.append(session)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sessions = [*self._idle, *self._active]
            self._idle.clear()
            self._active.clear()
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
    extra_binds: tuple[str, ...] = (),
) -> str:
    """把一条命令包进 bubblewrap，返回可直接交给 bash 的脚本片段。

    工作区（及其工作目录）以原路径可写挂回；其余只读或隐藏。`workdir` 必须存在，
    否则 bwrap 会因为 `--chdir` 目标缺失而失败。`extra_binds` 用于仓库外的
    per-user 记忆目录等宿主管理路径。`network=False` 时额外 `--unshare-net`。
    """
    rw: list[str] = []
    for path in (workdir, workspace, *extra_binds):
        if path and path not in rw:
            rw.append(path)

    args = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        # IPC 也隔离：System V 共享内存/信号量不应跨沙箱可见。
        "--unshare-ipc",
        "--cap-drop",
        "ALL",
    ]
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

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        slots: SyncLeasePool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._bwrap_ok: bool | None = None
        # 探测类结果的缓存。每探测一次就要启动一次 wsl.exe（约 0.3s），而这些值
        # 在一次运行内不会变。挂在实例上而不是模块级，这样每个沙箱（也就是每次
        # 运行、每个测试）各记各的，不会跨运行串味。
        self._probe_cache: dict[str, object] = {}
        self._probe_lock = threading.Lock()
        # 并发 worker 会各自调沙箱；没有这层背压时 4 个 worker 的一次工具
        # 轮次就能同时拉起十几个 wsl.exe。0 表示不限制。
        # 默认自建一个池；runtime 会传入共享的 governor.wsl，让主 agent、
        # worker 与探测共用同一组槽位。
        self._slots = slots or SyncLeasePool(
            "wsl",
            int(getattr(self.settings, "sandbox_max_concurrent_commands", 0) or 0),
            reserve=int(
                getattr(self.settings, "concurrency_wsl_reserve", 1) or 0
            ),
        )
        # 基于命令 p95 延迟的弹性并发。只在池子饱和时调整 —— 否则一条本来
        # 就慢的构建命令会让并发被无谓压低。
        self._latencies: list[int] = []
        self._latency_steps = 0
        self._wsl_limit_floor = max(
            1, int(getattr(settings, "concurrency_wsl_min", 2) or 2)
        )
        self._wsl_limit_ceiling = int(
            getattr(settings, "concurrency_wsl_max", 0)
            or self._slots.limit
            or 0
        )
        if self._wsl_limit_ceiling:
            self._wsl_limit_floor = min(
                self._wsl_limit_floor, self._wsl_limit_ceiling
            )
        self._command_count = 0
        self._session_pool: _ShellSessionPool | None = None
        self._session_disabled = False
        # 会话池的创建/关闭可能来自不同线程；指标更新也有复合操作。
        self._session_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        # bwrap 只绑定工作区；仓库外的 per-user 记忆目录需要显式可写绑定。
        self._bind_lock = threading.Lock()
        self._extra_binds: set[str] = set()
        # 健康检查与熔断状态。所有跨线程访问都走 `_breaker_lock`。
        self._breaker_lock = threading.Lock()
        self._breaker_failures = 0
        self._breaker_open_until = 0.0
        self._breaker_half_open = False
        self._health_ok: bool | None = None
        self._health_checked_at = 0.0

    @property
    def command_count(self) -> int:
        """本实例启动过多少次 wsl.exe（供评测/调试统计）。"""
        with self._stats_lock:
            return self._command_count

    def _bump_command_count(self) -> None:
        with self._stats_lock:
            self._command_count += 1

    def close(self) -> None:
        """关闭常驻会话；进程退出时也会随之结束。"""
        with self._session_lock:
            pool = self._session_pool
            self._session_pool = None
        if pool is not None:
            pool.close()

    def __del__(self) -> None:  # pragma: no cover - 解释器退出时的兜底清理
        try:
            self.close()
        except Exception:  # noqa: BLE001 - 析构阶段不能再抛异常
            pass

    # ---------------- 健康检查 / 熔断 ----------------

    def circuit_state(self) -> str:
        """closed / open / half-open，供日志与 doctor 使用。"""
        with self._breaker_lock:
            if self._breaker_failures < self._breaker_threshold:
                return "closed"
            if time.monotonic() < self._breaker_open_until:
                return "open"
            return "half-open"

    @property
    def _breaker_threshold(self) -> int:
        return max(1, int(self.settings.wsl_breaker_failures or 1))

    def _breaker_allow(self) -> None:
        """熔断打开时 fail fast；冷却到期只放一个半开探测请求。"""
        with self._breaker_lock:
            if self._breaker_failures < self._breaker_threshold:
                return
            now = time.monotonic()
            if now < self._breaker_open_until:
                remaining = max(0.0, self._breaker_open_until - now)
                raise WslUnavailableError(
                    f"WSL 连续失败 {self._breaker_failures} 次，已熔断"
                    f"（约 {remaining:.0f}s 后重试）。请检查 WSL 或运行 agent doctor。"
                )
            if self._breaker_half_open:
                raise WslUnavailableError(
                    "WSL 恢复探测正在进行，请稍后重试。"
                )
            self._breaker_half_open = True

    def _breaker_success(self) -> None:
        with self._breaker_lock:
            self._breaker_failures = 0
            self._breaker_open_until = 0.0
            self._breaker_half_open = False
            self._health_ok = True
            self._health_checked_at = time.monotonic()

    def _breaker_failure(self) -> None:
        with self._breaker_lock:
            self._breaker_failures += 1
            self._breaker_half_open = False
            if self._breaker_failures < self._breaker_threshold:
                return
            base = max(0.1, float(self.settings.wsl_breaker_cooldown_s or 0.1))
            cap = max(base, float(self.settings.wsl_breaker_max_cooldown_s or base))
            exponent = self._breaker_failures - self._breaker_threshold
            cooldown = min(base * (2**exponent), cap)
            self._breaker_open_until = time.monotonic() + cooldown
            self._health_ok = False
            self._health_checked_at = time.monotonic()

    def health(self, *, force: bool = False) -> bool:
        """轻量健康探针：不经过 bwrap/常驻会话，也不受熔断拦截。

        `force=False` 时结果在 TTL 内复用，避免每个工具调用都启动 wsl.exe。
        """
        if not self.settings.wsl_health_check_enabled:
            return True
        now = time.monotonic()
        ttl = max(0.0, float(self.settings.wsl_health_ttl_s or 0.0))
        if (
            not force
            and self._health_ok is not None
            and now - self._health_checked_at <= ttl
        ):
            return bool(self._health_ok)

        timeout = max(1, int(self.settings.wsl_health_timeout_s or 1))
        try:
            result = self._exec(
                "echo __AGENT_HEALTH_OK__",
                wall=timeout,
                reserved=True,
            )
        except ResourceTimeout:
            # 本地槽位都在忙，不代表 WSL 坏了。
            return False
        except Exception:  # noqa: BLE001 - 健康检查失败只返回 False
            self._breaker_failure()
            return False
        ok = result.ok and "__AGENT_HEALTH_OK__" in (result.stdout or "")
        if ok:
            self._breaker_success()
        else:
            self._breaker_failure()
        return ok

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
        # 探测 $HOME 必须绕过 bwrap：bwrap 需要已知 workdir，而 workdir 的
        # 默认值正是这里要探测的 $HOME/agent-ws。用 _exec 直接跑一次性 bash。
        result = self._exec(
            'printf "%s" "$HOME"',
            wall=max(1, int(self.settings.shell_timeout or 60)),
            command='printf "%s" "$HOME"',
        )
        home = result.stdout.strip()
        if not result.ok or not home:
            raise WslUnavailableError(
                f"无法获取 {self.settings.wsl_distro} 的 $HOME：{result.render(500)}"
            )
        return home

    def _argv(self) -> list[str]:
        # `env -i` 清空从 Windows 继承的环境：CI token、云凭据、甚至
        # `apply_tracing_env()` 写回的 LANGSMITH_API_KEY 都会随 wsl.exe
        # 进入 Linux 进程，而 `printenv` 曾经是 L0 自动放行。
        # 只保留最小的、与业务无关的运行环境；HOME 由登录 bash 从 passwd 推导。
        args = [
            "wsl.exe",
            "-d",
            self.settings.wsl_distro,
            "--",
            "/usr/bin/env",
            "-i",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG=C.UTF-8",
            "LC_ALL=C.UTF-8",
            "TERM=dumb",
        ]
        # 显式允许的构建变量（默认空）。敏感名称即使写进 allowlist 也忽略。
        for raw in str(self.settings.shell_env_allowlist or "").split(","):
            name = raw.strip()
            if not name or not _ENV_NAME.fullmatch(name):
                continue
            if _SENSITIVE_ENV_NAME.search(name):
                continue
            value = os.environ.get(name)
            if value is not None:
                args.append(f"{name}={value}")
        args += ["/bin/bash", "-l", "-s"]
        return args

    @property
    def limits(self) -> ResourceLimits:
        settings = self.settings
        return ResourceLimits(
            cpu_seconds=settings.shell_cpu_seconds,
            memory_mb=settings.shell_memory_mb,
            max_file_mb=settings.shell_max_file_mb,
            max_processes=settings.shell_max_processes,
        )

    @property
    def capture_limit(self) -> int:
        """单条命令 stdout/stderr 各自允许捕获的字节数；0 = 不限制。"""
        return max(0, int(getattr(self.settings, "max_capture_bytes", 0) or 0))

    def add_extra_bind(self, path: str) -> None:
        """把仓库外路径加入 bwrap 可写绑定（仅 off 模式无影响）。"""
        try:
            normalized = normalize_root(path)
        except Exception:  # noqa: BLE001 - 非法路径不加入绑定
            return
        with self._bind_lock:
            self._extra_binds.add(normalized)

    def ensure_directory(self, path: str) -> None:
        """在沙箱内创建目录（绕过 bwrap，供宿主初始化仓库外状态目录）。"""
        normalized = normalize_root(path)
        result = self._exec(
            f"mkdir -p {shlex.quote(normalized)}",
            wall=max(1, int(self.settings.shell_timeout or 60)),
            command="",
        )
        if not result.ok:
            raise WslUnavailableError(
                f"无法创建目录 {normalized}：{result.render(300)}"
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
        capture_limit: int | None = None,
    ) -> ExecResult:
        """执行一条命令。

        `capture_limit` 只应由**用户命令/验证命令**传入。文件工具等内部脚本
        通过 stdout 传 base64/JSON 协议，截断会直接破坏协议，因此默认 0
        （不限制），由调用方显式选择上限。
        """
        wall = timeout if timeout is not None else self.settings.shell_timeout
        limit = 0 if capture_limit is None else max(0, int(capture_limit))
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
            with self._bind_lock:
                extra_binds = tuple(sorted(self._extra_binds))
            body = build_bwrap_script(
                command,
                workdir=workdir,
                workspace=self.settings.wsl_workspace,
                network=self.settings.shell_network,
                extra_binds=extra_binds,
            )
        else:
            body = command if cwd is None else f"cd {shlex.quote(cwd)} && {command}"

        # 沙箱内再套一层 timeout：外层的 proc.kill() 只能杀掉 wsl.exe，
        # Linux 侧的子进程要靠这一层才能确定性清理。
        script = wrap_with_limits(body, limits=self.limits, wall_seconds=wall)

        # 配置类错误（bwrap 不可用）在到达这里之前就已经抛出了，不计入熔断。
        self._breaker_allow()
        try:
            if self.settings.persistent_shell and not self._session_disabled:
                try:
                    result = self._run_in_session(
                        script,
                        wall=wall,
                        command=command,
                        capture_limit=limit,
                    )
                except Exception:  # noqa: BLE001 - 会话失败退回一次性执行
                    with self._session_lock:
                        self._session_disabled = True
                    self.close()
                else:
                    self._finish_run(result)
                    return result
            result = self._exec(
                script,
                wall=wall,
                command=command,
                capture_limit=limit,
            )
        except ResourceTimeout:
            # 本地槽位饱和不是 WSL 故障，不能计入熔断。
            raise
        except Exception:
            # 到这里说明连一次性 wsl.exe 都没跑起来（或会话/进程层故障）。
            self._breaker_failure()
            raise
        self._finish_run(result)
        return result

    def _observe_latency(self, result: ExecResult) -> None:
        """在池子饱和时按命令 p95 延迟微调并发上限。

        饱和 + 高延迟 = 争用，降一个槽；饱和 + 低延迟 = 还有余量，升一个。
        非饱和时命令本身就慢，不能据此惩罚并发。

        `_latencies` / `_latency_steps` 会被多个线程同时更新，复合操作必须
        串行化，否则自适应上限会抖动。
        """
        settings = self.settings
        if (
            not getattr(settings, "concurrency_wsl_adaptive", True)
            or result.timed_out
            or self._slots.limit <= 0
        ):
            return
        window = max(
            5, int(getattr(settings, "concurrency_wsl_latency_window", 20) or 20)
        )
        with self._stats_lock:
            self._latencies.append(int(result.duration_ms or 0))
            del self._latencies[:-window]
            if len(self._latencies) < window:
                return
            self._latency_steps += 1
            # 每半个窗口评估一次，避免刚调完又立刻反向调。
            if self._latency_steps < max(1, window // 2):
                return
            self._latency_steps = 0

            ordered = sorted(self._latencies)
            p95 = ordered[int(0.95 * (len(ordered) - 1))]
            target = max(
                100,
                int(getattr(settings, "concurrency_wsl_target_ms", 1500) or 1500),
            )
            floor = self._wsl_limit_floor
            ceiling = self._wsl_limit_ceiling
            if ceiling <= 0:
                return
            saturated = self._slots.in_flight >= self._slots.limit
            if not saturated:
                return
            if p95 > target * 1.5 and self._slots.limit > floor:
                self._slots.set_limit(self._slots.limit - 1)
            elif p95 < target * 0.5 and self._slots.limit < ceiling:
                self._slots.set_limit(self._slots.limit + 1)

    def _finish_run(self, result: ExecResult) -> None:
        """一次真实执行结束后更新熔断状态：有结果 = WSL 活着。"""
        self._observe_latency(result)
        if _is_wsl_transport_failure(result):
            self._breaker_failure()
        else:
            self._breaker_success()

    def _run_in_session(
        self,
        script: str,
        *,
        wall: int,
        command: str,
        capture_limit: int = 0,
    ) -> ExecResult:
        with self._session_lock:
            pool = self._session_pool
            if pool is None:
                size = max(1, int(self.settings.persistent_shell_pool))
                pool = _ShellSessionPool(
                    self._argv(),
                    size,
                    on_start=self._bump_command_count,
                )
                self._session_pool = pool
        timeout = float(
            getattr(self.settings, "concurrency_wsl_timeout_s", 30.0) or 30.0
        )
        with self._slots.lease(timeout=timeout):
            return pool.run(
                _with_env_preamble(script),
                wall=wall,
                command=command,
                capture_limit=capture_limit,
            )

    def _exec(
        self,
        script: str,
        *,
        wall: int,
        command: str = "",
        reserved: bool = False,
        capture_limit: int = 0,
    ) -> ExecResult:
        """按统一 WSL 资源池执行；具体逻辑见 `_exec_locked`。"""
        timeout = float(
            getattr(self.settings, "concurrency_wsl_timeout_s", 30.0) or 30.0
        )
        with self._slots.lease(timeout=timeout, reserved=reserved):
            return self._exec_locked(
                _with_env_preamble(script),
                wall=wall,
                command=command,
                capture_limit=capture_limit,
            )

    def _exec_locked(
        self,
        script: str,
        *,
        wall: int,
        command: str = "",
        capture_limit: int = 0,
    ) -> ExecResult:
        """把脚本经 stdin 交给登录 bash 执行（不套隔离包装，供 run/探测复用）。"""
        self._bump_command_count()
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

        out, err, timed_out, out_cut, err_cut = _communicate_limited(
            proc,
            script.encode("utf-8"),
            timeout=limit,
            limit=capture_limit,
        )
        exit_code = (
            TIMEOUT_EXIT_CODE
            if timed_out or proc.returncode is None
            else proc.returncode
        )

        duration_ms = int((time.perf_counter() - started) * 1000)
        # 沙箱内 timeout 到点返回约定码 124；但命令自身也可能恰好以 124 退出，
        # 仅凭码会误报。真正的超时一定跑满了整段墙钟时间，据此区分。
        if not timed_out and exit_code == TIMEOUT_EXIT_CODE and duration_ms >= wall * 1000 * 0.9:
            timed_out = True

        stdout = out.decode("utf-8", errors="replace")
        stderr = err.decode("utf-8", errors="replace")
        if out_cut:
            stdout += "\n...<输出捕获已截断>..."
        if err_cut:
            stderr += "\n...<输出捕获已截断>..."

        return ExecResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
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
