"""Web 验证界面。

面向公网/多用户的约束：
- 所有 `/api/*`（除公开 `/healthz`）都要求 Bearer Token 或 loopback 匿名；
- runtime 按 `(user, thread)` 隔离，不能跨用户读取/恢复；
- workspace / audit / checkpoint / steering / memory 均按租户隔离；
- 单进程运行：WSL/LLM/worker 配额由进程级 governor 控制；
- 安全头、请求体上限、按用户限流在中间件/依赖层完成。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from coding_agent.concurrency import ConcurrencyGovernor
from coding_agent.config import Settings, apply_security_profile, get_settings
from coding_agent.events import (
    ApprovalRequested,
    Event,
    RunFailed,
    SteeringApplied,
)
from coding_agent.memory.sessions import SessionIndex, format_table_rows
from coding_agent.runtime import AgentRuntime
from coding_agent.tenancy import TenantError, TenantManager, WebTenant
from coding_agent.web.auth import AuthConfig, AuthUser
from coding_agent.web.middleware import (
    BodySizeLimitMiddleware,
    FixedWindowRateLimiter,
    SecurityHeadersMiddleware,
)

STATIC_DIR = Path(__file__).parent / "static"

WEB_APPROVAL_MODE = "ask"
_THREAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def current_user(request: Request) -> AuthUser:
    """从 app.state 读取认证配置，避免把 token 表放进依赖闭包。"""
    config: AuthConfig = request.app.state.auth
    return config.user_from_request(request)


UserDep = Annotated[AuthUser, Depends(current_user)]


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    thread_id: str = Field(default="", max_length=64)
    # Web 刻意不提供写会话：所有写操作都要求本机 CLI/TUI 的显式 --write。
    # 旧版这里有一个 allow_write 字段，但从未传给 AgentRuntime，属于误导性 API。


class SteerRequest(BaseModel):
    action: Literal["pause", "cancel", "add", "replan"]
    text: str = Field(default="", max_length=4_000)
    role: str = "explore"


class ResumeRequest(BaseModel):
    decisions: dict[str, bool] = Field(default_factory=dict)


@dataclass
class RuntimeEntry:
    runtime: AgentRuntime
    last_active: float = field(default_factory=time.monotonic)


def _sse(event: Event) -> str:
    """一条领域事件 → 一个 SSE 数据帧。事件本身就是 pydantic 模型，直接序列化。"""
    return f"data: {event.model_dump_json()}\n\n"


def _is_suspended(event: Event) -> bool:
    return isinstance(event, ApprovalRequested) or (
        isinstance(event, SteeringApplied) and event.action == "pause"
    )


def _check_thread_id(thread_id: str) -> str:
    text = str(thread_id or "").strip()
    if not text:
        return uuid.uuid4().hex[:12]
    if not _THREAD_RE.match(text):
        raise HTTPException(
            status_code=422,
            detail="thread_id 只允许字母、数字、下划线、连字符，长度 1–64",
        )
    return text


def create_app(
    settings: Settings | None = None,
    auth: AuthConfig | None = None,
    *,
    hsts: bool = False,
) -> FastAPI:
    settings = apply_security_profile(settings or get_settings())
    auth = auth or AuthConfig.from_settings(settings)
    app = FastAPI(title="CodingAgent", docs_url=None, redoc_url=None)

    # 单进程内所有租户共享一套资源池。
    governor = ConcurrencyGovernor(settings)
    tenants = TenantManager(
        settings, governor=governor, multi_user=auth.enabled
    )

    runtimes: dict[tuple[str, str], RuntimeEntry] = {}
    registry_lock = threading.Lock()
    limiter = FixedWindowRateLimiter(per_minute=settings.web_rate_limit_per_min)
    max_runtimes = max(1, int(settings.web_max_runtimes or 1))
    max_per_user = max(1, int(settings.web_max_runtimes_per_user or 1))
    runtime_ttl = max(0, int(settings.web_runtime_ttl_s or 0))

    def _tenant(user: AuthUser) -> WebTenant:
        try:
            return tenants.for_user(user.name)
        except TenantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 沙箱/目录不可用
            raise HTTPException(
                status_code=503,
                detail=f"工作区不可用：{type(exc).__name__}: {exc}",
            ) from exc

    def _rate_limit(user: AuthUser) -> None:
        if limiter.allow(user.name):
            return
        retry = limiter.retry_after(user.name)
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁",
            headers={"Retry-After": str(retry)},
        )

    async def _sweep_expired() -> None:
        if runtime_ttl <= 0:
            return
        now = time.monotonic()
        expired: list[AgentRuntime] = []
        with registry_lock:
            for key, entry in list(runtimes.items()):
                # 正在流式执行的 runtime 不能关：它还在 yield 事件。
                if getattr(entry.runtime, "_stream_active", False):
                    continue
                if now - entry.last_active > runtime_ttl:
                    runtimes.pop(key, None)
                    expired.append(entry.runtime)
        for runtime in expired:
            try:
                await runtime.aclose()
            except Exception:  # noqa: BLE001 - 清理失败不影响新请求
                pass

    def _count_user(user: str) -> int:
        return sum(1 for key in runtimes if key[0] == user)

    def _new_runtime(tenant: WebTenant) -> AgentRuntime:
        with registry_lock:
            total = len(runtimes)
            per_user = _count_user(tenant.user)
        if total >= max_runtimes or per_user >= max_per_user:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"运行中的会话数已达上限"
                    f"（用户 {per_user}/{max_per_user}，全局 {total}/{max_runtimes}）"
                ),
            )
        # Web 默认逐条确认；但 production/deny 档不能被这里的 ask 覆盖。
        mode = (
            "deny"
            if str(tenant.settings.approval_mode).lower() == "deny"
            else WEB_APPROVAL_MODE
        )
        return AgentRuntime(
            tenant.settings,
            governor=governor,
            approval_mode=mode,
        )

    async def _drop_runtime(key: tuple[str, str], runtime: AgentRuntime) -> None:
        with registry_lock:
            entry = runtimes.get(key)
            if entry is not None and entry.runtime is runtime:
                runtimes.pop(key, None)
        try:
            await runtime.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不覆盖原始事件
            pass

    def _get_entry(user: str, public_thread: str) -> RuntimeEntry | None:
        with registry_lock:
            entry = runtimes.get((user, public_thread))
        if entry is not None:
            entry.last_active = time.monotonic()
        return entry

    async def _runtime_for_resume(
        user: AuthUser, public_thread: str
    ) -> tuple[tuple[str, str], AgentRuntime]:
        key = (user.name, public_thread)
        entry = _get_entry(*key)
        if entry is not None:
            return key, entry.runtime
        # 进程重启/TTL 清理后，从 per-user checkpoint 重建。
        tenant = _tenant(user)
        runtime = _new_runtime(tenant)
        with registry_lock:
            runtimes[key] = RuntimeEntry(runtime)
        return key, runtime

    def _sse_response(
        key: tuple[str, str],
        public_thread: str,
        runtime: AgentRuntime,
        events: AsyncIterator[Event],
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            suspended = False
            try:
                async for event in events:
                    if _is_suspended(event):
                        suspended = True
                    yield _sse(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 错误以事件形式送到前端
                yield _sse(RunFailed(message=f"{type(exc).__name__}: {exc}"))
            finally:
                if suspended:
                    entry = _get_entry(*key)
                    if entry is not None:
                        entry.last_active = time.monotonic()
                else:
                    await _drop_runtime(key, runtime)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Thread-Id": public_thread,
            },
        )

    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=settings.web_max_body_bytes
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    app.state.auth = auth
    app.state.tenants = tenants
    app.state.governor = governor

    @app.get("/healthz")
    async def healthz() -> dict:
        """公开存活探针：不暴露模型、Key、工作区等配置。"""
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/health")
    async def health(
        user: UserDep,
    ) -> dict:
        """认证后的联通测试：不调用模型，只报配置是否齐备。"""
        from coding_agent.llm.deepseek import MissingApiKeyError, build_llm

        try:
            tenant = tenants.for_user(user.name)
        except Exception as exc:  # noqa: BLE001 - 沙箱不可用也要可读报出
            return {
                "model": settings.deepseek_model,
                "endpoint": settings.deepseek_base_url,
                "tracing": settings.langsmith_tracing,
                "workspace": None,
                "api_key": bool(settings.deepseek_api_key),
                "reachable": bool(settings.deepseek_api_key),
                "error": f"{type(exc).__name__}: {exc}",
                "user": user.name,
            }
        payload = {
            "model": tenant.settings.deepseek_model,
            "endpoint": tenant.settings.deepseek_base_url,
            "tracing": tenant.settings.langsmith_tracing,
            "workspace": tenant.workspace,
            "api_key": bool(tenant.settings.deepseek_api_key),
            "reachable": False,
            "error": "",
            "user": user.name,
        }
        try:
            build_llm(tenant.settings, streaming=False)
            payload["reachable"] = True
        except MissingApiKeyError as exc:
            payload["error"] = str(exc)
        return payload

    @app.get("/api/workspace")
    async def workspace(user: UserDep) -> dict:
        tenant = _tenant(user)
        return {"user": user.name, "workspace": tenant.workspace}

    @app.get("/api/sessions")
    async def sessions(
        user: UserDep,
        limit: int = 20,
    ) -> dict:
        await _sweep_expired()
        tenant = _tenant(user)
        index_ = SessionIndex(tenant.settings.resolved_audit_dir)
        found = index_.list(limit=limit)
        return {
            "sessions": [
                {
                    "thread_id": tenant.public_thread(row[0]),
                    "last_active": row[1],
                    "prompts": int(row[2]),
                    "tool_calls": int(row[3]),
                    "title": row[4],
                }
                for row in format_table_rows(found)
            ]
        }

    @app.post("/api/run")
    async def run(
        request: RunRequest,
        user: UserDep,
    ) -> StreamingResponse:
        _rate_limit(user)
        await _sweep_expired()
        if len(request.prompt) > settings.web_max_prompt_chars:
            raise HTTPException(
                status_code=413,
                detail=f"prompt 超过上限 {settings.web_max_prompt_chars} 字符",
            )
        public_thread = _check_thread_id(request.thread_id)
        key = (user.name, public_thread)
        with registry_lock:
            if key in runtimes:
                raise HTTPException(
                    status_code=409,
                    detail="该会话已挂起或正在运行；请先 resume",
                )
        tenant = _tenant(user)
        runtime = _new_runtime(tenant)
        with registry_lock:
            runtimes[key] = RuntimeEntry(runtime)
        return _sse_response(
            key,
            public_thread,
            runtime,
            runtime.run(
                request.prompt,
                thread_id=tenant.thread_id(public_thread),
            ),
        )

    @app.post("/api/steer/{thread_id}")
    async def steer(
        thread_id: str,
        request: SteerRequest,
        user: UserDep,
    ) -> dict:
        _rate_limit(user)
        public_thread = _check_thread_id(thread_id)
        entry = _get_entry(user.name, public_thread)
        if entry is None:
            raise HTTPException(
                status_code=404, detail="该会话没有运行中的 agent"
            )
        tenant = _tenant(user)
        try:
            entry.runtime.steer(
                tenant.thread_id(public_thread),
                request.action,
                text=request.text,
                role=request.role,
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "action": request.action}

    @app.post("/api/resume/{thread_id}")
    async def resume(
        thread_id: str,
        request: ResumeRequest,
        user: UserDep,
    ) -> StreamingResponse:
        _rate_limit(user)
        await _sweep_expired()
        public_thread = _check_thread_id(thread_id)
        key, runtime = await _runtime_for_resume(user, public_thread)
        tenant = _tenant(user)
        return _sse_response(
            key,
            public_thread,
            runtime,
            runtime.resume(
                tenant.thread_id(public_thread), request.decisions
            ),
        )

    @app.post("/api/resume_steering/{thread_id}")
    async def resume_steering(
        thread_id: str,
        user: UserDep,
    ) -> StreamingResponse:
        _rate_limit(user)
        await _sweep_expired()
        public_thread = _check_thread_id(thread_id)
        key, runtime = await _runtime_for_resume(user, public_thread)
        tenant = _tenant(user)
        internal = tenant.thread_id(public_thread)
        # 只有 steering 暂停才能走这个入口。审批挂起的 resume payload 形状
        # 不同；误用它会让 approval_gate 把所有待批调用按 fail-closed 拒绝。
        if not runtime.is_paused(internal):
            raise HTTPException(
                status_code=409,
                detail="该会话未处于 steering 暂停状态；审批恢复请用 /api/resume",
            )
        return _sse_response(
            key,
            public_thread,
            runtime,
            runtime.resume_steering(internal),
        )

    @app.get("/api/status/{thread_id}")
    async def status(
        thread_id: str,
        user: UserDep,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        entry = _get_entry(user.name, public_thread)
        if entry is None:
            return {
                "exists": False,
                "thread_id": public_thread,
                "paused": False,
            }
        tenant = _tenant(user)
        return {
            "exists": True,
            "thread_id": public_thread,
            "paused": bool(
                entry.runtime.is_paused(tenant.thread_id(public_thread))
            ),
        }

    @app.get("/api/thread/{thread_id}")
    async def thread_history(
        thread_id: str,
        user: UserDep,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        tenant = _tenant(user)
        runtime = AgentRuntime(
            tenant.settings,
            governor=governor,
            approval_mode=WEB_APPROVAL_MODE,
        )
        try:
            history = await runtime.history(
                tenant.thread_id(public_thread),
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001 - 拿不到历史不该让页面报错
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {
            "messages": [
                {"role": message.role, "text": message.text}
                for message in history
            ]
        }

    return app
