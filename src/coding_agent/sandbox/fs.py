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

    def _stat_script(self, path: str, *, include_data: bool = False) -> str:
        """一次进程启动里同时拿到 realpath / 类型 / 大小（可选：内容）。

        每次 `wsl.exe` 调用都是一次进程启动（实测 0.2–0.3s），
        所以"顺手多查一点"比"多跑一趟"划算得多 —— 早先的实现读一个文件要启动三次。
        """
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
        if include_data:
            # 文件不存在或不是普通文件时输出空串，由调用方按状态判断
            script += 'if [ -f "$p" ]; then echo "DATA=$(base64 -w0 -- "$p")"; fi\n'
        return script

    @staticmethod
    def _parse_stat(stdout: str) -> tuple[FileStat, bytes | None]:
        data = _parse_kv(stdout)
        info = FileStat(
            exists=data.get("EXISTS") == "1",
            is_file=data.get("FILE") == "1",
            size=int(data.get("SIZE") or 0),
            real_path=data.get("REAL", ""),
        )
        raw = data.get("DATA")
        if not raw:
            return info, None
        try:
            return info, base64.b64decode(raw, validate=True)
        except ValueError as exc:
            raise SandboxFsError(f"读取结果解码失败：{exc}") from exc

    def stat(self, path: str) -> FileStat:
        """查询文件状态；不存在的路径返回其「若创建会是」的规范路径。"""
        result = self._sandbox.run(self._stat_script(path))
        if not result.ok:
            raise SandboxFsError(f"无法读取文件状态：{result.render(500)}")
        return self._parse_stat(result.stdout)[0]

    def _check_real_path(self, lexical: str, info: FileStat) -> None:
        """把 realpath 校验单独抽出来，好让读取路径复用它而不多跑一趟。"""
        if not info.real_path:
            raise SandboxFsError(f"无法解析真实路径：{lexical}")
        if not is_within(info.real_path, self.real_root):
            raise SandboxPathError(
                f"路径经符号链接逃出工作区：{lexical} → {info.real_path}"
                f"（工作区 {self.real_root}）"
            )

    def probe(self, path: str) -> tuple[str, FileStat]:
        """一次调用里完成路径校验 + 状态查询。

        调用方几乎总是既要路径又要状态（"存在吗、是文件吗、多大"），
        拆成 `resolve()` + `stat()` 就是两次 `wsl.exe` 进程启动。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        info = self.stat(lexical)
        self._check_real_path(lexical, info)
        return lexical, info

    def resolve(self, path: str) -> str:
        """返回通过双重校验的可用路径。

        相对路径按工作区根展开 —— 模型习惯给 `src/a.py` 这样的相对路径。
        """
        return self.probe(path)[0]

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        """读文件内容：**一次**进程启动里完成路径校验 + 状态 + 读取。"""
        lexical = ensure_inside(path, self.root, cwd=self.root)
        result = self._sandbox.run(self._stat_script(lexical, include_data=True))
        if not result.ok:
            raise SandboxFsError(f"读取失败：{result.render(500)}")

        info, data = self._parse_stat(result.stdout)
        self._check_real_path(lexical, info)
        if not info.exists:
            raise SandboxFsError(f"文件不存在：{lexical}")
        if not info.is_file:
            raise SandboxFsError(f"不是普通文件：{lexical}")
        if info.size > max_bytes:
            raise SandboxFsError(
                f"文件过大：{info.size} 字节（上限 {max_bytes}）。请用 offset/limit 分段读取。"
            )
        if data is None:
            raise SandboxFsError(f"读取失败：没有取到内容（{lexical}）")
        return data

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
        """写入文本（覆盖或新建），自动创建父目录。返回写入字节数。

        校验与写入**合在一次进程启动里**：脚本自己先解析 realpath、
        确认仍在工作区内，再落盘。校验发生在写入之前，所以越界时一个字节都不会写。
        词法校验留在 Python 侧（不需要 I/O），realpath 校验由脚本执行。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        script = f"""\
p={_quote(lexical)}
root={_quote(self.real_root)}
dir=$(dirname -- "$p")
real_dir=$(realpath -m -- "$dir")
case "$real_dir" in
  "$root"|"$root"/*) ;;
  *) echo "ESCAPED=$real_dir" >&2; exit 9 ;;
esac
if [ -e "$p" ]; then
  real_p=$(realpath -m -- "$p")
  case "$real_p" in
    "$root"|"$root"/*) ;;
    *) echo "ESCAPED=$real_p" >&2; exit 9 ;;
  esac
fi
mkdir -p -- "$real_dir" && base64 -d > "$p" <<'__AGENT_PAYLOAD__'
{_wrap(payload)}
__AGENT_PAYLOAD__
"""
        result = self._sandbox.run(script)
        if not result.ok:
            if "ESCAPED=" in (result.stderr or ""):
                raise SandboxPathError(
                    f"路径经符号链接逃出工作区，已拒绝写入：{lexical}"
                    f"（{result.stderr.strip()}）"
                )
            raise SandboxFsError(f"写入失败：{result.render(500)}")
        return len(content.encode("utf-8"))


def _wrap(payload: str, width: int = 76) -> str:
    """base64 折行，避免脚本里出现超长单行。"""
    return "\n".join(payload[i : i + width] for i in range(0, len(payload), width))
