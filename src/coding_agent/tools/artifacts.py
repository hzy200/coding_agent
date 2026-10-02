"""工具的结构化产物（artifact）与其封装格式。

工具除了回灌给模型的文本，还要产出一份机器可读的结果。事件层据此渲染
时间线、审计层据此写日志 —— 都不需要去解析工具的文本输出。

为什么不用 LangChain 的 `response_format="content_and_artifact"`：
实测（langchain-core 1.6.6）`tool.invoke()` 只返回 content，artifact 被丢弃；
该语义只在 `ToolNode` 内部生效，而 `ToolNode` 在 langgraph 1.2.12 下无法脱离图单独调用。
这条契约是事件层与审计层的地基，不适合押在语义不明的框架行为上，
因此改为工具返回一个自描述的 JSON 封装，由宿主显式拆开。

封装格式：`{"text": <给模型看的文本>, "artifact": <结构化产物>}`
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

_TEXT_KEY = "text"
_ARTIFACT_KEY = "artifact"
_ENVELOPE_KEYS = {_TEXT_KEY, _ARTIFACT_KEY}


class ToolArtifact(BaseModel):
    """所有工具产物的共同基类。`kind` 是判别字段。"""

    kind: str = ""


class ShellArtifact(ToolArtifact):
    kind: Literal["shell"] = "shell"

    command: str = ""
    ok: bool = False
    exit_code: int | None = None
    duration_ms: int | None = None
    timed_out: bool = False
    # 被安全策略拦下时为 True，此时命令根本没有执行
    rejected: bool = False
    level: int | None = None
    level_label: str = ""


class FileArtifact(ToolArtifact):
    kind: Literal["file"] = "file"

    path: str = ""
    # read / create / overwrite / edit
    action: str = ""
    ok: bool = False
    # 被路径守卫或规则拒绝时为 True，此时文件未被触碰
    rejected: bool = False
    bytes_written: int = 0
    lines_read: int = 0
    lines_total: int = 0
    added: int = 0
    removed: int = 0
    diff: str = ""
    snapshot_id: str | None = None


_ARTIFACT_TYPES: dict[str, type[ToolArtifact]] = {
    "shell": ShellArtifact,
    "file": FileArtifact,
}


def parse_artifact(data: Any) -> ToolArtifact | None:
    """按 kind 还原工具产物；无法识别时返回 None。"""
    if not isinstance(data, dict):
        return None
    model = _ARTIFACT_TYPES.get(str(data.get("kind", "")))
    if model is None:
        return None
    try:
        return model.model_validate(data)
    except ValidationError:
        return None


def pack(text: str, artifact: BaseModel | dict[str, Any]) -> str:
    """把文本与产物封装成工具返回值。"""
    payload = artifact if isinstance(artifact, dict) else artifact.model_dump()
    return json.dumps({_TEXT_KEY: text, _ARTIFACT_KEY: payload}, ensure_ascii=False)


def unpack(raw: Any) -> tuple[str, dict[str, Any] | None]:
    """拆开工具返回值。

    非封装格式（不带 artifact 的工具）原样返回，artifact 为 None。
    键集合必须严格匹配，避免误吞工具自己返回的合法 JSON。
    """
    if not isinstance(raw, str):
        return str(raw), None
    try:
        data = json.loads(raw)
    except ValueError:
        return raw, None
    if not isinstance(data, dict) or set(data) != _ENVELOPE_KEYS:
        return raw, None
    artifact = data.get(_ARTIFACT_KEY)
    return str(data.get(_TEXT_KEY, "")), artifact if isinstance(artifact, dict) else None
