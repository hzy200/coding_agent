"""仓库检索工具。

为什么不让模型继续用 `grep -rn … | head -50`：

- **结果是结构化的**：逐条 `文件:行号: 内容` 解析成记录，能报「命中 N 条，
  显示前 M 条」，而不是让模型自己数 head 截到哪。
- **不会有引号事故**：模式、glob、路径都是逐参数传入，模型不需要自己拼引号。
- **没有管道就没有误伤**：`find … | xargs grep …` 会被分级判成 L2 而拦下，
  换成本工具就不必给 `xargs` 开白名单。

`rg` 不存在时退到 `grep` / `find` —— 值钱的是这个结构化接口，不是具体哪个二进制。
两个后端输出同一套「文件:行号:内容」格式，上层解析逻辑不分叉。
"""

from __future__ import annotations

import shlex

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.sandbox.pathguard import SandboxPathError, ensure_inside
from coding_agent.sandbox.policy import CommandLevel
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.security.read_policy import is_protected_read_path
from coding_agent.security.secrets import redact_secrets
from coding_agent.tools.artifacts import ShellArtifact, pack

SEARCH_CODE = "search_code"
FIND_FILES = "find_files"

DEFAULT_MAX_RESULTS = 50
MAX_RESULTS_CAP = 500

# grep 不会读 .gitignore，这些目录必须显式排除，否则在带 .venv 的仓库里会扫到天荒地老
EXCLUDED_DIRS = (".git", ".venv", "venv", "node_modules", ".agent", "__pycache__", "dist", "build")
# 秘密文件即使被搜索命中也不回显；与 read_policy 的默认规则保持一致。
# 这里按**文件名**排除（rg/grep 的 exclude 参数语义不同），路径型规则
# （`.aws/**`、`.kube/**`）由输出侧的 `is_protected_read_path` 兜底。
SECRET_EXCLUDES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*",
    ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "credentials", "credentials.json",
    "*service-account*.json", "*service_account*.json",
    "*.tfstate", "*.tfstate.backup", "*.jks", "*.keystore", "*.gpg",
)
SECRET_DIRS = (".aws", ".kube")

_NO_MATCH_EXIT = 1


class SearchCodeInput(BaseModel):
    pattern: str = Field(description="要搜索的内容，默认按正则解释")
    reason: str = Field(description="搜索的意图，一句话说明")
    glob: str | None = Field(default=None, description="限定文件，例如 *.py 或 *test*")
    path: str | None = Field(default=None, description="限定子目录，省略则搜整个工作区")
    max_results: int = Field(
        default=DEFAULT_MAX_RESULTS, ge=1, le=MAX_RESULTS_CAP, description="最多返回多少条"
    )
    fixed: bool = Field(default=False, description="把 pattern 当作字面量而不是正则")
    ignore_case: bool = Field(default=False, description="忽略大小写")


class FindFilesInput(BaseModel):
    pattern: str = Field(description="文件名模式，例如 *.py 或 pyproject.*")
    reason: str = Field(description="查找的意图，一句话说明")
    path: str | None = Field(default=None, description="限定子目录，省略则查整个工作区")
    max_results: int = Field(
        default=DEFAULT_MAX_RESULTS, ge=1, le=MAX_RESULTS_CAP, description="最多返回多少个"
    )


def _code_command(rg: bool, args: SearchCodeInput, target: str) -> str:
    parts = ["rg", "--line-number", "--no-heading", "--color=never", "--with-filename"]
    if args.ignore_case:
        parts.append("--ignore-case")
    if args.fixed:
        parts.append("--fixed-strings")
    for name in SECRET_EXCLUDES:
        parts.append(f"--glob=!{name}")
    for name in SECRET_DIRS:
        parts.append(f"--glob=!**/{name}/**")
    if args.glob:
        parts.append(f"--glob={args.glob}")
    parts += ["-e", args.pattern, "--", target]
    return " ".join(shlex.quote(p) for p in parts)


def _grep_command(args: SearchCodeInput, target: str) -> str:
    # 必须显式 -E：grep 默认是 BRE，`(` `)` `+` `?` 都是字面量，
    # 同一个模式在 rg 下和在 grep 下含义会不同 —— 双后端就失去意义了。
    # -E 与 rg 的正则语义足够接近。
    parts = ["grep", "-rn", "--color=never", "-I", "-F" if args.fixed else "-E"]
    if args.ignore_case:
        parts.append("-i")
    for name in (*EXCLUDED_DIRS, *SECRET_DIRS):
        parts.append(f"--exclude-dir={name}")
    # GNU grep 的 include/exclude 是「最后一个匹配的规则胜出」：必须先给
    # include，再给 secret exclude，否则 include 会把 exclude 覆盖掉。
    if args.glob:
        parts.append(f"--include={args.glob}")
    for name in SECRET_EXCLUDES:
        parts.append(f"--exclude={name}")
    parts += ["-e", args.pattern, "--", target]
    return " ".join(shlex.quote(p) for p in parts)


def _files_command(rg: bool, args: FindFilesInput, target: str) -> str:
    if rg:
        parts = ["rg", "--files"]
        for name in SECRET_EXCLUDES:
            parts.append(f"--glob=!{name}")
        for name in SECRET_DIRS:
            parts.append(f"--glob=!**/{name}/**")
        parts += ["--glob", args.pattern, "--", target]
    else:
        parts = ["find", target, "-type", "f"]
        for name in SECRET_EXCLUDES:
            parts += ["-not", "-name", name]
        parts += ["-name", args.pattern]
        for name in (*EXCLUDED_DIRS, *SECRET_DIRS):
            parts += ["-not", "-path", f"*/{name}/*"]
    return " ".join(shlex.quote(p) for p in parts)


def _trim_to_workspace(lines: list[str], root: str) -> list[str]:
    """把绝对路径压回工作区相对路径 —— 输出短一截，模型读起来也顺。"""
    prefix = root.rstrip("/") + "/"
    return [line[len(prefix) :] if line.startswith(prefix) else line for line in lines]


def build_search_tools(settings: Settings, sandbox: WslSandbox) -> list[BaseTool]:
    workspace = resolve_workspace(settings, sandbox)
    # 探测一次就记住：每次调用都探一遍纯属浪费。
    # 显式 cwd：bwrap 模式下 workdir 不能为空。
    has_rg = sandbox.run("command -v rg", cwd=workspace).ok

    def _resolve(path: str | None) -> tuple[str, str | None]:
        if not path:
            return workspace, None
        try:
            return ensure_inside(path, workspace, cwd=workspace), None
        except SandboxPathError as exc:
            return workspace, f"路径非法：{exc}"

    def _execute(command: str, reason: str) -> tuple[str, ShellArtifact]:
        """执行并返回 (stdout, artifact)。文本自己拼，不去反解析拼好的字符串。"""
        result = sandbox.run(command, cwd=workspace)
        artifact = ShellArtifact(
            command=command,
            # 「没搜到」不是失败，不该让模型以为工具坏了
            ok=result.ok or result.exit_code == _NO_MATCH_EXIT,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            level=int(CommandLevel.READ),
            level_label=CommandLevel.READ.label,
        )
        if result.timed_out:
            return f"检索超时：{command}", artifact
        if result.exit_code not in (0, _NO_MATCH_EXIT):
            return (
                f"检索失败（exit={result.exit_code}）：\n"
                f"{redact_secrets(result.render(2000))}",
                artifact,
            )
        # 命中内容可能来自未被文件名规则覆盖的配置（例如 config/database.yml）；
        # 回灌模型前统一做一次 secret 脱敏。
        return redact_secrets(result.stdout), artifact

    def _search_code(
        pattern: str,
        reason: str,
        glob: str | None = None,
        path: str | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
        fixed: bool = False,
        ignore_case: bool = False,
    ) -> str:
        args = SearchCodeInput(
            pattern=pattern, reason=reason, glob=glob, path=path,
            max_results=max_results, fixed=fixed, ignore_case=ignore_case,
        )
        target, error = _resolve(path)
        if error:
            return pack(error, ShellArtifact(ok=False, rejected=True))

        command = _code_command(has_rg, args, target) if has_rg else _grep_command(args, target)
        stdout, artifact = _execute(command, reason)

        hits = _trim_to_workspace([line for line in stdout.splitlines() if line.strip()], workspace)
        hits = [
            line for line in hits
            if not is_protected_read_path(
                line.split(":", 1)[0], settings.read_deny_globs
            )
        ]
        shown = hits[:max_results]
        header = f"命中 {len(hits)} 条" + (
            f"，显示前 {len(shown)} 条" if len(hits) > len(shown) else ""
        )
        body = "\n".join(shown) if shown else "（无匹配）"
        return pack(f"[检索] {reason}\n$ {command}\n{header}\n{body}", artifact)

    def _find_files(
        pattern: str,
        reason: str,
        path: str | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> str:
        args = FindFilesInput(pattern=pattern, reason=reason, path=path, max_results=max_results)
        target, error = _resolve(path)
        if error:
            return pack(error, ShellArtifact(ok=False, rejected=True))

        command = _files_command(has_rg, args, target)
        stdout, artifact = _execute(command, reason)

        listing = [line for line in stdout.splitlines() if line.strip()]
        files = _trim_to_workspace(listing, workspace)
        files = [
            path for path in files
            if not is_protected_read_path(path, settings.read_deny_globs)
        ]
        # rg --files 的输出顺序不保证；排一下让结果稳定可复现
        files.sort()
        shown = files[:max_results]
        header = f"找到 {len(files)} 个文件" + (
            f"，显示前 {len(shown)} 个" if len(files) > len(shown) else ""
        )
        body = "\n".join(shown) if shown else "（没有匹配的文件）"
        return pack(f"[检索] {reason}\n$ {command}\n{header}\n{body}", artifact)

    return [
        StructuredTool.from_function(
            func=_search_code,
            name=SEARCH_CODE,
            description=(
                "在工作区里按内容搜索代码，返回「文件:行号:内容」列表。只读。\n"
                "比 shell 里拼 grep 更省事：不用自己加引号，也不会因为管道被安全策略拦下。"
            ),
            args_schema=SearchCodeInput,
        ),
        StructuredTool.from_function(
            func=_find_files,
            name=FIND_FILES,
            description="按文件名模式列出工作区里的文件。只读。",
            args_schema=FindFilesInput,
        ),
    ]


def search_backend(sandbox: WslSandbox) -> str:
    """当前会用到哪个后端，供 doctor / 测试观察。"""
    workspace = resolve_workspace(sandbox.settings, sandbox)
    return (
        "ripgrep"
        if sandbox.run("command -v rg", cwd=workspace).ok
        else "grep"
    )
