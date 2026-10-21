"""依赖管理工具。

**包名必须严格校验。** 依赖安装在宿主里等同于执行任意代码（构建后端、
postinstall 脚本都会跑），所以这里有两道防线：

1. 包名走白名单正则：只允许 `名称[extras]版本约束`，出现 `-` 开头、
   空格、`;`、`|`、`&`、反引号等一律拒绝 —— 挡住参数注入与命令拼接。
2. 安装动作属于 L2，默认要用户确认后才执行。

安装器按工作区里的清单文件自动选择（uv / poetry / pnpm / yarn / npm / pip）。
"""

from __future__ import annotations

import re
import shlex
import time

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.sandbox.policy import CommandLevel
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import ShellArtifact, pack

DEPS_INSTALL = "deps_install"
DEPS_LIST = "deps_list"

# 名称[extras][版本约束]。刻意写得保守：宁可拒绝合法但罕见的写法，
# 也不放过任何可能被 shell 或包管理器重新解释的字符。
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_EXTRAS = r"(?:\[[A-Za-z0-9._,-]+\])?"
# 版本号必须以数字开头：否则 `requests>out.txt` 之类会被当成合法的版本比较
_SPEC = r"(?:(?:==|>=|<=|~=|!=|>|<)[0-9][A-Za-z0-9._*+!-]*)?"
_PACKAGE_RE = re.compile(rf"^{_NAME}{_EXTRAS}{_SPEC}$")

# (清单文件, 安装命令, 列依赖命令)
_MANAGERS: tuple[tuple[str, list[str], list[str]], ...] = (
    ("uv.lock", ["uv", "add"], ["uv", "pip", "list"]),
    ("poetry.lock", ["poetry", "add"], ["poetry", "show", "--top-level"]),
    ("pnpm-lock.yaml", ["pnpm", "add"], ["pnpm", "list", "--depth", "0"]),
    ("yarn.lock", ["yarn", "add"], ["yarn", "list", "--depth=0"]),
    ("package-lock.json", ["npm", "install"], ["npm", "ls", "--depth=0"]),
    ("package.json", ["npm", "install"], ["npm", "ls", "--depth=0"]),
    ("requirements.txt", ["pip", "install"], ["pip", "list"]),
    ("pyproject.toml", ["pip", "install"], ["pip", "list"]),
)
_DEFAULT_MANAGER = (["pip", "install"], ["pip", "list"])


class PackageSpecError(ValueError):
    """包名不合法。"""


def validate_package(spec: str) -> str:
    """校验单个包名，返回原值；不合法则抛异常。"""
    candidate = spec.strip()
    if not candidate:
        raise PackageSpecError("包名不能为空")
    if not _PACKAGE_RE.match(candidate):
        raise PackageSpecError(
            f"非法的包名：{spec!r}。只允许 名称[extras]版本约束，"
            f"且不能以 - 开头或包含空格与 shell 特殊字符。"
        )
    return candidate


def _manifest_probe(names: list[str]) -> str:
    quoted = " ".join(shlex.quote(name) for name in names)
    return f"for f in {quoted}; do [ -e \"$f\" ] && echo \"$f\"; done"


class DepsInstallInput(BaseModel):
    reason: str = Field(description="安装这些依赖的意图，一句话说明")
    packages: list[str] = Field(description="要安装的包，例如 [\"requests\", \"rich>=13\"]")


class DepsListInput(BaseModel):
    reason: str = Field(description="查看依赖列表的意图，一句话说明")


def detect_manager(sandbox: WslSandbox, workspace: str) -> tuple[list[str], list[str]]:
    """按工作区里的清单文件选出安装/列举命令。

    单独抽出来是为了能**不执行安装**地验证选择逻辑 ——
    否则测试会真的去 `pip install` / `npm install`，既慢又依赖网络。
    """
    probe = sandbox.run(_manifest_probe([m[0] for m in _MANAGERS]), cwd=workspace)
    present = {line.strip() for line in probe.stdout.splitlines() if line.strip()}
    for manifest, install, listing in _MANAGERS:
        if manifest in present:
            return install, listing
    return _DEFAULT_MANAGER


def resolve_manager(
    sandbox: WslSandbox, command: list[str]
) -> list[str] | None:
    """把探测到的命令落到**实际可用**的可执行文件。

    - `pip` 不存在但 `python3 -m pip` 可用时自动改写；
    - 明确依赖某个二进制（uv/npm/...）时先探测 `command -v`；
    - 都不可用返回 None，由调用方给出可操作的环境错误。
    """
    if not command:
        return None
    exe = command[0]
    if exe == "pip":
        if sandbox.run("command -v pip >/dev/null 2>&1").ok:
            return command
        if sandbox.run("python3 -m pip --version >/dev/null 2>&1").ok:
            return ["python3", "-m", "pip", *command[1:]]
        return None
    if exe in {"uv", "npm", "pnpm", "yarn", "poetry"}:
        probe = sandbox.run(
            f"command -v {shlex.quote(exe)} >/dev/null 2>&1"
        )
        return command if probe.ok else None
    return command


def build_deps_tools(settings: Settings, sandbox: WslSandbox) -> list[BaseTool]:
    workspace = resolve_workspace(settings, sandbox)
    detected: tuple[list[str], list[str]] | None = None
    detected_at = 0.0

    def _detect(*, force: bool = False) -> tuple[list[str], list[str]]:
        nonlocal detected, detected_at
        ttl = float(getattr(settings, "deps_detect_ttl_s", 30.0) or 0.0)
        now = time.monotonic()
        if force or detected is None or ttl <= 0 or now - detected_at > ttl:
            detected = detect_manager(sandbox, workspace)
            detected_at = now
        return detected

    def _invalidate() -> None:
        nonlocal detected
        detected = None

    def _report(command: list[str], reason: str, level: CommandLevel) -> str:
        planned = " ".join(shlex.quote(part) for part in command)
        resolved = resolve_manager(sandbox, command)
        if resolved is None:
            artifact = ShellArtifact(
                command=planned,
                ok=False,
                level=int(level),
                level_label=level.label,
            )
            return pack(
                f"[{level.label}] {reason}\n$ {planned}\n"
                f"未检测到可用的包管理器：{command[0]} 不可用。"
                "请先安装对应工具，或改用手动命令。",
                artifact,
            )
        command = resolved
        rendered = " ".join(shlex.quote(part) for part in command)
        result = sandbox.run(rendered, cwd=workspace)
        text = (
            f"[{level.label}] {reason}\n$ {rendered}\n"
            f"{result.render(settings.max_output_chars)}"
        )
        artifact = ShellArtifact(
            command=rendered,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            level=int(level),
            level_label=level.label,
        )
        return pack(text, artifact)

    def _install(reason: str, packages: list[str]) -> str:
        if not packages:
            return pack(
                "packages 不能为空。",
                ShellArtifact(ok=False, rejected=True, level=int(CommandLevel.MUTATE)),
            )
        try:
            validated = [validate_package(p) for p in packages]
        except PackageSpecError as exc:
            return pack(
                f"{exc}\n未执行任何安装。请给出合法、精确的包名。",
                ShellArtifact(
                    ok=False,
                    rejected=True,
                    decision="rejected",
                    level=int(CommandLevel.MUTATE),
                    level_label=CommandLevel.MUTATE.label,
                ),
            )

        install, _ = _detect(force=True)
        output = _report(
            [*install, *validated], reason, CommandLevel.MUTATE
        )
        # 安装可能生成/改写锁文件；下次重新探测。
        _invalidate()
        return output

    def _list(reason: str) -> str:
        _, listing = _detect()
        return _report(listing, reason, CommandLevel.READ)

    return [
        StructuredTool.from_function(
            func=_install,
            name=DEPS_INSTALL,
            description=(
                "为当前项目安装依赖，自动识别 uv / poetry / pnpm / yarn / npm / pip。"
                "安装会执行包的构建脚本，因此需要用户确认。"
            ),
            args_schema=DepsInstallInput,
        ),
        StructuredTool.from_function(
            func=_list,
            name=DEPS_LIST,
            description="列出当前项目已安装的依赖。只读。",
            args_schema=DepsListInput,
        ),
    ]
