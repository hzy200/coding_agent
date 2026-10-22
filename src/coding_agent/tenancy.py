"""Web 租户隔离：每个用户独立的 workspace、宿主数据目录与 WSL 记忆目录。

关键边界：
- workspace / memory 在 WSL 内，由 `sandbox.run(mkdir -p)` 创建；
- audit / checkpoint / steering 在**宿主**文件系统（AuditLogger、SQLite、
  SteeringController 都跑在宿主）；
- 用户只能得到 `workspaces_root/<user>`，不能指定任意主机路径。
"""

from __future__ import annotations

import re
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path

from coding_agent.config import Settings
from coding_agent.sandbox.pathguard import ensure_inside, normalize_root
from coding_agent.sandbox.wsl_exec import WslSandbox

_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class TenantError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WebTenant:
    user: str
    multi_user: bool
    workspace: str
    settings: Settings
    data_dir: Path | None = None
    memory_dir: str = ""

    def thread_id(self, public_thread: str) -> str:
        """内部 thread id；多用户加 user 前缀，避免共享限流文件里串号。"""
        return f"{self.user}:{public_thread}" if self.multi_user else public_thread

    def public_thread(self, internal: str) -> str:
        prefix = f"{self.user}:"
        if self.multi_user and internal.startswith(prefix):
            return internal[len(prefix) :]
        return internal


class TenantManager:
    def __init__(
        self,
        settings: Settings,
        *,
        sandbox: WslSandbox | None = None,
        governor: object | None = None,
        multi_user: bool | None = None,
    ) -> None:
        self.settings = settings
        # Web 层不应直接构造沙箱（架构约束）；由这里集中处理，
        # 并优先复用传入的 governor 的 WSL 槽位。
        slots = getattr(governor, "wsl", None) if governor is not None else None
        # 租户目录创建是**宿主管理动作**（mkdir/$HOME），不执行模型意图。
        # untrusted profile 的 bwrap 需要显式 workdir 且只绑定工作区，
        # 用它创建尚不存在的用户目录会失败；这里用 off 模式。
        provision_settings = settings.model_copy(
            update={"shell_sandbox": "off", "persistent_shell": False}
        )
        self.sandbox = sandbox or WslSandbox(  # type: ignore[arg-type]
            provision_settings, slots=slots
        )
        self.multi_user = (
            bool(multi_user)
            if multi_user is not None
            else bool(settings.web_auth_tokens or settings.web_auth_tokens_file)
        )
        self._tenants: dict[str, WebTenant] = {}
        self._lock = threading.Lock()
        self._home: str | None = None

    @property
    def home(self) -> str:
        if self._home is None:
            self._home = self.sandbox.home()
        return self._home

    @property
    def data_root(self) -> Path:
        configured = str(self.settings.web_data_root or "").strip()
        return (
            Path(configured)
            if configured
            else Path.cwd() / ".agent" / "web" / "users"
        )

    def _workspaces_root(self) -> str:
        configured = str(self.settings.web_workspaces_root or "").strip()
        if configured:
            return normalize_root(configured)
        return f"{self.home}/agent-ws/users"

    def _memory_root(self) -> str:
        configured = str(self.settings.web_memory_root or "").strip()
        if configured:
            return normalize_root(configured)
        return f"{self.home}/agent-memory"

    @staticmethod
    def _safe_user(user: str) -> str:
        text = str(user or "").strip()
        if not _USER_RE.match(text) or text in {".", ".."}:
            raise TenantError(f"非法用户名：{user!r}")
        return text

    def _mkdir_wsl(self, path: str) -> None:
        result = self.sandbox.run(f"mkdir -p {shlex.quote(path)}")
        if not result.ok:
            raise TenantError(f"无法创建目录 {path}：{result.render(300)}")

    def for_user(self, user: str) -> WebTenant:
        safe = self._safe_user(user)
        with self._lock:
            cached = self._tenants.get(safe)
            if cached is not None:
                return cached

        # 未启用认证时是单用户本地模式：沿用原有路径，不强制加 user 子目录。
        if not self.multi_user:
            workspace = normalize_root(
                str(self.settings.wsl_workspace or f"{self.home}/agent-ws")
            )
            tenant = WebTenant(
                user=safe,
                multi_user=False,
                workspace=workspace,
                settings=self.settings,
                memory_dir=str(self.settings.memory_root or ""),
            )
        else:
            workspaces_root = self._workspaces_root()
            memory_root = self._memory_root()
            workspace = ensure_inside(
                f"{workspaces_root}/{safe}", workspaces_root
            )
            memory_dir = ensure_inside(f"{memory_root}/{safe}", memory_root)
            self._mkdir_wsl(workspace)
            self._mkdir_wsl(memory_dir)

            data_dir = self.data_root / safe
            data_dir.mkdir(parents=True, exist_ok=True)
            tenant_settings = self.settings.model_copy(
                update={
                    "wsl_workspace": workspace,
                    "audit_dir": str(data_dir / "audit"),
                    "checkpoint_path": str(data_dir / "checkpoints.sqlite"),
                    "steering_dir": str(data_dir / "steering"),
                    "memory_root": memory_dir,
                }
            )
            tenant = WebTenant(
                user=safe,
                multi_user=True,
                workspace=workspace,
                settings=tenant_settings,
                data_dir=data_dir,
                memory_dir=memory_dir,
            )

        with self._lock:
            self._tenants[safe] = tenant
        return tenant
