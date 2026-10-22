"""文档—代码一致性检查（防腐化）。

没有这一步，分层做得再好也会腐烂：契约会漂移、链接会断、
文档会变成没有 owner 的孤儿、数字会各说各话。

八项检查：
    1. 契约漂移 —— docs/contracts/ 是否与源码一致（委托给 gen_contracts.py --check）
    2. 断链     —— .md / .txt 里的相对链接与 anchor 是否存在
    3. owner    —— 每份文档必须有 owner（无主文档等于无文档）
    4. 过期     —— last_reviewed 超过阈值就报警
    5. 命令卫生 —— 文档里不许出现裸 `pytest`（会真调模型、真的计费）
    6. 验收锚点 —— acceptance-criteria.yaml 引用的测试文件与用例必须存在
    7. 体量预算 —— 常驻上下文（AGENTS/llms）与模块 AGENTS 不得无限增长
    8. 不变量计数 —— ARCHITECTURE 第 6 节的条数与文档中的「N 条不变量」一致

只用标准库，不依赖项目自身可导入 —— 这样 Linux runner 上无需安装依赖也能跑。

用法：
    python scripts/check_docs.py              # 过期只警告
    python scripts/check_docs.py --strict     # 过期也算失败
    python scripts/check_docs.py --json       # 输出指标 JSON（供 CI artifact / 看板）
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
AGENTS = ROOT / "AGENTS.md"

STALE_DAYS = 90

# 不需要 owner 的文件：代码生成的契约、模板、索引本身就是事实源或占位
OWNER_EXEMPT = {
    "docs/contracts/index.md",
    "docs/contracts/events.md",
    "docs/contracts/events.json",
    "docs/contracts/tools.yaml",
    "docs/contracts/audit.yaml",
    "docs/contracts/config.md",
    "docs/contracts/config.json",
    "docs/contracts/dependencies.md",
    "docs/adr/TEMPLATE.md",
    "docs/rfc/TEMPLATE.md",
}

# 常驻上下文预算（字符数）。超限意味着低频知识正在挤占每轮注入的 token。
CONTEXT_BUDGETS = {
    "AGENTS.md": 12_000,
    "llms.txt": 8_000,
}
MODULE_AGENTS_BUDGET = 4_000
# 叙事文档的拆分信号。超过后先考虑改指针，而不是继续堆内容。
DOC_SOFT_LIMIT = 50_000

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^\s*```")
FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def collect_doc_files() -> list[Path]:
    files = sorted(DOCS.rglob("*.md"))
    files += [ROOT / "llms.txt", ROOT / "README.md"]
    files += sorted((ROOT / "tests").rglob("*.md"))
    if AGENTS.exists():
        files.append(AGENTS)
    files += sorted((ROOT / "src").rglob("AGENTS.md"))
    return files


def strip_code_blocks(text: str) -> str:
    """去掉围栏代码块，避免把示例里的路径当成真实链接。"""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)


def slugify(heading: str) -> str:
    """近似 GitHub 的 anchor 规则：小写、去掉标点、空格转连字符。

    注意：下划线要**保留**（`reasoning_content` 这类 snake_case 在 anchor 里原样出现），
    只去掉反引号与强调符。
    """
    text = heading.strip().lower()
    text = re.sub(r"`|\*", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", text)
    return re.sub(r"\s", "-", text)


def anchors_of(md_text: str) -> set[str]:
    out: set[str] = set()
    inside = False
    for line in md_text.splitlines():
        if FENCE_RE.match(line):
            inside = not inside
            continue
        if inside:
            continue
        if line.startswith("#"):
            out.add(slugify(line.lstrip("#").strip()))
    return out


# --------------------------------------------------------------------------
# 1. 契约漂移
# --------------------------------------------------------------------------


def check_contracts() -> list[str]:
    gen = ROOT / "scripts" / "gen_contracts.py"
    proc = subprocess.run(
        [sys.executable, str(gen), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if proc.returncode == 0:
        return []
    return [f"契约与源码不一致：{line}" for line in proc.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# 2. 断链
# --------------------------------------------------------------------------


def check_links(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        text = strip_code_blocks(path.read_text(encoding="utf-8"))
        for target in LINK_RE.findall(text):
            target = target.strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            file_part, _, anchor = target.partition("#")
            if not file_part:
                continue  # 同文件内 anchor，不校验
            resolved = (path.parent / file_part).resolve()
            label = f"{rel(path)} → {target}"
            if not resolved.exists():
                errors.append(f"断链：{label}（文件不存在）")
                continue
            if anchor and resolved.suffix == ".md":
                if anchor not in anchors_of(resolved.read_text(encoding="utf-8")):
                    errors.append(f"断链：{label}（anchor 不存在）")
    return errors


# --------------------------------------------------------------------------
# 3 & 4. owner 与过期
# --------------------------------------------------------------------------


def parse_metadata(text: str) -> dict[str, str]:
    """解析文档元数据，支持三种写法（按优先级）：

    1. YAML front-matter（docs/ 下的叙事/决策文档）
    2. HTML 注释（AGENTS.md —— 它会被注入 LLM 上下文，front-matter 太占地方）
    3. ADR 头部 `- **Owner**：` / `- **Date**：`（避免同一份元信息写两遍）
    """
    data: dict[str, str] = {}

    match = FRONT_MATTER_RE.match(text)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t", "-")):
                key, _, value = line.partition(":")
                data[key.strip()] = value.strip()
        return data

    for comment in re.findall(r"<!--(.*?)-->", text[:2000], re.DOTALL):
        # 一行里可能挤了多组 `key: value · key: value`，先按 · 拆再按 : 拆
        for chunk in comment.replace("\n", "·").split("·"):
            if ":" in chunk:
                key, _, value = chunk.partition(":")
                data.setdefault(key.strip(), value.strip())

    for line in text.splitlines()[:30]:
        m = re.match(r"-\s*\*\*(Owner|Date)\*\*\s*[:：]\s*(.+)", line)
        if m:
            key = "owner" if m.group(1) == "Owner" else "last_reviewed"
            data.setdefault(key, m.group(2).strip())
        # 纯文本文件（llms.txt）：`# owner: xxx` 形式的头部注释
        m = re.match(r"#\s*(owner|last_reviewed)\s*[:：]\s*(.+)", line)
        if m:
            data.setdefault(m.group(1), m.group(2).strip())

    return data


def check_owners(files: list[Path]) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    stale: list[str] = []
    today = date.today()

    for path in files:
        key = rel(path)
        if key in OWNER_EXEMPT:
            continue
        fm = parse_metadata(path.read_text(encoding="utf-8"))
        if not fm.get("owner"):
            missing.append(f"无 owner：{key}（加 front-matter 的 owner 字段，或登记豁免）")
        reviewed = fm.get("last_reviewed")
        if not reviewed:
            stale.append(f"无复核日期：{key}")
            continue
        try:
            when = datetime.strptime(reviewed, "%Y-%m-%d").date()
        except ValueError:
            stale.append(f"last_reviewed 格式错误：{key}（应为 YYYY-MM-DD，实际 {reviewed}）")
            continue
        if today - when > timedelta(days=STALE_DAYS):
            stale.append(f"疑似过期：{key}（{reviewed}，已 {(today - when).days} 天）")

    return missing, stale


# --------------------------------------------------------------------------
# 5. 命令卫生：不许出现裸 pytest
# --------------------------------------------------------------------------


def check_commands(files: list[Path]) -> list[str]:
    """文档里的 `pytest` 调用必须带 -m，否则会真调模型、真的计费。"""
    errors: list[str] = []
    for path in files:
        inside = False
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FENCE_RE.match(line):
                inside = not inside
                continue
            if not inside:
                continue
            stripped = line.strip()
            if not stripped.startswith("pytest"):
                continue
            if "-m" in stripped:
                continue
            if any(flag in stripped for flag in ("--collect-only", "--version", "--help")):
                continue
            errors.append(
                f"裸 pytest：{rel(path)}:{lineno} → {stripped}"
                "（必须带 -m，否则会跑 llm 标记用例、真的调用模型）"
            )
    return errors


# --------------------------------------------------------------------------
# 6. 验收锚点：机读需求引用的测试文件必须存在
# --------------------------------------------------------------------------


def _function_names(path: Path) -> set[str]:
    """收集测试文件里的函数/方法名，用于校验 `::test_name` 锚点。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set()
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def check_acceptance_tests() -> list[str]:
    """`tests:` 里手写的路径会随着重构漂移；文件或用例不存在时立即报错。"""
    path = DOCS / "acceptance" / "acceptance-criteria.yaml"
    if not path.exists():
        return [f"缺少验收标准：{rel(path)}"]
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []

    for test_path in sorted(set(re.findall(r"(tests/[\w./-]+\.py)", text))):
        if not (ROOT / test_path).exists():
            errors.append(f"验收标准引用了不存在的测试：{test_path}")

    cache: dict[str, set[str]] = {}
    for test_path, name in sorted(
        set(re.findall(r"(tests/[\w./-]+\.py)::([A-Za-z_]\w*)", text))
    ):
        target = ROOT / test_path
        if not target.exists():
            continue  # 文件缺失已在上一条报出
        if test_path not in cache:
            cache[test_path] = _function_names(target)
        if name not in cache[test_path]:
            errors.append(f"验收标准引用了不存在的测试用例：{test_path}::{name}")
    return errors


# --------------------------------------------------------------------------
# 7. 体量预算：常驻上下文与模块 AGENTS 不得无限增长
# --------------------------------------------------------------------------


INVARIANT_ROW = re.compile(r"^\|\s*(\d+)\s*\|", re.MULTILINE)
INVARIANT_CLAIM = re.compile(r"(\d+)\s*条不变量")


def check_invariants() -> tuple[list[str], int]:
    """ARCHITECTURE §6 的不变量表是硬资产；条数在别处被反复引用。

    这里同时校验两件事：编号从 1 连续，以及其它文档声称的「N 条」与表格一致。
    """
    arch = DOCS / "ARCHITECTURE.md"
    if not arch.exists():
        return [f"缺少 {rel(arch)}"], 0
    text = arch.read_text(encoding="utf-8")
    try:
        section = text.split("## 6.", 1)[1].split("## 7.", 1)[0]
    except IndexError:
        return ["ARCHITECTURE.md 缺少第 6 节不变量表"], 0

    numbers = [int(value) for value in INVARIANT_ROW.findall(section)]
    errors: list[str] = []
    if numbers != list(range(1, len(numbers) + 1)):
        errors.append(f"不变量编号不连续：共 {len(numbers)} 条（{numbers[:8]}…）")

    count = len(numbers)
    for path in collect_doc_files():
        if path.suffix not in {".md", ".txt"}:
            continue
        for match in INVARIANT_CLAIM.finditer(path.read_text(encoding="utf-8")):
            claimed = int(match.group(1))
            if claimed != count:
                errors.append(
                    f"{rel(path)} 声称 {claimed} 条不变量，"
                    f"但 ARCHITECTURE 第 6 节有 {count} 条"
                )
    return errors, count


def check_budgets(files: list[Path]) -> tuple[list[str], list[str]]:
    """返回 (硬超限, 软警告)。硬超限直接失败，软警告在 --strict 下失败。"""
    errors: list[str] = []
    warnings: list[str] = []
    for path in files:
        key = rel(path)
        size = len(path.read_text(encoding="utf-8"))
        limit = CONTEXT_BUDGETS.get(key)
        if key.startswith("src/") and key.endswith("/AGENTS.md"):
            limit = MODULE_AGENTS_BUDGET
        if limit is not None:
            if size > limit:
                errors.append(
                    f"常驻上下文超预算：{key} {size} > {limit} 字符"
                    "（把低频知识移到模块文档或改成指针）"
                )
        elif key.startswith("docs/") and size > DOC_SOFT_LIMIT:
            warnings.append(
                f"文档过长：{key} {size} > {DOC_SOFT_LIMIT} 字符（考虑拆分或改指针）"
            )
    return errors, warnings


# --------------------------------------------------------------------------


def main() -> int:
    strict = "--strict" in sys.argv
    json_mode = "--json" in sys.argv
    files = collect_doc_files()

    contract_errors = check_contracts()
    link_errors = check_links(files)
    acceptance_errors = check_acceptance_tests()
    budget_errors, budget_warnings = check_budgets(files)
    invariant_errors, invariant_count = check_invariants()
    missing, stale = check_owners(files)
    stale += budget_warnings
    command_errors = check_commands(files)

    errors = (
        contract_errors
        + link_errors
        + acceptance_errors
        + budget_errors
        + invariant_errors
        + missing
        + command_errors
    )
    problems = list(errors)
    if strict:
        problems += stale

    if json_mode:
        acceptance_path = DOCS / "acceptance" / "acceptance-criteria.yaml"
        acceptance_text = (
            acceptance_path.read_text(encoding="utf-8") if acceptance_path.exists() else ""
        )
        module_dirs = [
            p
            for p in (ROOT / "src" / "coding_agent").iterdir()
            if p.is_dir() and p.name != "__pycache__"
        ]
        covered = [p for p in module_dirs if (p / "AGENTS.md").exists()]
        largest_path, largest_chars = "", 0
        for path in files:
            size = len(path.read_text(encoding="utf-8"))
            if size > largest_chars:
                largest_path, largest_chars = rel(path), size
        metrics = {
            "ok": not problems,
            "documents": len(files),
            "module_agents": {
                "covered": len(covered),
                "total": len(module_dirs),
                "missing": sorted(p.name for p in module_dirs if p not in covered),
            },
            "contract_drift": len(contract_errors),
            "broken_links": len(link_errors),
            "missing_owner": len(missing),
            "stale": len(stale),
            "acceptance_anchors": {
                "files": len(set(re.findall(r"(tests/[\w./-]+\.py)", acceptance_text))),
                "named": len(
                    set(
                        re.findall(
                            r"(tests/[\w./-]+\.py)::([A-Za-z_]\w*)", acceptance_text
                        )
                    )
                ),
                "broken": len(acceptance_errors),
            },
            "budget": {"errors": len(budget_errors), "warnings": len(budget_warnings)},
            "invariants": {"count": invariant_count, "consistent": not invariant_errors},
            "command_hygiene": len(command_errors),
            "largest_document": {"path": largest_path, "chars": largest_chars},
            "errors": errors,
            "warnings": stale,
        }
        # ensure_ascii=True：Windows GBK 控制台也能安全输出；CI artifact 仍可读。
        print(json.dumps(metrics, ensure_ascii=True, indent=2))
        return 1 if problems else 0

    if problems:
        print(f"文档检查失败（{len(problems)} 项）：")
        for item in problems:
            print(f"  - {item}")
    if stale and not strict:
        print(f"\n过期警告（{len(stale)} 项，--strict 时算失败）：")
        for item in stale:
            print(f"  ! {item}")

    if problems:
        return 1
    print(f"文档检查通过（{len(files)} 份文档）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
