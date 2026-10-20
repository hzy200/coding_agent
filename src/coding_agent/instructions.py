"""分层项目指令：`AGENTS.md` / `CLAUDE.md`。

长期记忆（`.agent/memory.md`）记的是**事实与约定**；项目指令文件记的是
**执行规则**（怎么构建、哪些目录不要碰、提交格式）。两者来源不同、生命周期
不同，混在一个扁平文件里会越来越难维护。

发现策略：一次 `find` 扫工作区（限深度、排除重目录），按路径深度排序后
合并 —— 越靠近文件的指令越具体，放在后面，让模型最后读到。每个文件有
字符上限，总量也有上限，避免把上下文挤爆。
"""

from __future__ import annotations

import posixpath

from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.wsl_exec import WslSandbox

INSTRUCTION_FILENAMES = ("AGENTS.md", "CLAUDE.md")
MAX_FILE_CHARS = 8_000
MAX_TOTAL_CHARS = 24_000
MAX_FILES = 12
MAX_DEPTH = 4
EXCLUDED_DIRS = (".git", ".agent", ".venv", "venv", "node_modules", "__pycache__", "dist", "build")


def _find_command() -> str:
    names = " -o ".join(f"-name {name}" for name in INSTRUCTION_FILENAMES)
    excludes = " ".join(f"-not -path './{d}/*'" for d in EXCLUDED_DIRS)
    return (
        f"find . -maxdepth {MAX_DEPTH} -type f \\( {names} \\) "
        f"{excludes} -print"
    )


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（指令文件过长已截断，请直接读取原文件）"


def discover_instruction_paths(
    sandbox: WslSandbox, root: str
) -> list[str]:
    """返回工作区内的指令文件相对路径（深度浅的在前）。"""
    result = sandbox.run(_find_command(), cwd=root)
    if not result.ok:
        # find 不可用时至少检查根目录，不因为发现失败而全盘丢失
        fs = SandboxFs(sandbox, root)
        found: list[str] = []
        for name in INSTRUCTION_FILENAMES:
            try:
                if fs.stat(name).exists:
                    found.append(name)
            except (SandboxFsError, SandboxPathError):
                continue
        return found
    paths: list[str] = []
    for line in result.stdout.splitlines():
        rel = line.strip()
        if not rel or rel.startswith("./"):
            rel = rel[2:] if rel.startswith("./") else rel
        if not rel:
            continue
        name = posixpath.basename(rel)
        if name not in INSTRUCTION_FILENAMES:
            continue
        paths.append(rel)
    # 浅层指令先注入，深层/具体指令后注入（后者覆盖前者）
    paths.sort(key=lambda p: (p.count("/"), p))
    return paths[:MAX_FILES]


def load_project_instructions(
    sandbox: WslSandbox, root: str
) -> str:
    """读取并合并分层指令；没有文件时返回空串。"""
    fs = SandboxFs(sandbox, root)
    sections: list[str] = []
    total = 0
    for rel in discover_instruction_paths(sandbox, root):
        try:
            text = fs.read_text(rel, max_bytes=MAX_FILE_CHARS * 4)
        except (SandboxFsError, SandboxPathError):
            continue
        text = _truncate(text, MAX_FILE_CHARS)
        if not text:
            continue
        block = f"### {rel}\n{text}"
        if total + len(block) > MAX_TOTAL_CHARS:
            break
        sections.append(block)
        total += len(block)
    if not sections:
        return ""
    return (
        "项目指令（仓库内的 AGENTS.md / CLAUDE.md；越靠后的越具体，必须遵守）：\n"
        + "\n\n".join(sections)
    )
