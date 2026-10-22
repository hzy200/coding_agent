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

import hashlib
import re
import shlex
from typing import Literal

from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import CallArtifact, ShellArtifact, pack

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
# unittest 的失败行：FAIL: test_x (module.Class.test_x)
_UNITTEST_FAILED = re.compile(
    r"^(?:FAIL|ERROR):\s+\S+\s+\((?P<loc>[A-Za-z_][\w.]*)\)", re.MULTILINE
)
# 允许的 pytest nodeid：路径段 + ::测试名；不接受空白、shell 元字符与 `..`
_PYTEST_NODEID = re.compile(r"^[A-Za-z0-9_./-]+(?:::[A-Za-z0-9_\[\].-]+)*$")
_UNITTEST_NODEID = re.compile(r"^[A-Za-z_][\w.]*$")
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

    # ok / failed / error / skipped / not_configured
    # error 表示**验证环境本身不可用**（WSL 掉线、命令无法启动），
    # 与「测试失败」不同：不能拿它去驱动 repair，也不能当成通过。
    status: Literal[
        "ok", "failed", "error", "skipped", "not_configured"
    ] = "skipped"
    command: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None
    timed_out: bool = False
    summary: str = ""
    issues: list[VerifyIssue] = Field(default_factory=list)
    output_tail: str = ""
    # 解析出的失败测试 nodeid（pytest）或 module.Class.test（unittest）。
    # 用于 repair 时只跑失败测试，以及识别「同一个失败反复出现」。
    failed_tests: list[str] = Field(default_factory=list)
    # 失败签名的稳定哈希：测试 id + 归一化后的错误消息（去掉行号/地址/耗时）。
    # 连续两次相同签名说明上一次修复没有改变失败本身。
    failure_signature: str = ""
    # full：跑了完整验证命令；selected：repair 时只跑了上次失败的测试；
    # selected 通过后会自动补跑 full，所以最终落盘的通常是 full。
    scope: Literal["full", "selected"] = "full"
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
        if self.status == "error":
            return (
                "验证环境异常，**本次验证没有跑起来**："
                f"{self.summary or '未知错误'}。"
                "这不代表测试通过；需要先恢复执行环境。"
            )

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


def _safe_nodeid(nodeid: str) -> bool:
    """拒绝绝对路径与 `..`：nodeid 会被拼进下一条命令。"""
    path = nodeid.split("::", 1)[0]
    if nodeid.startswith(("/", "\\")):
        return False
    return ".." not in path.split("/") and ".." not in path.split("\\")


def parse_failed_tests(output: str, *, limit: int = MAX_ISSUES) -> list[str]:
    """从测试输出里抽出失败测试 id，保持出现顺序并去重。

    只接受白名单形状的 nodeid：repair 会把它拼进下一条测试命令，
    不能让测试输出（可能由模型写的测试文件产生）注入任意参数或路径。
    """
    found: list[str] = []
    seen: set[str] = set()

    for match in _PYTEST_FAILED.finditer(output):
        loc = match.group("loc")
        if not _safe_nodeid(loc) or not _PYTEST_NODEID.match(loc):
            continue
        if loc not in seen:
            seen.add(loc)
            found.append(loc)
        if len(found) >= limit:
            return found

    for match in _UNITTEST_FAILED.finditer(output):
        loc = match.group("loc")
        if not _safe_nodeid(loc) or not _UNITTEST_NODEID.match(loc):
            continue
        if loc not in seen:
            seen.add(loc)
            found.append(loc)
        if len(found) >= limit:
            return found

    return found


def compute_failure_signature(
    *,
    status: str,
    kind: str,
    failed_tests: list[str],
    issues: list[VerifyIssue],
) -> str:
    """稳定的失败指纹：同一测试、同一类错误得到同一个值。

    去掉行号、内存地址、耗时这类每次都会变的东西 —— 否则签名会漂移，
    停滞检测永远不触发。
    """
    if status != "failed":
        return ""
    parts = [status, kind, *failed_tests]
    # 即使已知失败测试 id，也要带上归一化后的错误消息：同一个测试可能换了
    # 一种失败方式（断言变了/异常类型变了），那不算「原地打转」。
    for issue in issues[:3]:
        path = issue.location.split(":", 1)[0]
        message = re.sub(r":\d+(?::\d+)?", "", issue.message)
        # 对象地址通常带一个 "at" 前缀；先连它一起删，避免留下孤零零的 at
        message = re.sub(r"\s+at\s+0x[0-9a-fA-F]+\b", "", message)
        message = re.sub(r"\b0x[0-9a-fA-F]+\b", "", message)
        message = re.sub(r"\s+", " ", message).strip()
        parts.append(f"{path}:{message}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _selected_command(resolved: str, only: tuple[str, ...]) -> str:
    """把失败测试拼成一条 pytest 命令；不安全或不支持时返回空串。"""
    if not only or "__AGENT_SYNTAX__" in resolved:
        return ""
    # 只对「就是 pytest」的命令做追加；`cd x && pytest` / `pytest && ruff`
    # 这类组合命令追加参数会改变语义，退回全量更安全。
    if not (resolved.startswith("python3 -m pytest") or resolved.startswith("pytest")):
        return ""
    safe = []
    for nodeid in only:
        if not _safe_nodeid(nodeid) or not _PYTEST_NODEID.match(nodeid):
            continue
        safe.append(nodeid)
    if not safe:
        return ""
    return f"{resolved} {' '.join(shlex.quote(nodeid) for nodeid in safe)}"


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
# 在没有测试的项目上整条验证脊柱静默失效（原 IMPROVEMENT_PLAN 的 P0-2，
# 去向见 docs/archive/README.md）。
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


def related_test_paths(
    sandbox: WslSandbox,
    root: str,
    changed: list[str],
) -> list[str]:
    """从改动文件推断相关测试文件。

    只做**文件名级**映射（`foo.py` → `test_foo.py` / `foo_test.py`），
    不做 import 图分析：误判的代价是漏跑测试，所以只在能明确匹配时收窄；
    匹配不到就返回空，让调用方退回完整验证。
    """
    changed_py = [
        str(path).replace("\\", "/")
        for path in changed
        if str(path).endswith(".py")
    ]
    if not changed_py:
        return []
    script = (
        "find . -type f \\( -name 'test_*.py' -o -name '*_test.py' \\) "
        "-not -path './.agent/*' -not -path '*/__pycache__/*' "
        "| sed 's|^\\./||' | sort"
    )
    result = sandbox.run(script, cwd=root)
    if not result.ok:
        return []
    available = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    selected: set[str] = set()
    for path in changed_py:
        name = path.rsplit("/", 1)[-1]
        if name.startswith("test_") or name.endswith("_test.py"):
            if path in available:
                selected.add(path)
            continue
        stem = name[:-3]
        wanted = {f"test_{stem}.py", f"{stem}_test.py"}
        for candidate in available:
            if candidate.rsplit("/", 1)[-1] in wanted:
                selected.add(candidate)
    return sorted(selected)[:20]


def run_verification(
    sandbox: WslSandbox,
    root: str,
    *,
    command: str = "",
    allow_manifest: bool = True,
    only: tuple[str, ...] = (),
    full_after_selected: bool = True,
    timeout: int | None = None,
) -> VerifyResult:
    """执行一次验证并解析结果。

    只接受 sandbox 一个配置源 —— 超时、资源上限、verify_command 都从
    `sandbox.settings` 读，避免出现"传进来的 settings 和沙箱实际用的不一致"。

    `allow_manifest=False` 供**自动 verify** 使用：它不过审批，因此不能执行
    命令文本来自工作区文件的命令（见 `_detect`）。模型主动调 `run_tests`
    走的是默认的 True —— 那条路径有审批兜着。

    `only` 是 repair/影响分析的快速通道：只跑指定 pytest 测试。
    `full_after_selected=True`（默认）时，子集通过后补跑完整命令，避免修复
    悄悄破坏其它测试；编排写 worker 传 False，把全量验证留给合并后的主 agent。
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
    scope: Literal["full", "selected"] = "full"
    effective = resolved
    selected = _selected_command(resolved, only)
    if selected:
        effective = selected
        scope = "selected"

    # 测试/构建通常比普通命令长；没显式给超时就用独立的 verify_timeout。
    if timeout is None:
        timeout = int(getattr(sandbox.settings, "verify_timeout", 0) or 0)
    capture_limit = int(
        getattr(sandbox.settings, "max_capture_bytes", 0) or 0
    )
    result = sandbox.run(
        effective,
        cwd=root,
        timeout=timeout or None,
        capture_limit=capture_limit,
    )

    # 默认补跑完整套件抓回归；编排 worker 可以把全量留给合并后的主 agent。
    if scope == "selected" and result.ok and full_after_selected:
        effective = resolved
        scope = "full"
        result = sandbox.run(
            effective,
            cwd=root,
            timeout=timeout or None,
            capture_limit=capture_limit,
        )

    combined = f"{result.stdout}\n{result.stderr}"
    status = "ok" if result.ok else "failed"
    issues = [] if result.ok else parse_issues(combined)
    failed_tests = parse_failed_tests(combined) if not result.ok else []

    return VerifyResult(
        status=status,
        kind=kind,
        command=effective,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        summary="超时" if result.timed_out else extract_summary(combined),
        issues=issues,
        output_tail=combined.strip()[-OUTPUT_TAIL_CHARS:],
        failed_tests=failed_tests,
        failure_signature=compute_failure_signature(
            status=status, kind=kind, failed_tests=failed_tests, issues=issues
        ),
        scope=scope,
    )


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

class RunTestsInput(BaseModel):
    reason: str = Field(description="跑测试的意图，一句话说明")
    selector: str | None = Field(
        default=None,
        description=(
            "可选：只跑一个 pytest 用例/文件，例如 "
            "tests/test_x.py::test_y 或 tests/test_x.py"
        ),
    )
    timeout: int | None = Field(
        default=None,
        ge=1,
        le=3600,
        description="可选：本次命令的超时秒数；省略使用 AGENT_VERIFY_TIMEOUT",
    )


def build_test_tool(settings: Settings, sandbox: WslSandbox):
    """模型主动调用测试的工具。

    与自动 verify 用的是同一个 runner，但因为是模型发起的，仍然走审批。
    """
    from langchain_core.tools import StructuredTool

    root = resolve_workspace(settings, sandbox)

    def _run(
        reason: str,
        selector: str | None = None,
        timeout: int | None = None,
    ) -> str:
        only = (selector,) if selector and selector.strip() else ()
        result = run_verification(
            sandbox, root, only=only, timeout=timeout
        )
        text = f"[验证] {reason}\n{result.render()}"
        if only and result.scope != "selected":
            text += (
                "\n（注意：当前测试命令不支持只跑指定用例，"
                "已退回运行完整套件。）"
            )
        if result.status in {"not_configured", "skipped"}:
            # 没跑验证 != 验证通过。`VerifyResult.ok` 为兼容历史调用仍把它们
            # 当 True，但这里必须如实标成 skipped，事件与审计才不会记成成功。
            artifact = CallArtifact(
                tool=RUN_TESTS_TOOL,
                ok=False,
                skipped=True,
                level=2,
                level_label="L2 变更性",
                duration_ms=result.duration_ms,
            )
        else:
            artifact = ShellArtifact(
                command=result.command,
                ok=result.status == "ok",
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
            "可选 selector 只跑单个 pytest 用例/文件；可选 timeout 覆盖默认验证超时。"
        ),
        args_schema=RunTestsInput,
    )
