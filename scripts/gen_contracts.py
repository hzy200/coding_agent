"""从权威源码生成 L0 可执行事实层的契约文档。

设计约束（都是为了能让 CI 用 `git diff --exit-code` 拦住漂移）：

1. **生成与 `--check` 只用标准库**：不 import `coding_agent`，因此不需要装
   langgraph/pydantic，任意 Python 3.11+ 都能跑。`--check-compat` 的 YAML 部分
   可选依赖 PyYAML；JSON 契约（events/config）不需要。
2. **输出确定性**：不写时间戳、不写 git commit、不做字典序以外的排序。
   否则每次生成都不一样，CI 的 diff 检查会永远红。
3. **事实源单一**：events.py / config.py / approve.py / audit/models.py 是唯一权威，
   `docs/contracts/` 下的文件全部由本脚本产出，禁止手改。

用法：
    python scripts/gen_contracts.py            # 生成
    python scripts/gen_contracts.py --check    # 只比对，不一致则退出码 1
    python scripts/gen_contracts.py --check-compat --base HEAD
                                               # 与基线比较破坏性变更
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "coding_agent"
OUT = ROOT / "docs" / "contracts"

GENERATED_BANNER = (
    "<!-- 由 scripts/gen_contracts.py 从源码生成，禁止手改。"
    "改代码后重跑生成，CI 会校验一致性。 -->"
)


# --------------------------------------------------------------------------
# 通用 ast 工具
# --------------------------------------------------------------------------


def parse(path: Path) -> tuple[ast.Module, list[str]]:
    text = path.read_text(encoding="utf-8")
    return ast.parse(text), text.splitlines()


def unparse(node: ast.AST | None) -> str:
    return ast.unparse(node) if node is not None else ""


def literal_str(node: ast.AST | None) -> str | None:
    """取出字符串字面量；不是字面量就返回 None（调用方决定如何兜底）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def literal_value(node: ast.AST | None) -> Any:
    """取出可 JSON 序列化的字面量；取不出则返回源码文本。"""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return unparse(node)


def comments_above(lines: list[str], lineno: int) -> str:
    """收集紧邻上方的连续 `#` 注释行（1-based lineno → 0-based 索引）。"""
    out: list[str] = []
    i = lineno - 2
    while i >= 0:
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            out.insert(0, stripped.lstrip("#").strip())
            i -= 1
        else:
            break
    return " ".join(out).strip()


def string_constants(tree: ast.Module) -> dict[str, str]:
    """收集模块级 `NAME = "literal"` 常量，用于跨模块解析工具名。"""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = literal_str(node.value)
            if value is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value = literal_str(node.value)
            if value is not None:
                out[node.target.id] = value
    return out


def frozenset_names(tree: ast.Module, varname: str) -> list[str]:
    """取出 `_READ_TOOLS = frozenset({...})` 里的 Name 元素。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if varname not in targets:
            continue
        call = node.value
        if isinstance(call, ast.Call):  # frozenset({...})
            args = call.args
        elif isinstance(call, ast.Set):  # 罕见：直接写集合
            args = list(call.elts)
        else:
            continue
        names: list[str] = []
        for arg in args:
            if isinstance(arg, ast.Set):
                names.extend(e.id for e in arg.elts if isinstance(e, ast.Name))
            elif isinstance(arg, ast.Name):
                names.append(arg.id)
        return names
    return []


# --------------------------------------------------------------------------
# 1. 事件契约
# --------------------------------------------------------------------------


def collect_events() -> list[dict[str, Any]]:
    tree, lines = parse(SRC / "events.py")
    events: list[dict[str, Any]] = []

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields: list[dict[str, Any]] = []
        event_type: str | None = None

        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            name = stmt.target.id
            annotation = unparse(stmt.annotation)
            if name == "type":
                # Literal["plan_created"] → 取出那个字面量
                ann = stmt.annotation
                if isinstance(ann, ast.Subscript) and isinstance(ann.slice, ast.Tuple):
                    for elt in ann.slice.elts:
                        found = literal_str(elt)
                        if found:
                            event_type = found
                            break
                elif isinstance(ann, ast.Subscript):
                    event_type = literal_str(ann.slice)
                continue
            fields.append(
                {
                    "name": name,
                    "type": annotation,
                    "default": literal_value(stmt.value),
                    "doc": comments_above(lines, stmt.lineno),
                }
            )

        if event_type is None:
            continue  # 基类 Event 没有 Literal type，跳过

        events.append(
            {
                "event_type": event_type,
                "class": node.name,
                "doc": (ast.get_docstring(node) or "").strip(),
                "fields": fields,
            }
        )

    events.sort(key=lambda e: e["event_type"])
    return events


def render_events(events: list[dict[str, Any]]) -> tuple[str, str]:
    payload = {
        "contract": "coding-agent/events",
        "version": 1,
        "source": "src/coding_agent/events.py",
        "note": (
            "前端唯一可消费的领域事件契约。前端只认识这里的事件，"
            "不认识 LangGraph，也拿不到 tools/ 与 sandbox/。"
        ),
        "events": events,
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    md: list[str] = [
        GENERATED_BANNER,
        "",
        "# 领域事件契约",
        "",
        "事实源：`src/coding_agent/events.py`（pydantic 模型）。",
        "机器可读版本：[`events.json`](events.json)。",
        "",
        "前端（CLI / TUI / Web）**只**消费这里的事件，不导入 `tools/` 与 `sandbox/`。",
        "这条边界由 `tests/unit/test_architecture.py` 用 AST 扫描守住。",
        "",
        f"共 **{len(events)}** 个事件类型。",
        "",
    ]
    for ev in events:
        md.append(f"## `{ev['event_type']}`")
        md.append("")
        md.append(f"类 `{ev['class']}`")
        if ev["doc"]:
            md.append("")
            md.append(ev["doc"])
        md.append("")
        if ev["fields"]:
            md.append("| 字段 | 类型 | 默认值 | 说明 |")
            md.append("|---|---|---|---|")
            for f in ev["fields"]:
                default = "" if f["default"] is None else f"`{json.dumps(f['default'], ensure_ascii=False)}`"
                md.append(
                    f"| `{f['name']}` | `{f['type']}` | {default} | {f['doc']} |"
                )
        else:
            md.append("（无额外字段）")
        md.append("")

    return json_text, "\n".join(md) + "\n"


# --------------------------------------------------------------------------
# 2. 工具等级契约
# --------------------------------------------------------------------------

LEVEL_LABELS = {
    0: "L0 只读",
    1: "L1 低风险写",
    2: "L2 变更性",
    3: "L3 危险",
}

LEVEL_BEHAVIOUR = {
    0: "自动执行",
    1: "需 --write 一次性授权后自动执行 + 审计",
    2: "需用户确认",
    3: "需用户确认（标注危险）；--yes 也不能放行",
}


def collect_tools() -> dict[str, Any]:
    tree, lines = parse(SRC / "graph" / "nodes" / "approve.py")

    # 跨模块解析工具名常量：扫 tools/ 下所有模块收集 NAME = "literal"
    consts: dict[str, str] = {}
    for path in sorted((SRC / "tools").glob("*.py")):
        consts.update(string_constants(parse(path)[0]))

    groups: dict[str, list[str]] = {}
    for var in ("_READ_TOOLS", "_WRITE_TOOLS", "_MUTATE_TOOLS"):
        groups[var] = [consts.get(n, n) for n in frozenset_names(tree, var)]

    # 每个工具的注释说明（frozenset 内每个 Name 上方的注释）
    notes: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in consts:
            doc = comments_above(lines, node.lineno)
            if doc and not notes.get(consts[node.id]):
                notes[consts[node.id]] = doc

    tools: list[dict[str, Any]] = []
    for level, var in ((0, "_READ_TOOLS"), (1, "_WRITE_TOOLS"), (2, "_MUTATE_TOOLS")):
        for name in sorted(groups[var]):
            tools.append(
                {
                    "tool": name,
                    "level": level,
                    "label": LEVEL_LABELS[level],
                    "behaviour": LEVEL_BEHAVIOUR[level],
                    "note": notes.get(name, ""),
                }
            )

    tools.sort(key=lambda t: (t["level"], t["tool"]))
    return {
        "contract": "coding-agent/tools",
        "version": 1,
        "source": "src/coding_agent/graph/nodes/approve.py",
        "rule": (
            "新工具必须在 approve.py::tool_level 登记；"
            "未登记一律按 L3 危险处理（fail-closed，不是默认值偷懒）。"
        ),
        "shell": {
            "tool": "shell_exec",
            "level": "按命令文本分类（policy.classify）",
            "note": "复合命令按 && || ; | 分段取最高级；无法解析的一律降级为 L2。",
        },
        "levels": [
            {"level": i, "label": LEVEL_LABELS[i], "behaviour": LEVEL_BEHAVIOUR[i]}
            for i in range(4)
        ],
        "tools": tools,
    }


def yaml_str(value: str) -> str:
    """最小可用的 YAML 双引号标量转义。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_tools(payload: dict[str, Any]) -> str:
    out: list[str] = [
        "# 由 scripts/gen_contracts.py 从源码生成，禁止手改。",
        f"# 事实源：{payload['source']}",
        "",
        f"contract: {payload['contract']}",
        f"version: {payload['version']}",
        f"rule: {yaml_str(payload['rule'])}",
        "",
        "# 命令四级分级",
        "levels:",
    ]
    for lv in payload["levels"]:
        out.append(f"  - level: {lv['level']}")
        out.append(f"    label: {yaml_str(lv['label'])}")
        out.append(f"    behaviour: {yaml_str(lv['behaviour'])}")
    out.append("")
    out.append("# shell_exec 不参与静态分级，按命令文本实时分类")
    out.append("shell:")
    out.append(f"  tool: {payload['shell']['tool']}")
    out.append(f"  level: {yaml_str(payload['shell']['level'])}")
    out.append(f"  note: {yaml_str(payload['shell']['note'])}")
    out.append("")
    out.append("tools:")
    for t in payload["tools"]:
        out.append(f"  - tool: {t['tool']}")
        out.append(f"    level: {t['level']}")
        out.append(f"    label: {yaml_str(t['label'])}")
        out.append(f"    behaviour: {yaml_str(t['behaviour'])}")
        if t["note"]:
            out.append(f"    note: {yaml_str(t['note'])}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# 3. 审计契约
# --------------------------------------------------------------------------


def collect_audit() -> dict[str, Any]:
    tree, lines = parse(SRC / "audit" / "models.py")
    consts = string_constants(tree)

    kinds, decisions = [], []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = literal_str(node.value)
        if value is None:
            continue
        name = node.targets[0].id if isinstance(node.targets[0], ast.Name) else ""
        doc = comments_above(lines, node.lineno)
        # 跳过 `# kind 取值` / `# decision 取值` 这类分节标题 —— 它不是字段说明
        if doc.endswith("取值"):
            doc = ""
        if name.startswith("DECISION_"):
            decisions.append({"const": name, "value": value, "doc": doc})
        elif name.isupper() and not name.startswith("_"):
            kinds.append({"const": name, "kind": value, "doc": doc})

    # AuditRecord 字段，按 `# ---- group ----` 分组
    groups: list[dict[str, Any]] = []
    current = "common"
    bucket: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "AuditRecord":
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                text = str(stmt.value.value).strip()
                if text.startswith("----") and text.endswith("----"):
                    groups.append({"group": current, "fields": bucket})
                    current = text.strip("-").strip()
                    bucket = []
                continue
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            bucket.append(
                {
                    "name": stmt.target.id,
                    "type": unparse(stmt.annotation),
                    "default": literal_value(stmt.value),
                    "doc": comments_above(lines, stmt.lineno),
                }
            )
    groups.append({"group": current, "fields": bucket})

    return {
        "contract": "coding-agent/audit",
        "version": 1,
        "source": "src/coding_agent/audit/models.py",
        "rule": (
            "审计由 runtime 写，不由前端写 —— 任何界面都绕不过；"
            "写入失败会转成 RunFailed（宁可显式失败，也不静默丢记录）。"
        ),
        "storage": "宿主侧 <cwd>/.agent/audit/<YYYY-MM-DD>.jsonl，哈希链可校验",
        "kinds": kinds,
        "decisions": decisions,
        "field_groups": [g for g in groups if g["fields"]],
    }


def render_audit(payload: dict[str, Any]) -> str:
    out: list[str] = [
        "# 由 scripts/gen_contracts.py 从源码生成，禁止手改。",
        f"# 事实源：{payload['source']}",
        "",
        f"contract: {payload['contract']}",
        f"version: {payload['version']}",
        f"storage: {yaml_str(payload['storage'])}",
        f"rule: {yaml_str(payload['rule'])}",
        "",
        "kinds:",
    ]
    for k in payload["kinds"]:
        out.append(f"  - kind: {k['kind']}")
        if k["doc"]:
            out.append(f"    doc: {yaml_str(k['doc'])}")
    out.append("")
    out.append("decisions:")
    for d in payload["decisions"]:
        out.append(f"  - value: {d['value']}")
        if d["doc"]:
            out.append(f"    doc: {yaml_str(d['doc'])}")
    out.append("")
    out.append("field_groups:")
    for g in payload["field_groups"]:
        out.append(f"  - group: {yaml_str(g['group'])}")
        out.append("    fields:")
        for f in g["fields"]:
            out.append(f"      - name: {f['name']}")
            out.append(f"        type: {yaml_str(f['type'])}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# 4. 配置契约（消灭"44 个变量零文档"）
# --------------------------------------------------------------------------


def collect_config() -> tuple[list[dict[str, Any]], str]:
    tree, lines = parse(SRC / "config.py")
    settings = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings"
    )
    fields: list[dict[str, Any]] = []
    section = "通用"

    for stmt in settings.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            text = str(stmt.value.value).strip()
            if text.startswith("----------") and text.endswith("----------"):
                section = text.strip("-").strip()
            continue
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue

        name = stmt.target.id
        alias: str | None = None
        default: Any = None
        call = stmt.value
        if isinstance(call, ast.Call):
            for kw in call.keywords:
                if kw.arg == "alias":
                    alias = literal_str(kw.value)
                elif kw.arg == "default":
                    default = literal_value(kw.value)
        else:
            default = literal_value(call)

        if alias is None:
            alias = f"AGENT_{name.upper()}"

        fields.append(
            {
                "name": name,
                "env": alias,
                "type": unparse(stmt.annotation),
                "default": default,
                "section": section,
                "doc": comments_above(lines, stmt.lineno),
            }
        )

    fields.sort(key=lambda f: f["env"])
    return fields, (ast.get_docstring(tree) or "").strip()


def render_config(fields: list[dict[str, Any]], module_doc: str) -> tuple[str, str]:
    payload = {
        "contract": "coding-agent/config",
        "version": 1,
        "source": "src/coding_agent/config.py",
        "priority": ["构造函数参数", ".env", "环境变量", "默认值"],
        "priority_note": (
            "刻意偏离「环境变量 > .env」的通用约定：某些工具链会在进程启动时注入"
            "用户看不见也删不掉的陈旧值，导致改 .env 不生效。项目目录里的 .env "
            "是显式写下、看得见、可编辑的意图，因此让它优先。"
        ),
        "count": len(fields),
        "env_vars": sorted(f["env"] for f in fields),
        "fields": fields,
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    md: list[str] = [
        GENERATED_BANNER,
        "",
        "# 配置契约",
        "",
        "事实源：`src/coding_agent/config.py`。**这份是全量**，README 的配置表只是摘录。",
        "机器可读版本（可直接用来校验有没有捏造配置项）：[`config.json`](config.json)。",
        "",
        "优先级：" + " **>** ".join(payload["priority"]) + "。",
        "",
        f"> {payload['priority_note']}",
        "",
        f"共 **{len(fields)}** 个配置项。",
        "",
    ]

    by_section: dict[str, list[dict[str, Any]]] = {}
    for f in fields:
        by_section.setdefault(f["section"], []).append(f)

    for section in sorted(by_section):
        md.append(f"## {section}")
        md.append("")
        md.append("| 环境变量 | 字段 | 类型 | 默认值 | 说明 |")
        md.append("|---|---|---|---|---|")
        for f in by_section[section]:
            default = "" if f["default"] is None else json.dumps(f["default"], ensure_ascii=False)
            default = default.replace("|", "\\|")
            doc = f["doc"].replace("|", "\\|").replace("\n", " ")
            md.append(f"| `{f['env']}` | `{f['name']}` | `{f['type']}` | `{default}` | {doc} |")
        md.append("")

    return json_text, "\n".join(md) + "\n"


# --------------------------------------------------------------------------
# 5. 依赖契约
# --------------------------------------------------------------------------


def render_dependencies() -> str:
    """从 pyproject.toml 抽取声明的依赖范围（实测版本属环境快照，不进生成物）。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lines = text.splitlines()

    def grab(header: str) -> list[str]:
        out: list[str] = []
        inside = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(header):
                inside = True
                continue
            if inside:
                if stripped.startswith("]"):
                    break
                if stripped.startswith('"'):
                    out.append(stripped.strip('",'))
        return out

    runtime = grab("dependencies = [")
    groups = {"dev": grab("dev = ["), "ui": grab("ui = ["), "web": grab("web = [")}

    md: list[str] = [
        GENERATED_BANNER,
        "",
        "# 依赖契约",
        "",
        "事实源：`pyproject.toml`（声明范围）+ `uv.lock`（精确锁定）。",
        "",
        "> **实测版本快照不在这里**：那是环境属性，会随机器变化。",
        "> 精确复现请跑 `pip freeze > requirements.lock.txt`，见 `ENVIRONMENT.md`。",
        "",
        "## 运行时依赖（必装）",
        "",
    ]
    md += [f"- `{d}`" for d in runtime]
    md.append("")
    md.append("## 可选 extras")
    md.append("")
    md.append("| extra | 装了它才能用 | 依赖 |")
    md.append("|---|---|---|")
    purpose = {
        "dev": "pytest / ruff / pytest-xdist / pytest-cov —— 跑测试与 lint",
        "ui": "`agent tui`（Textual）",
        "web": "`agent web`（FastAPI + uvicorn）",
    }
    for extra, deps in groups.items():
        md.append(f"| `{extra}` | {purpose.get(extra, '')} | {', '.join(f'`{d}`' for d in deps)} |")
    md.append("")
    return "\n".join(md) + "\n"


# --------------------------------------------------------------------------
# 6. 索引
# --------------------------------------------------------------------------


def render_index(events: list[dict[str, Any]], fields: list[dict[str, Any]], tools: dict[str, Any]) -> str:
    return "\n".join(
        [
            GENERATED_BANNER,
            "",
            "# L0 可执行事实层 · 契约索引",
            "",
            "这一层的共同特征：**能编译、能跑、能校验**。它们不是文档，",
            "但它们是 AI 最可靠的文档。全部由源码生成，禁止手改。",
            "",
            "| 契约 | 事实源 | 产出 | 校验方式 |",
            "|---|---|---|---|",
            "| 领域事件 | `events.py` | [`events.json`](events.json) / [`events.md`](events.md) | pydantic 模型 + 前端 AST 扫描 |",
            "| 工具等级 | `approve.py::tool_level` | [`tools.yaml`](tools.yaml) | 未登记工具按 L3 fail-closed |",
            "| 审计记录 | `audit/models.py` | [`audit.yaml`](audit.yaml) | JSONL + 哈希链 `agent audit --verify` |",
            "| 配置项 | `config.py` | [`config.json`](config.json) / [`config.md`](config.md) | `config.json` 可校验配置项是否存在 |",
            "| 依赖 | `pyproject.toml` + `uv.lock` | [`dependencies.md`](dependencies.md) | `pip install -e .` |",
            "| 测试套件 | `tests/` | 见 `ACCEPTANCE.md` 第 1 节 | `pytest -m \"not llm\"` |",
            "| 流水线 | `.github/workflows/` | [`../CI.md`](../CI.md) | GitHub Actions |",
            "",
            f"- 事件类型：**{len(events)}** 个",
            f"- 配置项：**{len(fields)}** 个",
            f"- 静态分级工具：**{len(tools['tools'])}** 个（另有 `shell_exec` 按命令文本实时分类）",
            "",
            "重跑生成：`python scripts/gen_contracts.py`。",
            "CI 校验：`python scripts/gen_contracts.py --check`。",
            "",
        ]
    )


# --------------------------------------------------------------------------
# 7. 兼容性检查（相对某个 git 基线）
# --------------------------------------------------------------------------

COMPAT_CONTRACTS = ("events.json", "config.json", "tools.yaml", "audit.yaml")


def _git_show(base: str, name: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{base}:docs/contracts/{name}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout if proc.returncode == 0 else None


def _load_contract(name: str, text: str) -> dict[str, Any] | None:
    try:
        if name.endswith(".json"):
            return json.loads(text)
        import yaml  # 只有兼容性检查需要；生成与 --check 仍保持零依赖
    except (ImportError, json.JSONDecodeError):
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _diff_events(old: dict, new: dict) -> list[str]:
    problems: list[str] = []
    old_events = {item["event_type"]: item for item in old.get("events", [])}
    new_events = {item["event_type"]: item for item in new.get("events", [])}
    for name in sorted(old_events.keys() - new_events.keys()):
        problems.append(f"events.json: 删除事件类型 `{name}`")
    for name in sorted(old_events.keys() & new_events.keys()):
        old_fields = {f["name"]: f for f in old_events[name].get("fields", [])}
        new_fields = {f["name"]: f for f in new_events[name].get("fields", [])}
        for field in sorted(old_fields.keys() - new_fields.keys()):
            problems.append(f"events.json: `{name}` 删除字段 `{field}`")
        for field in sorted(old_fields.keys() & new_fields.keys()):
            before = old_fields[field].get("type")
            after = new_fields[field].get("type")
            if before != after:
                problems.append(
                    f"events.json: `{name}.{field}` 类型 `{before}` -> `{after}`"
                )
    return problems


def _diff_config(old: dict, new: dict) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []

    old_envs = set(old.get("env_vars", []))
    new_envs = set(new.get("env_vars", []))
    for env in sorted(old_envs - new_envs):
        problems.append(f"config.json: 删除配置项 `{env}`")

    old_fields = {f["env"]: f for f in old.get("fields", []) if f.get("env")}
    new_fields = {f["env"]: f for f in new.get("fields", []) if f.get("env")}
    for env in sorted(old_fields.keys() & new_fields.keys()):
        before = old_fields[env].get("type")
        after = new_fields[env].get("type")
        if before != after:
            problems.append(f"config.json: `{env}` 类型 `{before}` -> `{after}`")
        if old_fields[env].get("default") != new_fields[env].get("default"):
            warnings.append(f"config.json: `{env}` 默认值发生变化")
    if old.get("priority") != new.get("priority"):
        warnings.append("config.json: 配置优先级顺序发生变化")
    return problems, warnings


def _diff_tools(old: dict, new: dict) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    old_tools = {item["tool"]: item for item in old.get("tools", [])}
    new_tools = {item["tool"]: item for item in new.get("tools", [])}
    for name in sorted(old_tools.keys() - new_tools.keys()):
        problems.append(f"tools.yaml: 删除工具 `{name}`")
    for name in sorted(old_tools.keys() & new_tools.keys()):
        before = old_tools[name].get("level")
        after = new_tools[name].get("level")
        if before == after:
            continue
        if isinstance(before, int) and isinstance(after, int):
            if after < before:
                problems.append(
                    f"tools.yaml: `{name}` 等级降低 L{before} -> L{after}（更宽松）"
                )
            else:
                warnings.append(
                    f"tools.yaml: `{name}` 等级升高 L{before} -> L{after}"
                    "（更严格，可能影响自动化）"
                )
        else:
            warnings.append(f"tools.yaml: `{name}` 等级 `{before}` -> `{after}`")
    return problems, warnings


def _diff_audit(old: dict, new: dict) -> list[str]:
    problems: list[str] = []

    old_kinds = {item["kind"] for item in old.get("kinds", [])}
    new_kinds = {item["kind"] for item in new.get("kinds", [])}
    for kind in sorted(old_kinds - new_kinds):
        problems.append(f"audit.yaml: 删除记录类型 `{kind}`")

    old_decisions = {item["value"] for item in old.get("decisions", [])}
    new_decisions = {item["value"] for item in new.get("decisions", [])}
    for value in sorted(old_decisions - new_decisions):
        problems.append(f"audit.yaml: 删除审批决定 `{value}`")

    old_groups = {
        group["group"]: {field["name"] for field in group.get("fields", [])}
        for group in old.get("field_groups", [])
    }
    new_groups = {
        group["group"]: {field["name"] for field in group.get("fields", [])}
        for group in new.get("field_groups", [])
    }
    for group in sorted(old_groups.keys() - new_groups.keys()):
        problems.append(f"audit.yaml: 删除字段组 `{group}`")
    for group in sorted(old_groups.keys() & new_groups.keys()):
        for field in sorted(old_groups[group] - new_groups[group]):
            problems.append(f"audit.yaml: `{group}` 删除字段 `{field}`")
    return problems


def check_compat(base: str, allow_breaking: bool) -> int:
    files = build()
    breaking: list[str] = []
    warnings: list[str] = []
    skipped: list[str] = []

    for name in COMPAT_CONTRACTS:
        old_text = _git_show(base, name)
        if old_text is None:
            skipped.append(name)
            continue
        old = _load_contract(name, old_text)
        new = _load_contract(name, files[name])
        if old is None or new is None:
            skipped.append(name)
            continue
        if name == "events.json":
            breaking += _diff_events(old, new)
        elif name == "config.json":
            problems, notes = _diff_config(old, new)
            breaking += problems
            warnings += notes
        elif name == "tools.yaml":
            problems, notes = _diff_tools(old, new)
            breaking += problems
            warnings += notes
        elif name == "audit.yaml":
            breaking += _diff_audit(old, new)

    if skipped:
        print(f"兼容性基线不可用，已跳过：{', '.join(skipped)}")
        if any(name.endswith(".yaml") for name in skipped):
            print("  （YAML 检查需要 PyYAML：pip install pyyaml）")

    if warnings:
        print("兼容性提醒（非破坏性）：")
        for item in warnings:
            print(f"  ! {item}")

    if breaking:
        print(f"检测到 {len(breaking)} 项破坏性契约变更：")
        for item in breaking:
            print(f"  - {item}")
        if allow_breaking:
            print("已指定 --allow-breaking：仅报告，不失败。")
            return 0
        print("确认是有意变更后，用 --allow-breaking 放行；否则请恢复兼容性。")
        return 1

    print(f"契约兼容性检查通过（基线 {base}）。")
    return 0


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def build() -> dict[str, str]:
    events = collect_events()
    events_json, events_md = render_events(events)
    tools = collect_tools()
    audit = collect_audit()
    fields, _ = collect_config()
    config_json, config_md = render_config(fields, "")

    return {
        "index.md": render_index(events, fields, tools),
        "events.json": events_json,
        "events.md": events_md,
        "tools.yaml": render_tools(tools),
        "audit.yaml": render_audit(audit),
        "config.json": config_json,
        "config.md": config_md,
        "dependencies.md": render_dependencies(),
    }


def main() -> int:
    if "--check-compat" in sys.argv:
        base = "HEAD"
        if "--base" in sys.argv:
            index = sys.argv.index("--base")
            if index + 1 < len(sys.argv):
                base = sys.argv[index + 1]
        return check_compat(base, "--allow-breaking" in sys.argv)

    check_only = "--check" in sys.argv
    files = build()

    if check_only:
        drift = []
        for name, text in files.items():
            path = OUT / name
            if not path.exists():
                drift.append(f"{name}（缺失）")
            elif path.read_text(encoding="utf-8") != text:
                drift.append(name)
        if drift:
            print("契约与源码不一致，请重跑 `python scripts/gen_contracts.py`：")
            for name in drift:
                print(f"  - docs/contracts/{name}")
            return 1
        print(f"契约一致（{len(files)} 个文件）")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="utf-8")
        print(f"生成 docs/contracts/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
