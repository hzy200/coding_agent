"""配置来源与优先级。

优先级是 **`.env` > 环境变量**（刻意偏离通用约定）。原因是本机踩到的实际故障：
harness 把陈旧的值注入进程环境，用户既看不到来源也删不掉，改 `.env` 完全不生效 ——
表现为「明明更新了 API key 却一直 403」。

这里既测优先级本身，也测「差异要被报出来」—— 覆盖无论如何都是静默的。
"""

from __future__ import annotations

from coding_agent.config import Settings, shadowed_env_keys


def _write_env(tmp_path, content: str) -> str:
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return str(path)


# ---------------- 优先级 ----------------

def test_dotenv_wins_over_environment(tmp_path, monkeypatch) -> None:
    env_file = _write_env(tmp_path, "LANGSMITH_PROJECT=from_dotenv\n")
    monkeypatch.setenv("LANGSMITH_PROJECT", "from_environment")

    assert Settings(_env_file=env_file).langsmith_project == "from_dotenv"


def test_constructor_argument_wins_over_everything(tmp_path, monkeypatch) -> None:
    env_file = _write_env(tmp_path, "LANGSMITH_PROJECT=from_dotenv\n")
    monkeypatch.setenv("LANGSMITH_PROJECT", "from_environment")

    settings = Settings(_env_file=env_file, langsmith_project="from_ctor")
    assert settings.langsmith_project == "from_ctor"


def test_environment_used_when_not_in_dotenv(tmp_path, monkeypatch) -> None:
    env_file = _write_env(tmp_path, "OTHER=1\n")
    monkeypatch.setenv("LANGSMITH_PROJECT", "from_environment")
    assert Settings(_env_file=env_file).langsmith_project == "from_environment"


def test_blank_placeholder_does_not_override_environment(tmp_path, monkeypatch) -> None:
    """从 .env.example 复制来的 `KEY=` 空占位不该盖掉可用的环境变量。"""
    env_file = _write_env(tmp_path, "LANGSMITH_API_KEY=\n")
    monkeypatch.setenv("LANGSMITH_API_KEY", "real_key_from_environment")

    assert Settings(_env_file=env_file).langsmith_api_key == "real_key_from_environment"


def test_explicit_env_file_bypass_is_still_respected(tmp_path, monkeypatch) -> None:
    """`_env_file=None` 必须继续生效（测试与嵌入式用法依赖它），
    不能被自定义 source 无视掉。"""
    monkeypatch.setenv("LANGSMITH_PROJECT", "from_environment")
    assert Settings(_env_file=None).langsmith_project == "from_environment"
    assert Settings(_env_file=str(tmp_path / "missing.env")).langsmith_project == "from_environment"


# ---------------- 差异上报 ----------------

def test_reports_key_covered_by_dotenv(tmp_path, monkeypatch) -> None:
    env_file = _write_env(tmp_path, "LANGSMITH_API_KEY=from_dotenv\n")
    monkeypatch.setenv("LANGSMITH_API_KEY", "from_environment")

    assert shadowed_env_keys(env_file) == {"LANGSMITH_API_KEY": ".env"}


def test_same_value_is_not_a_conflict(tmp_path, monkeypatch) -> None:
    env_file = _write_env(tmp_path, "LANGSMITH_API_KEY=same\n")
    monkeypatch.setenv("LANGSMITH_API_KEY", "same")
    assert shadowed_env_keys(env_file) == {}


def test_key_only_in_env_file_is_fine(tmp_path, monkeypatch) -> None:
    env_file = _write_env(tmp_path, "LANGSMITH_API_KEY=only_here\n")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    assert shadowed_env_keys(env_file) == {}


def test_key_only_in_environment_is_fine(tmp_path, monkeypatch) -> None:
    env_file = _write_env(tmp_path, "OTHER=1\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "abc")
    assert shadowed_env_keys(env_file) == {}


def test_missing_env_file_is_not_an_error(tmp_path) -> None:
    assert shadowed_env_keys(str(tmp_path / "nope.env")) == {}


def test_blank_values_in_env_file_are_ignored(tmp_path, monkeypatch) -> None:
    """.env.example 那种留空占位不该被当成「配置了」。"""
    env_file = _write_env(tmp_path, "LANGSMITH_API_KEY=\n")
    monkeypatch.setenv("LANGSMITH_API_KEY", "from_environment")
    assert shadowed_env_keys(env_file) == {}


def test_comments_and_quotes_are_handled(tmp_path, monkeypatch) -> None:
    env_file = _write_env(
        tmp_path,
        "# 注释\nLANGSMITH_API_KEY=\"quoted_value\"\n\nDEEPSEEK_MODEL=deepseek-chat\n",
    )
    monkeypatch.setenv("LANGSMITH_API_KEY", "unquoted")
    monkeypatch.setenv("DEEPSEEK_MODEL", "other-model")

    assert set(shadowed_env_keys(env_file)) == {"LANGSMITH_API_KEY", "DEEPSEEK_MODEL"}


def test_reports_every_conflicting_key(tmp_path, monkeypatch) -> None:
    env_file = _write_env(tmp_path, "A=1\nB=2\nC=3\n")
    monkeypatch.setenv("A", "x")
    monkeypatch.setenv("B", "2")  # 一致
    monkeypatch.setenv("C", "y")

    assert set(shadowed_env_keys(env_file)) == {"A", "C"}
