"""Web 验证界面。

**模块级不再有 `app = create_app()`**（A10）：import 就建 app 并读 settings 是
副作用 —— `cli web` 导入 `create_app` 时会凭空多造一个默认 app。支持的用法是
调用方自己建：`uvicorn.run(create_app(settings), ...)`。

支持：
1. SSE 流式事件；
2. 会话历史恢复；
3. L2/L3 审批（ask 模式）；
4. 运行中 pause/cancel/add/replan；
5. 审批/暂停后的 resume。

刻意不引前端构建链：一个 HTML 文件 + 原生 JS。它不是主要交付界面（TUI 才是），
但端点已具备完整的实时控制能力。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from coding_agent.config import Settings, get_settings
from coding_agent.events import (
    ApprovalRequested,
    Event,
    RunFailed,
    SteeringApplied,
)
from coding_agent.memory.sessions import SessionIndex, format_table_rows
from coding_agent.runtime import AgentRuntime

STATIC_DIR = Path(__file__).parent / "static"

WEB_APPROVAL_MODE = "ask"


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1)
    thread_id: str = ""
    allow_write: bool = False


class SteerRequest(BaseModel):
    action: Literal["pause", "cancel", "add", "replan"]
    text: str = ""
    role: str = "explore"


class ResumeRequest(BaseModel):
    decisions: dict[str, bool] = Field(default_factory=dict)


def _sse(event: Event) -> str:
    """一条领域事件 → 一个 SSE 数据帧。事件本身就是 pydantic 模型，直接序列化。"""
    return f"data: {event.model_dump_json()}\n\n"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="CodingAgent", docs_url=None, redoc_url=None)
    # 挂起/运行中的 runtime 必须跨请求保留：审批和 steering 需要同一个
    # checkpointer 与 steering controller。
    runtimes: dict[str, AgentRuntime] = {}
    registry_lock = threading.Lock()

    def _require_runtime(thread_id: str) -> AgentRuntime:
        with registry_lock:
            runtime = runtimes.get(thread_id)
        if runtime is None:
            raise HTTPException(
                status_code=404, detail=f"会话 {thread_id} 没有运行中的 agent"
            )
        return runtime

    async def _drop_runtime(thread_id: str, runtime: AgentRuntime) -> None:
        with registry_lock:
            if runtimes.get(thread_id) is runtime:
                runtimes.pop(thread_id, None)
        await runtime.aclose()

    def _is_suspended(event: Event) -> bool:
        return isinstance(event, ApprovalRequested) or (
            isinstance(event, SteeringApplied) and event.action == "pause"
        )

    def _sse_response(
        thread_id: str,
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
                if not suspended:
                    await _drop_runtime(thread_id, runtime)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Thread-Id": thread_id,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    async def health() -> dict:
        """API 联通测试：不调用模型，只报配置是否齐备。"""
        from coding_agent.llm.deepseek import MissingApiKeyError, build_llm

        payload = {
            "model": settings.deepseek_model,
            "endpoint": settings.deepseek_base_url,
            "tracing": settings.langsmith_tracing,
            "workspace": None,
            "api_key": bool(settings.deepseek_api_key),
            "reachable": False,
            "error": "",
        }
        try:
            build_llm(settings, streaming=False)
            payload["reachable"] = True
        except MissingApiKeyError as exc:
            payload["error"] = str(exc)

        runtime = AgentRuntime(settings, approval_mode=WEB_APPROVAL_MODE)
        try:
            payload["workspace"] = runtime.workspace
        except Exception as exc:  # noqa: BLE001 - 沙箱不可用也要如实报出来
            # 不要覆盖已有诊断（如"未配置 API Key"）：两处问题都应可见
            detail = f"{type(exc).__name__}: {exc}"
            payload["error"] = f"{payload['error']}；{detail}" if payload["error"] else detail
        finally:
            await runtime.aclose()
        return payload

    @app.get("/api/sessions")
    async def sessions(limit: int = 20) -> dict:
        index_ = SessionIndex(settings.resolved_audit_dir)
        found = index_.list(limit=limit)
        return {
            "sessions": [
                {
                    "thread_id": row[0],
                    "last_active": row[1],
                    "prompts": int(row[2]),
                    "tool_calls": int(row[3]),
                    "title": row[4],
                }
                for row in format_table_rows(found)
            ]
        }

    @app.post("/api/run")
    async def run(request: RunRequest) -> StreamingResponse:
        thread_id = request.thread_id or uuid.uuid4().hex[:8]
        with registry_lock:
            if thread_id in runtimes:
                raise HTTPException(
                    status_code=409,
                    detail="该会话已挂起或正在运行；请先 resume",
                )
            runtime = AgentRuntime(
                settings,
                allow_write=request.allow_write,
                approval_mode=WEB_APPROVAL_MODE,
            )
            runtimes[thread_id] = runtime
        return _sse_response(
            thread_id, runtime, runtime.run(request.prompt, thread_id=thread_id)
        )

    @app.post("/api/steer/{thread_id}")
    async def steer(thread_id: str, request: SteerRequest) -> dict:
        runtime = _require_runtime(thread_id)
        try:
            runtime.steer(
                thread_id,
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
        thread_id: str, request: ResumeRequest
    ) -> StreamingResponse:
        runtime = _require_runtime(thread_id)
        return _sse_response(
            thread_id,
            runtime,
            runtime.resume(thread_id, request.decisions),
        )

    @app.post("/api/resume_steering/{thread_id}")
    async def resume_steering(thread_id: str) -> StreamingResponse:
        runtime = _require_runtime(thread_id)
        return _sse_response(
            thread_id, runtime, runtime.resume_steering(thread_id)
        )

    @app.get("/api/status/{thread_id}")
    async def status(thread_id: str) -> dict:
        with registry_lock:
            runtime = runtimes.get(thread_id)
        if runtime is None:
            return {"exists": False, "thread_id": thread_id, "paused": False}
        return {
            "exists": True,
            "thread_id": thread_id,
            "paused": bool(runtime.is_paused(thread_id)),
        }

    @app.get("/api/thread/{thread_id}")
    async def thread_history(
        thread_id: str, limit: int | None = None, offset: int = 0
    ) -> dict:
        """会话历史：新会话切回来时用于恢复上下文。支持 limit/offset 分页。"""
        runtime = AgentRuntime(settings, approval_mode=WEB_APPROVAL_MODE)
        try:
            history = await runtime.history(
                thread_id, limit=limit, offset=offset
            )
        except Exception as exc:  # noqa: BLE001 - 拿不到历史不该让页面报错
            raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc
        finally:
            await runtime.aclose()
        return {"messages": [{"role": m.role, "text": m.text} for m in history]}

    return app
