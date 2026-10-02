"""路径守卫与 Windows ↔ WSL 路径转换。

当前版本做的是**词法级**防护：归一化后必须落在工作区根目录内。
符号链接逃逸需要文件系统信息，将在 W3 通过沙箱内 `realpath` 校验补齐。
"""

from __future__ import annotations

import posixpath
import re

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


class SandboxPathError(ValueError):
    """路径越界或无法安全解析。"""


def looks_like_windows_path(path: str) -> bool:
    """`D:\\proj`、`C:/tmp` 这类带盘符的路径。"""
    return bool(_WIN_DRIVE_RE.match(path))


def win_to_wsl(path: str) -> str:
    """`D:\\proj\\a` → `/mnt/d/proj/a`；已是 POSIX 路径则原样返回。"""
    if not path:
        raise SandboxPathError("路径为空")
    if path.startswith("/"):
        return path
    match = _WIN_DRIVE_RE.match(path)
    if not match:
        raise SandboxPathError(f"无法识别的 Windows 路径：{path}")
    drive, rest = match.groups()
    return f"/mnt/{drive.lower()}/{rest.replace(chr(92), '/')}"


def wsl_to_win(path: str) -> str:
    """`/mnt/d/proj/a` → `D:\\proj\\a`；非 /mnt 挂载则原样返回。"""
    match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path)
    if not match:
        return path
    drive, rest = match.groups()
    return f"{drive.upper()}:\\{rest.replace('/', chr(92))}"


def normalize_wsl(path: str, cwd: str = "/") -> str:
    """返回归一化的绝对 POSIX 路径。

    容忍 Windows 形式的输入（`D:\\proj` → `/mnt/d/proj`），因为配置和模型
    都可能给出盘符路径；相对路径按 cwd 展开。
    """
    if "\x00" in path:
        raise SandboxPathError("路径包含空字节")
    # 不做 ~ 展开：要么是模型笔误，要么是想逃出工作区，两者都应显式失败
    if path == "~" or path.startswith("~/"):
        raise SandboxPathError(f"不支持 ~ 展开，请给出工作区内的绝对路径：{path}")
    path = _to_posix(path)
    cwd = _to_posix(cwd)
    candidate = path if path.startswith("/") else posixpath.join(cwd, path)
    return posixpath.normpath(candidate)


def _to_posix(path: str) -> str:
    return win_to_wsl(path) if looks_like_windows_path(path) else path


def normalize_root(root: str) -> str:
    """归一化工作区根目录；必须是绝对路径。"""
    if not root.startswith("/") and not looks_like_windows_path(root):
        raise SandboxPathError(f"工作区必须是绝对路径：{root}")
    return normalize_wsl(root)


def is_within(path: str, root: str) -> bool:
    """纯词法判断，入参需已是 POSIX 绝对路径。"""
    normalized = posixpath.normpath(path)
    root_norm = posixpath.normpath(root).rstrip("/") or "/"
    if root_norm == "/":
        return normalized.startswith("/")
    return normalized == root_norm or normalized.startswith(root_norm + "/")


def ensure_inside(path: str, root: str, cwd: str = "/") -> str:
    """归一化并确认路径位于 root 之内，返回可直接使用的 POSIX 路径。

    两侧都做归一化 —— 否则工作区根写成 Windows 路径时，连它自己都会被判越界。
    """
    normalized = normalize_wsl(path, cwd)
    root_norm = normalize_root(root)
    if not is_within(normalized, root_norm):
        raise SandboxPathError(f"路径越出工作区：{normalized} 不在 {root_norm} 内")
    return normalized
