"""CLI 子命令。

命令体此前完全没有覆盖。这里用 CliRunner 真正调一遍，
把输出换成记录型 Console 来断言，不依赖真实终端。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from coding_agent.audit import AuditLogger, AuditRecord, read_records
from coding_agent.cli import app as cli_app
from coding_agent.config import Settings
from coding_agent.events import ApprovalRequested
from coding_agent.steering import SteeringController

runner = CliRunner()


@pytest.fixture(autouse=True)
def output(monkeypatch) -> Console:
    console = Console(record=True, width=200, force_terminal=False, no_color=True)
    monkeypatch.setattr(cli_app, "console", console)
    return console


def _text() -> str:
    # clear=False：默认会清空记录缓冲，同一次输出读两遍就变成空串
    return cli_app.console.export_text(clear=False)


def _scoped(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        **overrides,
    )


def _use_settings(monkeypatch, settings: Settings) -> None:
    monkeypatch.setattr(cli_app, "get_settings", lambda: settings)


def _write_records(settings: Settings, count: int = 3) -> AuditLogger:
    logger = AuditLogger(settings.resolved_audit_dir)
    logger.write(AuditRecord(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        kind="run_start", thread_id="abc123", detail="看看目录",
    ))
    for i in range(count):
        logger.write(AuditRecord(
            ts=datetime.now(UTC).isoformat(timespec="seconds"),
            kind="tool_call", thread_id="abc123", tool="shell_exec",
            level="L0 只读", decision="auto", ok=True, duration_ms=10 + i,
        ))
    logger.write(AuditRecord(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        kind="tool_call", thread_id="abc123", tool="shell_exec",
        level="L3 危险", decision="rejected", ok=False,
    ))
    return logger


# ---------------- 帮助 ----------------

@pytest.mark.parametrize(
    "command",
    ["doctor", "sandbox-init", "chat", "run", "snapshots", "undo", "diff",
     "sessions", "checkpoints", "fork", "rewind", "memory", "audit", "tui"],
)
def test_every_command_has_help(command: str) -> None:
    result = runner.invoke(cli_app.app, [command, "--help"])
    assert result.exit_code == 0
    assert command in result.output or "Usage" in result.output


def test_root_help_lists_commands() -> None:
    result = runner.invoke(cli_app.app, ["--help"])
    assert result.exit_code == 0
    for command in ("doctor", "run", "tui", "undo", "sessions"):
        assert command in result.output


def test_no_args_shows_help() -> None:
    assert runner.invoke(cli_app.app, []).exit_code != 0


# ---------------- audit ----------------

def test_audit_lists_records(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(cli_app.app, ["audit", "--limit", "2"])

    assert result.exit_code == 0
    text = _text()
    assert "最近 2 条" in text
    assert "tool_call" in text


def test_audit_marks_rejected_rows(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    _write_records(settings)
    _use_settings(monkeypatch, settings)

    runner.invoke(cli_app.app, ["audit", "--limit", "50"])
    assert "已拒绝" in _text()


def test_audit_with_no_records(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path))
    result = runner.invoke(cli_app.app, ["audit"])

    assert result.exit_code == 0
    assert "没有审计记录" in _text()


def test_audit_filters_by_thread(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    logger = _write_records(settings)
    logger.write(AuditRecord(ts="2026-10-03T10:00:00+00:00", kind="run_start",
                             thread_id="other", detail="别的会话"))
    _use_settings(monkeypatch, settings)

    runner.invoke(cli_app.app, ["audit", "--thread-id", "other"])
    assert "别的会话" not in _text()  # run_start 的 detail 不在表格里，但计数要对
    assert "最近 1 条" in _text()


def test_audit_verify_cli(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(cli_app.app, ["audit", "--verify"])

    assert result.exit_code == 0
    assert "哈希链" in _text()


def test_audit_reads_an_explicit_path(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    logger = _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(cli_app.app, ["audit", "--path", str(logger.path)])
    assert result.exit_code == 0
    assert "tool_call" in _text()


# ---------------- sessions ----------------

def test_sessions_lists_history(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(cli_app.app, ["sessions"])

    assert result.exit_code == 0
    text = _text()
    assert "abc123" in text
    assert "看看目录" in text
    assert "thread-id" in text  # 提示如何接着聊


def test_sessions_when_empty(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path))
    result = runner.invoke(cli_app.app, ["sessions"])

    assert result.exit_code == 0
    assert "没有历史会话记录" in _text()


def test_sessions_respects_limit(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    logger = AuditLogger(settings.resolved_audit_dir)
    for i in range(5):
        logger.write(AuditRecord(
            ts=f"2026-10-0{i + 1}T10:00:00+00:00", kind="run_start",
            thread_id=f"t{i}", detail=f"会话 {i}",
        ))
    _use_settings(monkeypatch, settings)

    runner.invoke(cli_app.app, ["sessions", "--limit", "2"])
    assert "t4" in _text()
    assert "t0" not in _text()


# ---------------- doctor ----------------

@pytest.mark.wsl_env
def test_doctor_reports_environment(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path, langsmith_tracing=False))
    result = runner.invoke(cli_app.app, ["doctor"])

    text = _text()
    assert "环境自检" in text
    assert "Python 版本" in text
    assert "依赖 langgraph" in text
    assert "跨进程文件锁" in text
    assert "WSL 发行版" in text
    # 退出码取决于真实环境（WSL/工作区是否就绪），只要求是 0 或 1
    assert result.exit_code in (0, 1)


@pytest.mark.wsl_env
def test_doctor_reports_missing_api_key(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path, deepseek_api_key=""))
    runner.invoke(cli_app.app, ["doctor"])
    assert "DEEPSEEK_API_KEY" in _text()


@pytest.mark.wsl_env
def test_doctor_warns_on_shadowed_config(tmp_path, monkeypatch) -> None:
    """环境变量被 .env 覆盖这件事必须显式说出来。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LANGSMITH_PROJECT=from_dotenv\n", encoding="utf-8")
    monkeypatch.setenv("LANGSMITH_PROJECT", "from_environment")
    monkeypatch.chdir(tmp_path)
    _use_settings(monkeypatch, _scoped(tmp_path, langsmith_tracing=False))

    runner.invoke(cli_app.app, ["doctor"])

    text = _text()
    assert "WARN" in text
    assert "LANGSMITH_PROJECT" in text


# ---------------- run 的参数校验 ----------------

def test_run_requires_a_prompt() -> None:
    assert runner.invoke(cli_app.app, ["run"]).exit_code != 0


def test_run_rejects_unknown_option() -> None:
    assert runner.invoke(cli_app.app, ["run", "--nope", "hi"]).exit_code != 0


def test_tui_reports_missing_ui_extra(monkeypatch) -> None:
    """没装 ui 依赖时要给明确的安装提示，而不是 ImportError 栈。"""

    def boom(name: str, *args, **kwargs):
        raise ImportError("No module named 'textual'")

    monkeypatch.setattr(cli_app, "get_settings", lambda: Settings(_env_file=None))
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "coding_agent.tui":
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = runner.invoke(cli_app.app, ["tui"])

    assert result.exit_code == 2
    assert "ui" in _text()


# ---------------- steering ----------------

def test_steer_command_writes_the_persistent_queue(monkeypatch, tmp_path) -> None:
    settings = _scoped(
        tmp_path, steering_dir=str(tmp_path / "steering")
    )
    _use_settings(monkeypatch, settings)

    result = runner.invoke(
        cli_app.app,
        [
            "steer",
            "--thread-id",
            "t1",
            "add",
            "--text",
            "检查边界",
            "--role",
            "review",
        ],
    )

    assert result.exit_code == 0
    controller = SteeringController(settings.resolved_steering_dir)
    commands = controller.peek("t1")
    assert [item.action for item in commands] == ["add"]
    assert commands[0].text == "检查边界"
    assert commands[0].role == "review"

    # CLI 通道也必须留审计：提交时落账，source 标明来源。
    records = read_records(AuditLogger(settings.resolved_audit_dir).path)
    assert records[-1].kind == "steering"
    assert records[-1].source == "cli"
    assert records[-1].thread_id == "t1"
    assert "add: 检查边界" in records[-1].detail


def test_steer_command_honours_an_explicit_queue_dir(monkeypatch, tmp_path) -> None:
    settings = _scoped(
        tmp_path, steering_dir=str(tmp_path / "default-steering")
    )
    _use_settings(monkeypatch, settings)
    explicit = tmp_path / "shared-steering"

    result = runner.invoke(
        cli_app.app,
        [
            "steer",
            "--thread-id",
            "t1",
            "pause",
            "--steering-dir",
            str(explicit),
        ],
    )

    assert result.exit_code == 0
    assert [item.action for item in SteeringController(explicit).peek("t1")] == [
        "pause"
    ]
    # 默认目录不应被写入
    assert SteeringController(settings.resolved_steering_dir).peek("t1") == []


# ---------------- Web 公网守卫 ----------------

def test_loopback_bind_detection() -> None:
    assert cli_app._is_loopback_bind("127.0.0.1")
    assert cli_app._is_loopback_bind("::1")
    assert cli_app._is_loopback_bind("localhost")
    assert not cli_app._is_loopback_bind("0.0.0.0")
    assert not cli_app._is_loopback_bind("203.0.113.10")


def test_remote_web_without_tokens_is_refused(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path, web_auth_tokens=""))

    result = runner.invoke(
        cli_app.app, ["web", "--host", "0.0.0.0", "--allow-remote"]
    )

    assert result.exit_code == 2
    assert "AUTH_TOKENS" in _text()


def test_behind_proxy_without_tokens_is_refused(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path, web_auth_tokens=""))

    result = runner.invoke(cli_app.app, ["web", "--behind-proxy"])

    assert result.exit_code == 2
    assert "behind-proxy" in _text()


# ---------------- 审批交互 ----------------

def test_yes_does_not_auto_approve_danger(monkeypatch) -> None:
    """--yes 只跳过 L2：L3 危险命令仍要人工回答。"""
    monkeypatch.setattr(
        type(cli_app.console), "input", lambda self, *args, **kwargs: "n"
    )
    requests = [
        ApprovalRequested(request_id="l2", level="L2 变更性", command="pip install x"),
        ApprovalRequested(request_id="l3", level="L3 危险", command="rm -rf /"),
    ]

    decisions = cli_app._ask_approval(requests, assume_yes=True)

    assert decisions == {"l2": True, "l3": False}


# ---------------- main 入口 ----------------

def test_main_reports_missing_api_key(monkeypatch) -> None:
    """缺 Key 时给一句人话，而不是抛栈。"""
    from coding_agent.llm.deepseek import MissingApiKeyError

    def boom() -> None:
        raise MissingApiKeyError("未配置 DEEPSEEK_API_KEY。")

    monkeypatch.setattr(cli_app, "app", boom)
    with pytest.raises(typer.Exit) as excinfo:
        cli_app.main()

    assert excinfo.value.exit_code == 2
    assert "DEEPSEEK_API_KEY" in _text()
