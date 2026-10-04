"""全局配置。

约定：环境变量名与业界惯例保持一致（如 ``DEEPSEEK_API_KEY``），
Agent 自有配置统一加 ``AGENT_`` 前缀。

**优先级：构造函数参数 > `.env` > 环境变量 > 默认值。**

通用约定是「环境变量 > .env」，这里刻意反过来。原因是本机踩到的实际故障：
harness 在进程启动时把陈旧的值注入进程环境，用户既看不到来源（不在注册表、
也不在任何配置文件里）也删不掉，于是改 `.env` 完全不生效 ——
表现为「明明更新了 API key 却一直 403」。

项目目录里的 `.env` 是**显式写下、看得见、可编辑**的意图，让它优先；
需要临时覆盖时用命令行前缀或环境变量改 `.env` 本身即可。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class _NonEmptyDotEnvSource(PydanticBaseSettingsSource):
    """给 `.env` 源加一层空值过滤：`KEY=` 这种占位不算「配置过」。

    否则从 `.env.example` 复制来的空行会盖掉环境变量里真正可用的值 ——
    那正是这套优先级最容易被吐槽的地方。

    **委托**而不是重建：`Settings(_env_file=None)` 这类显式覆盖必须继续生效，
    重建一个源会把它无视掉，测试里大量依赖这一点。
    """

    def __init__(self, inner: PydanticBaseSettingsSource) -> None:
        super().__init__(inner.settings_cls)
        self._inner = inner

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # 不使用：取值全部委托给 _inner

    def __call__(self) -> dict[str, Any]:
        data = self._inner()
        return {k: v for k, v in data.items() if v not in ("", None)}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        # 构造函数参数仍然最高优先 —— 测试与嵌入式用法依赖这一点
        return (
            init_settings,
            _NonEmptyDotEnvSource(dotenv_settings),
            env_settings,
            file_secret_settings,
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
    # shell 隔离后端：off（默认，仅工作区 cwd 约束）| bwrap（挂载命名空间隔离）
    # bwrap 模式下若沙箱内不可用，会**拒绝执行**（fail closed），不静默降级
    shell_sandbox: str = Field(default="off", alias="AGENT_SHELL_SANDBOX")
    shell_timeout: int = Field(default=60, alias="AGENT_SHELL_TIMEOUT")
    # 沙箱资源上限，0 表示不限制
    shell_cpu_seconds: int = Field(default=600, alias="AGENT_SHELL_CPU_SECONDS")
    shell_memory_mb: int = Field(default=0, alias="AGENT_SHELL_MEMORY_MB")
    shell_max_file_mb: int = Field(default=512, alias="AGENT_SHELL_MAX_FILE_MB")
    shell_max_processes: int = Field(default=1024, alias="AGENT_SHELL_MAX_PROCESSES")
    max_output_chars: int = Field(default=20_000, alias="AGENT_MAX_OUTPUT_CHARS")
    max_file_read_bytes: int = Field(default=2_000_000, alias="AGENT_MAX_FILE_READ_BYTES")

    # ---------- 上下文裁剪 ----------
    # 0 表示不裁剪；这些值作用于发给模型的历史，不改写会话状态
    context_max_chars: int = Field(default=60_000, alias="AGENT_CONTEXT_MAX_CHARS")
    context_keep_recent: int = Field(default=12, alias="AGENT_CONTEXT_KEEP_RECENT")
    context_tool_chars: int = Field(default=1_500, alias="AGENT_CONTEXT_TOOL_CHARS")

    # ---------- 验证 ----------
    verify_enabled: bool = Field(default=True, alias="AGENT_VERIFY_ENABLED")
    # 留空表示按项目清单自动识别（pytest / cargo / go / npm / make）
    verify_command: str = Field(default="", alias="AGENT_VERIFY_COMMAND")

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
    # ask（逐条询问）| approve（一律放行）| deny（一律拒绝）
    approval_mode: str = Field(default="ask", alias="AGENT_APPROVAL_MODE")
    llm_temperature: float = Field(default=0.0, alias="AGENT_LLM_TEMPERATURE")
    llm_timeout: int = Field(default=120, alias="AGENT_LLM_TIMEOUT")

    @property
    def context_budget(self) -> Any:
        """延迟导入：config 不该在导入期拉起 llm 层。"""
        from coding_agent.llm.context import ContextBudget

        return ContextBudget(
            max_chars=self.context_max_chars,
            keep_recent=self.context_keep_recent,
            tool_chars=self.context_tool_chars,
        )

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


def shadowed_env_keys(env_file: str = ".env") -> dict[str, str]:
    """找出「环境变量里有、但被 .env 覆盖掉」的键。

    优先级是 `.env` > 环境变量，所以这是**有意的**行为；但它同样静默 ——
    如果环境变量里那份才是你以为在生效的（比如 CI 注入的），
    结果就会与预期不符。doctor 把差异列出来，避免又一次「改了没生效」的排查。

    返回值：{键: ".env"（当前生效的来源）}。
    """
    from dotenv import dotenv_values

    try:
        file_values = {k: v for k, v in dotenv_values(env_file).items() if v}
    except OSError:
        return {}

    return {
        key: ".env"
        for key, value in file_values.items()
        if key in os.environ and os.environ[key] != value
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.apply_tracing_env()
    return settings
