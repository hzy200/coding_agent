"""wsl.exe 的输出编码随调用环境在 UTF-16LE / UTF-8 之间摆动，
解码错了会导致发行版匹配不上、WSL 能力被判定为不可用。"""

from __future__ import annotations

from coding_agent.sandbox.wsl_exec import parse_distro_list

EXPECTED = ["Ubuntu", "docker-desktop", "kali-linux"]


def test_decodes_utf8_output() -> None:
    raw = b"Ubuntu\r\ndocker-desktop\r\nkali-linux\r\n"
    assert parse_distro_list(raw) == EXPECTED


def test_decodes_utf16le_output() -> None:
    raw = "Ubuntu\r\ndocker-desktop\r\nkali-linux\r\n".encode("utf-16-le")
    assert parse_distro_list(raw) == EXPECTED


def test_strips_bom_and_trailing_nulls() -> None:
    raw = "\ufeffUbuntu\r\n\x00\r\n".encode("utf-16-le")
    assert parse_distro_list(raw) == ["Ubuntu"]


def test_empty_output() -> None:
    assert parse_distro_list(b"") == []
    assert parse_distro_list(b"\x00\x00") == []
