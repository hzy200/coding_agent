"""DeepSeek V4 客户端。

DeepSeek 提供 OpenAI 兼容协议，因此直接复用 langchain-openai 的 ChatOpenAI，
只替换 base_url 与模型名。
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from coding_agent.config import Settings


class MissingApiKeyError(RuntimeError):
    """未配置 DeepSeek API Key。"""


def with_json_output(llm: ChatOpenAI, schema: type) -> Any:
    """结构化输出，走 JSON 模式而不是默认的 json_schema。

    **不要改回默认方式。** `with_structured_output(schema)` 走 OpenAI 的
    `json_schema` response_format，而 DeepSeek 端点对这个直接返回 400
    （`This response_format type is unavailable now`，v4-flash / chat / reasoner
    都一样）。而 planner 把异常静默吞掉、降级成「整条请求当作一步」——
    于是一个**完全失效的规划，看起来和正常工作一模一样**，直到有人去翻审计
    才发现每次的计划都是请求原文。

    `method="json_mode"` 走老式的 `json_object`，DeepSeek 支持。代价是 schema
    不随请求下发，**必须写进提示词**（见 prompts.py 里各处的「输出格式」一节）。
    `method="function_calling"` 也不行：v4-flash 的 thinking 模式不支持强制
    `tool_choice`。

    真实 provider 上的可用性由 `tests/integration/test_llm_structured_output.py`
    守着（标 `llm`，需要 API Key）。
    """
    return llm.with_structured_output(schema, method="json_mode")


def build_llm(settings: Settings, *, streaming: bool = True) -> ChatOpenAI:
    if not settings.deepseek_api_key:
        raise MissingApiKeyError(
            "未配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入密钥。"
        )
    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
        max_retries=2,
        streaming=streaming,
        # 流式响应也带回 usage，运行结束才能在审计里记 token 用量
        stream_usage=True,
    )
