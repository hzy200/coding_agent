from __future__ import annotations

import json

from coding_agent.tools.artifacts import (
    CallArtifact,
    FileArtifact,
    ShellArtifact,
    pack,
    parse_artifact,
    unpack,
)


def test_pack_is_json_with_both_keys() -> None:
    raw = pack("hello", ShellArtifact(command="ls", ok=True, exit_code=0))
    data = json.loads(raw)
    assert set(data) == {"text", "artifact"}
    assert data["text"] == "hello"
    assert data["artifact"]["exit_code"] == 0


def test_pack_keeps_chinese_readable_and_accepts_plain_dict() -> None:
    raw = pack("命令被拒绝", {"ok": False})
    assert "命令被拒绝" in raw  # ensure_ascii=False，日志里可读
    assert unpack(raw) == ("命令被拒绝", {"ok": False})


def test_roundtrip() -> None:
    artifact = ShellArtifact(
        command="echo hi", ok=True, exit_code=0, level=0, level_label="L0 只读"
    )
    text, parsed = unpack(pack("管道\n输出", artifact))
    assert text == "管道\n输出"
    assert parsed == artifact.model_dump()


def test_plain_string_passes_through() -> None:
    """不带 artifact 的工具原样返回。"""
    assert unpack("just text") == ("just text", None)


def test_other_json_is_not_mistaken_for_envelope() -> None:
    assert unpack('{"a": 1}') == ('{"a": 1}', None)
    assert unpack('{"text": "x"}') == ('{"text": "x"}', None)
    assert unpack('{"text": "x", "artifact": 1, "extra": 2}') == (
        '{"text": "x", "artifact": 1, "extra": 2}',
        None,
    )
    assert unpack("[1, 2]") == ("[1, 2]", None)


def test_non_dict_artifact_is_discarded() -> None:
    raw = json.dumps({"text": "t", "artifact": "not-a-dict"})
    assert unpack(raw) == ("t", None)


def test_non_string_input() -> None:
    assert unpack(None) == ("None", None)


# ---------------- kind 判别 ----------------

def test_parse_artifact_dispatches_on_kind() -> None:
    shell = parse_artifact(ShellArtifact(command="ls", ok=True).model_dump())
    assert isinstance(shell, ShellArtifact)
    assert shell.command == "ls"

    file = parse_artifact(FileArtifact(path="a.py", action="edit", added=2).model_dump())
    assert isinstance(file, FileArtifact)
    assert file.added == 2

    call = parse_artifact(
        CallArtifact(tool="git_commit", ok=False, rejected=True, level=2).model_dump()
    )
    assert isinstance(call, CallArtifact)
    assert call.tool == "git_commit"
    assert call.rejected is True


def test_parse_artifact_rejects_unknown_and_malformed() -> None:
    assert parse_artifact(None) is None
    assert parse_artifact("nope") is None
    assert parse_artifact({}) is None
    assert parse_artifact({"kind": "unknown"}) is None
    # 字段类型不合法时要兜住，不能让事件层崩掉
    assert parse_artifact({"kind": "file", "added": "not-an-int"}) is None


def test_file_artifact_defaults_are_safe() -> None:
    artifact = FileArtifact()
    assert artifact.ok is False
    assert artifact.rejected is False
    assert artifact.snapshot_id is None
    assert artifact.diff == ""
