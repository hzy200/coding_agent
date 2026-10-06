"""测试运行与错误结构化解析。

verify 节点与 `run_tests` 工具共用这一套：探测项目用哪个测试命令、执行、
把输出解析成结构化 issue。

**为什么解析输出**：原始 stdout 回灌给模型既长又散，模型得自己在一堆噪声里
找失败原因。抽成 `文件:行号 + 消息` 之后，回灌的是可直接定位的清单。

**为什么自动 verify 不需要审批**：它跑的是宿主按项目清单推导出的固定命令，
模型无法影响跑什么；而模型主动调用的 `run_tests` 走正常审批。
这个区别正是"谁决定执行什么"。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import ShellArtifact, pack

RUN_TESTS_TOOL = "run_tests"

MAX_ISSUES = 20
OUTPUT_TAIL_CHARS = 4_000

# 探测顺序：越靠前越具体，避免 polyglot 仓库选错
_PROBE_FILES = (
    "Cargo.toml",
    "go.mod",
    "package.json",
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "Makefile",
)

# 编译器的定点报错：path/to/file.py:12:5: error: ...
_LOCATED_ERROR = re.compile(
    r"^(?P<loc>(?=[^\s:]*[./\\])[^\s:]+):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?P<msg>.+)$",
    re.MULTILINE,
)
# Python traceback：File "path", line N, in func，紧跟一行源码
_TRACEBACK = re.compile(
    r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>[^\n]*)\n(?P<source>[^\n]*)',
    re.MULTILINE,
)
# pytest 的失败摘要行：FAILED tests/test_x.py::test_y - AssertionError: ...
_PYTEST_FAILED = re.compile(
    r"^FAILED\s+(?P<loc>\S+?)(?:\s+-\s+(?P<msg>.+))?$", re.MULTILINE
)
# pytest 的断言细节行：E   assert 1 == 2
_ASSERTION_LINE = re.compile(r"^E\s+(?P<msg>\S.*)$", re.MULTILINE)
# 异常收尾行：AssertionError: 0 != 2（unittest 与 pytest 都有）
_EXCEPTION_LINE = re.compile(
    r"^(?P<kind>[A-Za-z_][\w.]*(?:Error|Exception)):\s*(?P<msg>.+)$", re.MULTILINE
)


class VerifyIssue(BaseModel):
    """一条结构化错误。location 形如 `tests/test_x.py:12`。"""

    location: str = ""
    message: str = ""


class VerifyResult(BaseModel):
    """一次验证的结果。字段都是可 JSON 化的，要能进 checkpoint。"""

    # ok / failed / skipped / not_configured
    status: Literal["ok", "failed", "skipped", "not_configured"] = "skipped"
    command: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None
    timed_out: bool = False
    summary: str = ""
    issues: list[VerifyIssue] = Field(default_factory=list)
    output_tail: str = ""
    # 这次验证**有多强**：`tests`（跑了项目自己的测试）| `syntax`（只做了语法解析级兜底）
    # | `none`（什么都没跑）。
    #
    # 单列一个字段是因为「通过」的含金量差别很大：语法过了不等于行为对。
    # 让调用方能把它显示出来、也能按它决定要不要更谨慎地收尾。
    kind: Literal["tests", "syntax", "none"] = "none"

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped", "not_configured")

    @property
    def ran(self) -> bool:
        return self.status in ("ok", "failed")

    def render(self) -> str:
        """回灌给模型的紧凑文本。"""
        if self.status == "not_configured":
            return "未检测到可用的测试命令，也没有可解析的源码 —— 本次没有做任何验证。"
        if self.status == "skipped":
            return "本次未执行验证。"

        head = f"验证命令：{self.command}\nexit_code={self.exit_code}"
        if self.summary:
            head += f"\n{self.summary}"
        if self.status == "ok":
            if self.kind == "syntax":
                return (
                    f"{head}\n语法校验通过 —— 项目里没有可识别的测试命令，"
                    f"所以只做了这一层。**行为是否正确没有被验证。**"
                )
            return f"{head}\n验证通过。"

        lines = [head, f"发现 {len(self.issues)} 个问题："]
        for issue in self.issues:
            where = f"{issue.location} " if issue.location else ""
            lines.append(f"- {where}{issue.message}")
        if self.output_tail:
            lines.append(f"\n原始输出（末尾）：\n{self.output_tail}")
        return "\n".join(lines)


def parse_issues(output: str, *, limit: int = MAX_ISSUES) -> list[VerifyIssue]:
    """从测试/编译输出里抽出「位置 + 消息」。

    先取定点报错（信息最全），再补 pytest 的 FAILED 摘要，
    最后用断言细节兜底 —— 顺序保证同一处问题不会被两种格式重复记录。
    """
    issues: list[VerifyIssue] = []
    seen: set[tuple[str, str]] = set()

    def add(location: str, message: str) -> None:
        message = " ".join(message.split())
        key = (location, message)
        if not message or key in seen or len(issues) >= limit:
            return
        seen.add(key)
        issues.append(VerifyIssue(location=location, message=message[:400]))

    def add_exception_reasons() -> None:
        for match in _EXCEPTION_LINE.finditer(output):
            add("", f"{match.group('kind')}: {match.group('msg')}")

    # 按输出格式分派，而不是让某一种模式抢先返回。
    # Python traceback（unittest）位置最全：文件:行 + 出错源码，再补异常原因。
    tracebacks = list(_TRACEBACK.finditer(output))
    if tracebacks:
        for match in tracebacks:
            source = match.group("source").strip() or f"in {match.group('func').strip()}"
            add(f"{match.group('path')}:{match.group('line')}", source)
        add_exception_reasons()
        return issues

    # 定点报错（mypy / gcc）以及 pytest 的 `path:line: AssertionError` 收尾行
    located = list(_LOCATED_ERROR.finditer(output))
    if located:
        for match in located:
            add(f"{match.group('loc')}:{match.group('line')}", match.group("msg"))
        # pytest 的 FAILED 摘要另带「哪个测试、为什么失败」，与上面的行号互补
        for match in _PYTEST_FAILED.finditer(output):
            add(match.group("loc"), match.group("msg") or "测试失败")
        add_exception_reasons()
        return issues

    failed = list(_PYTEST_FAILED.finditer(output))
    if failed:
        for match in failed:
            add(match.group("loc"), match.group("msg") or "测试失败")
        for match in _ASSERTION_LINE.finditer(output):
            add("", match.group("msg").split(" - ")[0].strip())
        return issues

    # 兜底：先看断言细节，再退到异常行
    for match in _ASSERTION_LINE.finditer(output):
        add("", match.group("msg").split(" - ")[0].strip())
    if not issues:
        add_exception_reasons()

    return issues


def extract_summary(output: str) -> str:
    """挑出最有信息量的一行作为摘要。

    覆盖 pytest（`1 failed, 2 passed in 0.05s`）与 unittest（`FAILED (failures=1)`）两种收尾格式。
    """
    for pattern in (
        r"^.*\b\d+ (?:failed|passed|error).*$",
        r"^(?:FAILED|OK)\b.*$",
    ):
        match = re.search(pattern, output, re.MULTILINE)
        if match:
            return match.group(0).strip(" =")

    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


# --------------------------------------------------------------------------
# 命令探测
# --------------------------------------------------------------------------

# 没有可识别的测试时的**降级验证**：逐个 `.py` 做语法解析。
#
# 为什么要它：识别不出测试命令 → `not_configured` → 路由判为非 failed → 直接
# advance，审计还记成 `ok=True`。于是**「没验证」与「验证通过」在账上长得一样**，
# 在没有测试的项目上整条验证脊柱静默失效（IMPROVEMENT_PLAN 的 P0-2）。
# 今天 P0-3 的修复又放大了它：`make test` / `npm test` 不再走自动 verify，
# 这类项目现在必然落进 `not_configured`。
#
# 为什么是语法级而不是更强的东西：沙箱里只有 `python3`（没有 ruff / mypy / tsc /
# go）。**能确定做到的那一点，比做不到的承诺有用** —— 语法错是"改了但根本跑不起来"
# 这一类里最硬的信号，而且解析不写任何文件（不产 `__pycache__`，不污染工作区）。
_SYNTAX_CHECK = """\
python3 - <<'__AGENT_SYNTAX__'
import ast, pathlib, sys

problems = []
for path in sorted(pathlib.Path(".").rglob("*.py")):
    if any(part.startswith(".") for part in path.parts):
        continue
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        problems.append("%s:%s: %s" % (path, exc.lineno or 0, exc.msg))
    except (UnicodeDecodeError, OSError):
        continue  # 非文本或读不了的文件不该让校验失败

for line in problems:
    print(line)
sys.exit(1 if problems else 0)
__AGENT_SYNTAX__
"""


def _probe_script(files: tuple[str, ...]) -> str:
    quoted = " ".join(f'"{name}"' for name in files)
    return (
        f"for f in {quoted}; do [ -e \"$f\" ] && echo \"FILE $f\"; done; "
        f"[ -d tests ] && echo \"DIR tests\"; "
        f"[ -d test ] && echo \"DIR test\"; "
        f"command -v python3 >/dev/null && python3 -c 'import pytest' 2>/dev/null "
        f"&& echo \"HAS pytest\"; "
        f"[ -f Makefile ] && grep -qE '^test:' Makefile && echo \"MAKE test\"; "
        f"[ -f package.json ] && grep -q '\"test\"' package.json && echo \"NPM test\"; "
        # 降级验证的判据：这个项目里有没有可解析的源码
        f"find . -maxdepth 3 -name '*.py' -not -path '*/.*' -print -quit 2>/dev/null "
        f"| grep -q . && echo \"HAS python\""
    )


def _detect(sandbox: WslSandbox, root: str, *, allow_manifest: bool) -> str:
    """真的去沙箱里探一次（一次 wsl.exe），返回命令或空串。

    `allow_manifest=False` 时只认**命令文本完全由宿主确定**的那几条：
    `make test` 跑什么写在 Makefile 里、`npm test` 跑什么写在 package.json 里，
    而这两个文件模型可以用 file_write 改（`--write` 下 L1 自动放行）——
    于是「写个恶意 Makefile → 下一次脏写自动执行」就成了一条绕过命令分级的
    执行路径。带上 `allow_manifest=True` 的只有走审批的 `run_tests` 工具。
    """
    result = sandbox.run(_probe_script(_PROBE_FILES), cwd=root)
    lines = {line.strip() for line in result.stdout.splitlines() if line.strip()}

    if "FILE Cargo.toml" in lines:
        return "cargo test"
    if "FILE go.mod" in lines:
        return "go build ./... && go test ./..."

    has_python_tests = (
        "HAS pytest" in lines
        and (
            "DIR tests" in lines
            or "DIR test" in lines
            or "FILE pytest.ini" in lines
            or "FILE tox.ini" in lines
        )
    )
    if has_python_tests:
        return "python3 -m pytest -q"

    if not allow_manifest:
        # 降级：没有可识别的测试命令时，**至少**做一遍语法解析。
        # 不这么做的话这里会返回空串 → `not_configured` → 路由当作没失败 →
        # 审计记 `ok=True`，「没验证」与「验证通过」就分不开了（P0-2）。
        return _SYNTAX_CHECK if "HAS python" in lines else ""

    if "NPM test" in lines:
        return "npm test --silent"
    if "MAKE test" in lines:
        return "make test"

    return _SYNTAX_CHECK if "HAS python" in lines else ""


def detect_test_command(
    sandbox: WslSandbox,
    root: str,
    *,
    override: str = "",
    allow_manifest: bool = True,
) -> str:
    """推导出该项目的验证命令；没有可用的返回空串。

    探测结果按工作区缓存：一次运行里验证会跑很多遍（每步一遍、修复后再一遍），
    而项目清单不会变，每次重探都是白跑一次 wsl.exe（约 0.3s）。

    **只缓存"探到了"，不缓存"没探到"** —— 任务很可能是先建目录、写好
    pyproject.toml 才第一次出现可识别的测试命令；把空结果也记住，后面就再
    也发现不了它了，验证会一直停在 `not_configured`。

    缓存键带上 `allow_manifest`：两种口径答案可能不同（Makefile-only 的项目
    在只读口径下是空串、在审批口径下是 `make test`），共用一个键会让先跑的
    那次把答案灌给另一次 —— 审批口径的结果漏进无人审批的自动 verify 是最坏的方向。
    """
    if override.strip():
        return override.strip()

    key = f"test_command:{root}:{int(allow_manifest)}"
    found = sandbox.cached_probe(
        key, lambda: _detect(sandbox, root, allow_manifest=allow_manifest)
    )
    # 不缓存**降级**结果，理由与"不缓存没探到"是同一条：语法校验是这里最弱的一档，
    # 而任务常常是先建目录、后写测试 —— 把兜底记死了，真正的测试命令就再也发现不了。
    # 代价是没测试的项目每次验证多探一次（约 0.25s），可以接受。
    if not found or found == _SYNTAX_CHECK:
        sandbox.forget_probe(key)
    return found


def run_verification(
    sandbox: WslSandbox,
    root: str,
    *,
    command: str = "",
    allow_manifest: bool = True,
) -> VerifyResult:
    """执行一次验证并解析结果。

    只接受 sandbox 一个配置源 —— 超时、资源上限、verify_command 都从
    `sandbox.settings` 读，避免出现"传进来的 settings 和沙箱实际用的不一致"。

    `allow_manifest=False` 供**自动 verify** 使用：它不过审批，因此不能执行
    命令文本来自工作区文件的命令（见 `_detect`）。模型主动调 `run_tests`
    走的是默认的 True —— 那条路径有审批兜着。
    """
    resolved = detect_test_command(
        sandbox,
        root,
        override=command or sandbox.settings.verify_command,
        allow_manifest=allow_manifest,
    )
    if not resolved:
        return VerifyResult(status="not_configured")

    kind = "syntax" if resolved.startswith("python3 - <<'__AGENT_SYNTAX__'") else "tests"
    result = sandbox.run(resolved, cwd=root)
    combined = f"{result.stdout}\n{result.stderr}"
    issues = [] if result.ok else parse_issues(combined)

    return VerifyResult(
        status="ok" if result.ok else "failed",
        kind=kind,
        command=resolved,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        summary="超时" if result.timed_out else extract_summary(combined),
        issues=issues,
        output_tail=combined.strip()[-OUTPUT_TAIL_CHARS:],
    )


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

class RunTestsInput(BaseModel):
    reason: str = Field(description="跑测试的意图，一句话说明")


def build_test_tool(settings: Settings, sandbox: WslSandbox):
    """模型主动调用测试的工具。

    与自动 verify 用的是同一个 runner，但因为是模型发起的，仍然走审批。
    """
    from langchain_core.tools import StructuredTool

    root = resolve_workspace(settings, sandbox)

    def _run(reason: str) -> str:
        result = run_verification(sandbox, root)
        text = f"[验证] {reason}\n{result.render()}"
        artifact = ShellArtifact(
            command=result.command,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            level=2,
            level_label="L2 变更性",
        )
        return pack(text, artifact)

    return StructuredTool.from_function(
        func=_run,
        name=RUN_TESTS_TOOL,
        description=(
            "运行本项目的测试/构建命令并返回结构化结果。"
            f"命令由宿主从项目清单推导（{settings.verify_command or '自动识别'}），"
            "你也可以在 AGENT_VERIFY_COMMAND 里预先指定。"
        ),
        args_schema=RunTestsInput,
    )
