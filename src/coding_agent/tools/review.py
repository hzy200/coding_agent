"""代码审查：确定性静态检查 + 可选外部 linter。

为什么自己实现，不用 ruff / mypy
--------------------------------

沙箱里 `python3` 一定有，`ruff` / `mypy` / `tsc` **不一定有**（本机实测：三者都没有）。
把审查建立在"装了才跑得起来"的工具上，等于在没装的环境里让这条质量关**静默失效** ——
而 `verify` 已经吃过一次同款亏（识别不出测试命令就 `not_configured`，整条验证脊柱
形同不存在）。所以这里的主干是**零依赖的确定性检查**，外部 linter 只作为加分项：
探到就跑，探不到就如实说明，不假装审过。

改动前的内容从哪来
------------------

**用 `SnapshotStore` 的留底**（`.agent/backups/<snapshot_id>/<path>`），不另建基线。
这是直接吃「不变量 8：所有写操作先留底」的红利 —— 文件工具每次落盘前都留了底，
所以"这次改了什么"本来就有权威记录，不需要再加一套影子存储。

代价说清楚：**经 shell 改的文件（`sed -i`）没有留底，会完全逃过审查**。
系统提示禁止用 shell 改源码，但那是软约束。这条质量关**不是安全边界**。

水位线
------

只看 `snapshot_id > watermark` 的留底。没有水位线会出一个很隐蔽的错：留底记的是
**写前内容**，所以上一步改过的文件会永远与它自己的留底不同 —— 于是每一步都把
前面所有步骤的改动重审一遍，同一个问题反复告警，直到把修复预算耗光。
水位线由 review 节点自己推进，**跨步骤保留、只在整轮开始时归零**（见 graph/state.py）。
"""

from __future__ import annotations

import base64
import json
import posixpath
import re
from typing import Literal

from pydantic import BaseModel, Field

from coding_agent.sandbox.snapshots import ABSENT_SUFFIX, BACKUP_DIRNAME
from coding_agent.sandbox.wsl_exec import WslSandbox

BLOCKING = "blocking"
WARNING = "warning"

# 单次审查最多比对多少个文件。改动极多时审前 N 个并如实说明，而不是无界地跑下去。
MAX_REVIEW_FILES = 200
MAX_FINDINGS = 40

# 审查脚本的输出标记。用标记而不是"整段 stdout 就是 JSON"：命令一旦有额外输出
# （警告、profile 之类），直接 json.loads 会崩，而崩了就会被当成"审过了没问题"。
_MARKER = "__AGENT_REVIEW_JSON__"


class ReviewFinding(BaseModel):
    """一条审查发现。

    `severity=blocking` 表示结构性缺陷（语法坏、测试被改弱、污染 .agent/），
    会路由回 repair；`warning` 只记录，不阻断。
    """

    severity: Literal["blocking", "warning"] = WARNING
    rule: str = ""
    location: str = ""
    message: str = ""


class ReviewResult(BaseModel):
    """一次代码审查的结果。字段都可 JSON 化（要进 checkpoint）。"""

    # clean（没问题）/ warned（只有告警）/ blocked（有阻断项）/ skipped（没跑）
    status: Literal["clean", "warned", "blocked", "skipped"] = "skipped"
    findings: list[ReviewFinding] = Field(default_factory=list)
    checked_files: int = 0
    # 本节点推进后的水位线；调用方要写回 state
    watermark: str = ""
    linters: list[str] = Field(default_factory=list)
    summary: str = ""

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    @property
    def blocking_findings(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.severity == BLOCKING]

    @property
    def warnings(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.severity == WARNING]

    def render(self) -> str:
        """回灌给模型的紧凑文本。"""
        if self.status == "skipped":
            return "本次未执行代码审查。"
        if self.status == "clean":
            return f"代码审查通过（审了 {self.checked_files} 个改动文件）。"

        head = (
            f"代码审查：审了 {self.checked_files} 个改动文件，"
            f"{len(self.blocking_findings)} 个阻断问题、{len(self.warnings)} 个告警。"
        )
        lines = [head]
        for finding in self.findings[:MAX_FINDINGS]:
            where = f"{finding.location} " if finding.location else ""
            lines.append(f"- [{finding.severity}] {where}{finding.message}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 沙箱内跑的检查脚本
# --------------------------------------------------------------------------

# 只对**新增行**生效的行级规则。分三类，判据写在各自的分组上。
_SCRIPT_BODY = r'''
import ast, base64, difflib, json, os, re

CFG = json.loads(base64.b64decode("«CFG»").decode("utf-8"))
ROOT = CFG["root"]
BACKUP = CFG["backup_root"]
WATERMARK = CFG["watermark"]
ABSENT = CFG["absent_suffix"]
MAX_FILES = CFG["max_files"]

findings = []


def add(severity, rule, location, message):
    findings.append(
        {"severity": severity, "rule": rule, "location": location, "message": message}
    )


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def is_test_file(rel):
    base = os.path.basename(rel)
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or "/tests/" in "/" + rel
        or "/test/" in "/" + rel
    )


def added_lines(before, current):
    """返回 [(行号, 内容)]，只含**新增**的行。

    difflib 而不是"逐行求差集"：差集会把同一行内容在别处出现也算成新增。
    """
    out = []
    lineno = 0
    diff = difflib.unified_diff(
        before.splitlines(), current.splitlines(), n=0, lineterm=""
    )
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            lineno = (int(match.group(1)) - 1) if match else lineno
            continue
        if line.startswith("+"):
            lineno += 1
            out.append((lineno, line[1:]))
    return out


# 结构性缺陷：调试残留会让程序在运行中停住，测试里留着等于没测。
BLOCKING_ANY = (
    ("debug-breakpoint", re.compile(r"\bbreakpoint\s*\("),
     "新增了 breakpoint()，运行时会停住"),
    ("debug-pdb", re.compile(r"\bpdb\.set_trace\s*\("),
     "新增了 pdb.set_trace()，运行时会停住"),
)
# 只对测试文件生效：把测试改弱以迎合实现，是"测试通过"最廉价的伪造方式。
BLOCKING_TEST = (
    ("test-skipped",
     re.compile(r"@\s*(?:unittest\.)?skip\b|@\s*pytest\.mark\.(?:skip|xfail)\b"),
     "新增了跳过标记：这条测试被绕过了"),
    ("test-weakened", re.compile(r"\bassert\s+True\s*(?:[,)#]|$)"),
     "新增了 `assert True`：断言被架空"),
    ("test-weakened", re.compile(r"\bassertTrue\(\s*True\s*\)"),
     "新增了 assertTrue(True)：断言被架空"),
)
WARNING_ANY = (
    ("debug-print", re.compile(r"^\s*print\s*\("), "新增了 print("),
    ("todo-marker", re.compile(r"\b(?:TODO|FIXME|XXX)\b"), "新增了 TODO/FIXME 标记"),
)


def inspect(rel, before, current):
    if rel == ".agent" or rel.startswith(".agent/"):
        add("blocking", "agent-state", rel, "改动落在 agent 自己的状态目录 .agent/ 下")
        return

    if rel.endswith(".py"):
        try:
            ast.parse(current)
        except SyntaxError as exc:
            add(
                "blocking",
                "syntax-error",
                "%s:%s" % (rel, exc.lineno or 0),
                "语法错误：%s" % exc.msg,
            )

    in_test = is_test_file(rel)
    for lineno, text in added_lines(before, current):
        where = "%s:%d" % (rel, lineno)
        for rule, pattern, message in BLOCKING_ANY:
            if pattern.search(text):
                add("blocking", rule, where, message)
        if in_test:
            for rule, pattern, message in BLOCKING_TEST:
                if pattern.search(text):
                    add("blocking", rule, where, message)
        for rule, pattern, message in WARNING_ANY:
            if pattern.search(text):
                add("warning", rule, where, message)


# ---- 找出水位线之后每个路径**最早**的那份留底当"改动前" ----
earliest = {}
try:
    snapshot_ids = sorted(
        name for name in os.listdir(BACKUP)
        if os.path.isdir(os.path.join(BACKUP, name))
    )
except OSError:
    snapshot_ids = []

for sid in snapshot_ids:
    if sid <= WATERMARK:
        continue
    base = os.path.join(BACKUP, sid)
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            absent = rel.endswith(ABSENT)
            if absent:
                rel = rel[: -len(ABSENT)]
            # 同一路径在一批留底里取最早的那份 —— 那才是"这一步开始前"的内容。
            # 取最新的会把同一步内的多次修改只算最后一笔。
            earliest.setdefault(rel, {"sid": sid, "absent": absent})

checked = 0
truncated = 0
for rel in sorted(earliest):
    if checked >= MAX_FILES:
        truncated = len(earliest) - checked
        break
    info = earliest[rel]
    if info["absent"]:
        before = ""
    else:
        before = read(os.path.join(BACKUP, info["sid"], rel))
        if before is None:
            continue
    current = read(os.path.join(ROOT, rel))
    if current is None or current == before:
        continue
    checked += 1
    inspect(rel, before, current)

if truncated:
    add(
        "warning",
        "too-many-files",
        "",
        "改动文件过多，只审了前 %d 个（另有 %d 个未审）" % (MAX_FILES, truncated),
    )

newest = max(snapshot_ids) if snapshot_ids else WATERMARK
if newest < WATERMARK:
    newest = WATERMARK

print("«MARKER»" + json.dumps({
    "findings": findings,
    "checked_files": checked,
    "watermark": newest,
}))
'''


def review_config(root: str, *, watermark: str = "") -> dict:
    return {
        "root": root,
        "backup_root": posixpath.join(root, BACKUP_DIRNAME),
        "watermark": watermark,
        "absent_suffix": ABSENT_SUFFIX,
        "max_files": MAX_REVIEW_FILES,
    }


def script_body(root: str, *, watermark: str = "") -> str:
    """脚本正文（纯 Python，不依赖沙箱）。

    拆出来是为了能**用宿主 Python 直接跑它**：检查逻辑全是 stdlib 文件操作，
    没有理由非要在 WSL 里才能测 —— 单测因此不需要 `-m wsl`。

    配置以 base64 内联在脚本里（而不是作为命令行参数）：命令行有长度上限，
    而这个脚本还要经 `wrap_with_limits` 的内层 heredoc，别在启动上再埋一个坑。
    """
    cfg = review_config(root, watermark=watermark)
    payload = base64.b64encode(json.dumps(cfg).encode("utf-8")).decode("ascii")
    return _SCRIPT_BODY.replace("«CFG»", payload).replace("«MARKER»", _MARKER)


def build_script(root: str, *, watermark: str = "") -> str:
    """沙箱内执行的完整命令（经 stdin heredoc 交给 bash）。"""
    body = script_body(root, watermark=watermark)
    return f"python3 - <<'__AGENT_REVIEW__'\n{body}\n__AGENT_REVIEW__"


# --------------------------------------------------------------------------
# 可选外部 linter
# --------------------------------------------------------------------------

# 命令文本**全部由宿主写死**：review 与自动 verify 一样不过审批，不能执行
# 任何来自工作区文件的命令（理由见 graph/nodes/verify.py）。
_LINTERS: tuple[tuple[str, str, str], ...] = (
    ("ruff", "command -v ruff", "ruff check --output-format=concise ."),
    ("mypy", "python3 -c 'import mypy'", "python3 -m mypy --no-color-output ."),
)

# ruff concise 与 mypy 都是 `路径:行[:列]: 消息`
_LINT_LINE = re.compile(r"^(?P<path>[^\s:][^:]*):(?P<line>\d+)(?::\d+)?:\s*(?P<msg>.+)$")


def available_linters(sandbox: WslSandbox) -> list[str]:
    """沙箱里装了哪些 linter（探测结果按实例缓存）。"""
    return [
        name
        for name, probe, _command in _LINTERS
        if sandbox.cached_probe(f"linter:{name}", lambda p=probe: sandbox.run(p).ok)
    ]


def _lint_findings(sandbox: WslSandbox, root: str, names: list[str]) -> list[ReviewFinding]:
    """跑一遍探测到的 linter，输出按 warning 收（不阻断 repair 循环）。"""
    findings: list[ReviewFinding] = []
    for name, _probe, command in _LINTERS:
        if name not in names:
            continue
        result = sandbox.run(command, cwd=root)
        for line in (result.stdout + "\n" + result.stderr).splitlines():
            match = _LINT_LINE.match(line.strip())
            if not match:
                continue
            findings.append(
                ReviewFinding(
                    severity=WARNING,
                    rule=f"lint:{name}",
                    location=f"{match.group('path')}:{match.group('line')}",
                    message=match.group("msg").strip(),
                )
            )
            if len(findings) >= MAX_FINDINGS:
                return findings
    return findings


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def run_review(
    sandbox: WslSandbox,
    root: str,
    *,
    watermark: str = "",
    enable_linters: bool = True,
) -> ReviewResult:
    """审一遍自上次水位线以来的改动。

    脚本本身失败时返回 `warned` 并记一条可见的告警，**不静默当成"审过了"** ——
    审查没跑起来和审查通过是两件事，混为一谈等于把质量关变成摆设。
    """
    return parse_review(
        sandbox, root, sandbox.run(build_script(root, watermark=watermark), cwd=root),
        watermark=watermark, enable_linters=enable_linters,
    )


def parse_review(
    sandbox: WslSandbox,
    root: str,
    result,
    *,
    watermark: str = "",
    enable_linters: bool = True,
) -> ReviewResult:
    """把脚本输出解析成 `ReviewResult`（与执行分开，便于单测）。"""
    payload: dict | None = None
    for line in result.stdout.splitlines():
        if line.startswith(_MARKER):
            try:
                payload = json.loads(line[len(_MARKER) :])
            except ValueError:
                payload = None
            break

    if payload is None:
        return ReviewResult(
            status="warned",
            findings=[
                ReviewFinding(
                    severity=WARNING,
                    rule="review-unavailable",
                    message=f"代码审查未能执行：{result.render(300)}",
                )
            ],
            watermark=watermark,
            summary="代码审查未能执行",
        )

    findings = [ReviewFinding(**item) for item in payload["findings"]]
    linters: list[str] = []
    if enable_linters:
        linters = available_linters(sandbox)
        if linters:
            findings.extend(_lint_findings(sandbox, root, linters))

    blocking = sum(1 for f in findings if f.severity == BLOCKING)
    checked = int(payload["checked_files"])
    if blocking:
        status: Literal["clean", "warned", "blocked", "skipped"] = "blocked"
        summary = f"审查 {checked} 个改动文件：{blocking} 个阻断问题"
    elif findings:
        status = "warned"
        summary = f"审查 {checked} 个改动文件：{len(findings)} 个告警"
    else:
        status = "clean"
        summary = f"审查 {checked} 个改动文件：未发现问题"

    return ReviewResult(
        status=status,
        findings=findings,
        checked_files=checked,
        watermark=str(payload.get("watermark") or watermark),
        linters=linters,
        summary=summary,
    )
