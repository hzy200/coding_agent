from __future__ import annotations

import pytest

from coding_agent.sandbox.pathguard import (
    SandboxPathError,
    ensure_inside,
    is_within,
    normalize_root,
    normalize_wsl,
    win_to_wsl,
    wsl_to_win,
)

ROOT = "/home/agent/ws"


def test_win_to_wsl() -> None:
    assert win_to_wsl("D:\\proj\\src\\a.py") == "/mnt/d/proj/src/a.py"
    assert win_to_wsl("C:/tmp") == "/mnt/c/tmp"
    assert win_to_wsl("/already/posix") == "/already/posix"


def test_win_to_wsl_rejects_relative() -> None:
    with pytest.raises(SandboxPathError):
        win_to_wsl("proj\\src\\a.py")


def test_wsl_to_win_roundtrip() -> None:
    assert wsl_to_win("/mnt/d/proj/a.py") == "D:\\proj\\a.py"
    assert wsl_to_win("/home/agent/ws") == "/home/agent/ws"


def test_normalize_relative_uses_cwd() -> None:
    assert normalize_wsl("src/a.py", ROOT) == f"{ROOT}/src/a.py"
    assert normalize_wsl("./src/../src/a.py", ROOT) == f"{ROOT}/src/a.py"


def test_is_within_boundaries() -> None:
    assert is_within(f"{ROOT}/src/a.py", ROOT)
    assert is_within(ROOT, ROOT)
    # 前缀相同但并非子目录，必须判为越界
    assert not is_within(f"{ROOT}xyz/a.py", ROOT)
    assert not is_within("/home/agent/other", ROOT)


def test_ensure_inside_accepts_legit_path() -> None:
    assert ensure_inside("src/a.py", ROOT, cwd=ROOT) == f"{ROOT}/src/a.py"


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "/home/agent/ws/../other",
        "~/secrets",
    ],
)
def test_ensure_inside_rejects_escape(path: str) -> None:
    with pytest.raises(SandboxPathError):
        ensure_inside(path, ROOT, cwd=ROOT)


def test_null_byte_rejected() -> None:
    with pytest.raises(SandboxPathError):
        ensure_inside("a\x00b", ROOT, cwd=ROOT)


# 配置层很容易把工作区写成 Windows 路径（AGENT_WSL_WORKSPACE=D:\proj）。
# 若不归一化，工作区根自己都会被判成越界，所有命令全被拒。
def test_windows_style_root_is_normalized() -> None:
    root = "D:\\proj\\ws"
    assert normalize_root(root) == "/mnt/d/proj/ws"
    assert ensure_inside("/mnt/d/proj/ws/src/a.py", root) == "/mnt/d/proj/ws/src/a.py"
    assert ensure_inside("src/a.py", root, cwd=root) == "/mnt/d/proj/ws/src/a.py"


def test_windows_style_child_path_is_normalized() -> None:
    assert ensure_inside("D:\\proj\\ws\\a.py", "/mnt/d/proj/ws") == "/mnt/d/proj/ws/a.py"


def test_windows_root_still_blocks_escape() -> None:
    with pytest.raises(SandboxPathError):
        ensure_inside("C:\\Windows\\system32", "D:\\proj\\ws")


def test_normalize_root_rejects_relative() -> None:
    with pytest.raises(SandboxPathError):
        normalize_root("proj/ws")
