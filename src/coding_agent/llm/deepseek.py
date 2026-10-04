"""DeepSeek V4 客户端。

DeepSeek 提供 OpenAI 兼容协议，因此直接复用 langchain-openai 的 ChatOpenAI，
只替换 base_url 与模型名。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from coding_agent.config import Settings


class MissingApiKeyError(RuntimeError):
    """未配置 DeepSeek API Key。"""


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
