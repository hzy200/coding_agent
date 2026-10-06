"""Web 最小验证界面。

定位是**最小验证**，不是主要交付界面（TUI 才是）。这里只证明三件事：

1. 流式对话能跑通（事件流直接变成 SSE 帧）
2. 多会话记忆能跨会话恢复
3. API 联通状态可见

刻意不引前端构建链：一个 HTML 文件 + 原生 JS。引了 Vite/React 之后，
这块的维护成本会盖过它作为"验证界面"的价值。

**审批固定为拒绝**（fail closed）：这里没有做审批交互，
与其让 L2/L3 命令悬在那里等一个永远不会来的答复，不如明确拒绝并在界面上说清楚。
需要审批能力请用 TUI。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from coding_agent.config import Settings, get_settings
from coding_agent.events import Event
from coding_agent.memory.sessions import SessionIndex, format_table_rows
from coding_agent.runtime import AgentRuntime

STATIC_DIR = Path(__file__).parent / "static"

# 单页界面固定拒绝变更类命令，理由见模块文档
WEB_APPROVAL_MODE = "deny"


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1)
    thread_id: str = ""
    allow_write: bool = False


def _sse(event: Event) -> str:
    """一条领域事件 → 一个 SSE 数据帧。事件本身就是 pydantic 模型，直接序列化。"""
    return f"data: {event.model_dump_json()}\n\n"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="CodingAgent", docs_url=None, redoc_url=None)

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
        import uuid

        thread_id = request.thread_id or uuid.uuid4().hex[:8]
        runtime = AgentRuntime(
            settings,
            allow_write=request.allow_write,
            approval_mode=WEB_APPROVAL_MODE,
        )

        async def stream() -> AsyncIterator[str]:
            import asyncio

            # RunStarted 由 runtime 在事件流开头发出（见 AgentRuntime._stream），
            # 前端不再自己造 —— 否则多前端各造一份，契约就散了。
            try:
                async for event in runtime.run(request.prompt, thread_id=thread_id):
                    yield _sse(event)
            except asyncio.CancelledError:  # 浏览器断开
                raise
            except Exception as exc:  # noqa: BLE001 - 错误要以事件形式送到前端
                from coding_agent.events import RunFailed

                yield _sse(RunFailed(message=f"{type(exc).__name__}: {exc}"))
            finally:
                await runtime.aclose()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Thread-Id": thread_id,
            },
        )

    @app.get("/api/thread/{thread_id}")
    async def thread_history(thread_id: str) -> dict:
        """会话历史：新会话切回来时用于恢复上下文。"""
        runtime = AgentRuntime(settings, approval_mode=WEB_APPROVAL_MODE)
        try:
            history = await runtime.history(thread_id)
        except Exception as exc:  # noqa: BLE001 - 拿不到历史不该让页面报错
            raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc
        finally:
            await runtime.aclose()
        return {"messages": [{"role": m.role, "text": m.text} for m in history]}

    return app


app = create_app()
