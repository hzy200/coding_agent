"""项目长期记忆工具：让模型在运行中检索/补充 `.agent/memory.md`。

为什么需要它：记忆此前只在 run 启动时整份注入 system prompt，模型无法在
中途按需查询，也无法把「这轮确认下来的稳定约定」记下来。注入改成 top-K 之后，
检索能力必须交给模型自己，否则与请求无关的事实会被挤出去。

边界：
- `memory_search` 只读，可被 explore 子智能体使用；
- `memory_save` 是 L1 写，只在 `--write` 会话暴露，且仍受 `MAX_FACTS` 约束；
- 两者都经 `LongTermMemory`，文件位置与 CLI `/remember` 完全一致。
"""

from __future__ import annotations

from collections.abc import MutableMapping

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.memory.longterm import (
    MAX_FACT_CHARS,
    MAX_FACTS,
    LongTermMemory,
    MemoryError,
    resolve_memory_root,
)
from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import CallArtifact, pack

MEMORY_SEARCH = "memory_search"
MEMORY_SAVE = "memory_save"

DEFAULT_SEARCH_LIMIT = 8


class MemorySearchInput(BaseModel):
    query: str = Field(
        default="",
        description="检索关键词或一句话描述；留空则返回最早的几条记忆",
    )
    limit: int = Field(
        default=DEFAULT_SEARCH_LIMIT,
        ge=1,
        le=MAX_FACTS,
        description=f"最多返回多少条（上限 {MAX_FACTS}）",
    )
    reason: str = Field(description="为什么要查项目记忆，一句话说明")


class MemorySaveInput(BaseModel):
    fact: str = Field(
        min_length=1,
        max_length=MAX_FACT_CHARS,
        description="一条稳定的项目约定或事实，写成单句；不要记临时任务状态",
    )
    reason: str = Field(description="为什么要记住它，一句话说明")
    ttl_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description="可选：这条记忆多少天后过期；省略表示长期有效",
    )


def build_memory_tools(
    settings: Settings,
    sandbox: WslSandbox,
    *,
    allow_write: bool = False,
    read_cache: MutableMapping[str, bytes] | None = None,
) -> list[BaseTool]:
    workspace = resolve_workspace(settings, sandbox)
    memory_root = resolve_memory_root(settings, sandbox, workspace)
    fs = SandboxFs(sandbox, memory_root, cache=read_cache)
    memory = LongTermMemory(fs, workspace, state_root=memory_root)

    def _search(query: str, limit: int, reason: str) -> str:
        del reason
        facts = memory.search(query, limit)
        if not facts:
            return pack(
                "当前没有项目记忆。",
                CallArtifact(tool=MEMORY_SEARCH, ok=True),
            )
        total = len(memory.load())
        lines = "\n".join(f"- {fact}" for fact in facts)
        return pack(
            f"项目记忆（显示 {len(facts)} / {total} 条）：\n{lines}",
            CallArtifact(tool=MEMORY_SEARCH, ok=True),
        )

    def _save(fact: str, reason: str, ttl_days: int | None = None) -> str:
        del reason
        try:
            facts = memory.add(fact, source="model", ttl_days=ttl_days)
        except MemoryError as exc:
            return pack(
                f"记忆保存失败：{exc}",
                CallArtifact(tool=MEMORY_SAVE, ok=False),
            )
        note = ""
        if memory.last_replaced:
            note = "\n（已替换旧版本：" + "；".join(memory.last_replaced) + "）"
        expiry = f"（{ttl_days} 天后过期）" if ttl_days else ""
        return pack(
            f"已记住（当前 {len(facts)} 条）：{fact}{expiry}{note}",
            CallArtifact(tool=MEMORY_SAVE, ok=True),
        )

    tools: list[BaseTool] = [
        StructuredTool.from_function(
            func=_search,
            name=MEMORY_SEARCH,
            description=(
                "按关键词检索跨会话的项目记忆（`.agent/memory.md`）。只读。\n"
                "处理与项目约定、历史决策相关的问题时先查一次，不要凭猜测。"
            ),
            args_schema=MemorySearchInput,
        )
    ]
    if allow_write:
        tools.append(
            StructuredTool.from_function(
                func=_save,
                name=MEMORY_SAVE,
                description=(
                    "把一条**稳定的**项目约定/事实写入跨会话记忆。\n"
                    "只记后续会话仍然成立的内容（例如「测试用 pytest -q」）；"
                    "不要记本次任务的临时状态或进度。"
                ),
                args_schema=MemorySaveInput,
            )
        )
    return tools
