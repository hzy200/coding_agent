"""沙箱文件系统操作。

所有读写都在沙箱内执行（复用 WslSandbox），内容经 base64 传输，
保证字节精确、不受引号/换行/编码影响。

**双重路径校验**（这是「路径安全」的关键）：

1. 词法校验：`ensure_inside` 归一化后必须在工作区内，挡住 `..`、`~`、越界绝对路径。
2. 真实路径校验：在沙箱内 `realpath` 解析符号链接，确认解析结果仍在工作区内。

只做第 1 步是不够的 —— 攻击面是「模型用 shell 建一个指向 /etc 的符号链接，
再让文件工具去读写它」。第 2 步专门堵这个洞。
"""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass

from coding_agent.sandbox.pathguard import (
    SandboxPathError,
    ensure_inside,
    is_within,
)
from coding_agent.sandbox.wsl_exec import WslSandbox


class SandboxFsError(RuntimeError):
    """文件系统操作失败（沙箱不可用、路径非法等）。"""


@dataclass(slots=True)
class FileStat:
    exists: bool
    is_file: bool
    size: int
    real_path: str


def _quote(path: str) -> str:
    return shlex.quote(path)


def _parse_kv(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key.strip()] = value
    return result


class SandboxFs:
    """工作区内的文件读写。"""

    def __init__(self, sandbox: WslSandbox, root: str) -> None:
        self._sandbox = sandbox
        self.root = root
        self._real_root: str | None = None

    # ------------------------------------------------------------------
    # 路径校验
    # ------------------------------------------------------------------

    @property
    def real_root(self) -> str:
        """工作区的规范路径（工作区自身也可能是符号链接）。"""
        if self._real_root is None:
            result = self._sandbox.run(f"realpath -m -- {_quote(self.root)}")
            if not result.ok or not result.stdout.strip():
                raise SandboxFsError(f"无法解析工作区路径：{result.render(500)}")
            self._real_root = result.stdout.strip()
        return self._real_root

    def stat(self, path: str) -> FileStat:
        """查询文件状态；不存在的路径返回其「若创建会是」的规范路径。"""
        script = f"""\
p={_quote(path)}
if [ -e "$p" ]; then
  echo "EXISTS=1"
  if [ -f "$p" ]; then echo "FILE=1"; else echo "FILE=0"; fi
  echo "SIZE=$(stat -c %s -- "$p" 2>/dev/null || echo 0)"
  echo "REAL=$(realpath -m -- "$p")"
else
  echo "EXISTS=0"
  echo "FILE=0"
  echo "SIZE=0"
  echo "REAL=$(realpath -m -- "$(dirname -- "$p")")/$(basename -- "$p")"
fi
"""
        result = self._sandbox.run(script)
        if not result.ok:
            raise SandboxFsError(f"无法读取文件状态：{result.render(500)}")
        data = _parse_kv(result.stdout)
        return FileStat(
            exists=data.get("EXISTS") == "1",
            is_file=data.get("FILE") == "1",
            size=int(data.get("SIZE") or 0),
            real_path=data.get("REAL", ""),
        )

    def resolve(self, path: str) -> str:
        """返回通过双重校验的可用路径。

        相对路径按工作区根展开 —— 模型习惯给 `src/a.py` 这样的相对路径。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        info = self.stat(lexical)
        if not info.real_path:
            raise SandboxFsError(f"无法解析真实路径：{lexical}")
        if not is_within(info.real_path, self.real_root):
            raise SandboxPathError(
                f"路径经符号链接逃出工作区：{lexical} → {info.real_path}（工作区 {self.real_root}）"
            )
        return lexical

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        target = self.resolve(path)
        info = self.stat(target)
        if not info.exists:
            raise SandboxFsError(f"文件不存在：{target}")
        if not info.is_file:
            raise SandboxFsError(f"不是普通文件：{target}")
        if info.size > max_bytes:
            raise SandboxFsError(
                f"文件过大：{info.size} 字节（上限 {max_bytes}）。请用 offset/limit 分段读取。"
            )
        result = self._sandbox.run(f"base64 -w0 -- {_quote(target)}")
        if not result.ok:
            raise SandboxFsError(f"读取失败：{result.render(500)}")
        try:
            return base64.b64decode(result.stdout.strip(), validate=True)
        except ValueError as exc:
            raise SandboxFsError(f"读取结果解码失败：{exc}") from exc

    def read_text(self, path: str, *, max_bytes: int) -> str:
        """读取文本；非 UTF-8 或含 NUL 视为二进制并拒绝。"""
        data = self.read_bytes(path, max_bytes=max_bytes)
        if b"\x00" in data:
            raise SandboxFsError(f"二进制文件（含 NUL 字节），不支持读取：{path}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SandboxFsError(f"非 UTF-8 文本，不支持读取：{path}（{exc.reason}）") from exc

    def write_text(self, path: str, content: str) -> int:
        """写入文本（覆盖或新建），自动创建父目录。返回写入字节数。"""
        target = self.resolve(path)
        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        script = f"""\
mkdir -p -- "$(dirname -- {_quote(target)})" && base64 -d > {_quote(target)} <<'__AGENT_PAYLOAD__'
{_wrap(payload)}
__AGENT_PAYLOAD__
"""
        result = self._sandbox.run(script)
        if not result.ok:
            raise SandboxFsError(f"写入失败：{result.render(500)}")
        return len(content.encode("utf-8"))


def _wrap(payload: str, width: int = 76) -> str:
    """base64 折行，避免脚本里出现超长单行。"""
    return "\n".join(payload[i : i + width] for i in range(0, len(payload), width))
