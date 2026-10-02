"""全局配置。

约定：环境变量名与业界惯例保持一致（如 ``DEEPSEEK_API_KEY``），
Agent 自有配置统一加 ``AGENT_`` 前缀。优先级：环境变量 > .env > 默认值。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ---------- DeepSeek ----------
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")

    # ---------- LangSmith ----------
    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: str = Field(default="", alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="coding-agent", alias="LANGSMITH_PROJECT")

    # ---------- WSL2 沙箱 ----------
    wsl_distro: str = Field(default="Ubuntu", alias="AGENT_WSL_DISTRO")
    # 留空表示自动使用沙箱内 $HOME/agent-ws，避免与真实用户名不匹配
    wsl_workspace: str = Field(default="", alias="AGENT_WSL_WORKSPACE")
    win_workspace: str = Field(default="", alias="AGENT_WIN_WORKSPACE")
    shell_timeout: int = Field(default=60, alias="AGENT_SHELL_TIMEOUT")
    max_output_chars: int = Field(default=20_000, alias="AGENT_MAX_OUTPUT_CHARS")
    max_file_read_bytes: int = Field(default=2_000_000, alias="AGENT_MAX_FILE_READ_BYTES")

    # ---------- 持久化与审计 ----------
    # 留空表示自动用 <当前目录>/.agent/checkpoints.sqlite；填 :memory: 强制不落盘
    checkpoint_path: str = Field(default="", alias="AGENT_CHECKPOINT_PATH")
    audit_enabled: bool = Field(default=True, alias="AGENT_AUDIT_ENABLED")
    # 留空表示自动用 <当前目录>/.agent/audit
    audit_dir: str = Field(default="", alias="AGENT_AUDIT_DIR")

    # ---------- Agent 循环 ----------
    max_repair_rounds: int = Field(default=3, alias="AGENT_MAX_REPAIR_ROUNDS")
    max_tool_rounds: int = Field(default=12, alias="AGENT_MAX_TOOL_ROUNDS")
    max_plan_steps: int = Field(default=5, alias="AGENT_MAX_PLAN_STEPS")
    llm_temperature: float = Field(default=0.0, alias="AGENT_LLM_TEMPERATURE")
    llm_timeout: int = Field(default=120, alias="AGENT_LLM_TIMEOUT")

    @property
    def resolved_checkpoint_path(self) -> str:
        """checkpoint 落盘位置；空配置落到当前目录的 .agent/ 下。"""
        return self.checkpoint_path or str(Path.cwd() / ".agent" / "checkpoints.sqlite")

    @property
    def resolved_audit_dir(self) -> Path:
        return Path(self.audit_dir) if self.audit_dir else Path.cwd() / ".agent" / "audit"

    def apply_tracing_env(self) -> None:
        """把配置写回 os.environ，供 langsmith 自动读取。"""
        if not self.langsmith_tracing:
            os.environ["LANGSMITH_TRACING"] = "false"
            return
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_PROJECT"] = self.langsmith_project
        if self.langsmith_api_key:
            os.environ["LANGSMITH_API_KEY"] = self.langsmith_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.apply_tracing_env()
    return settings
