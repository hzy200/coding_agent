"""长期记忆：跨会话保留的项目事实与约定。

存放在**工作区内**的 `.agent/memory.md`，而不是宿主侧的用户目录 ——
事实是关于项目的（"这个仓库用 pytest"、"别动 legacy/"），换个工作区就该换一套。

刻意做成**显式增删**，不做 LLM 自动提炼：
自动记忆既容易积累噪声，又难以审计"这条结论是哪来的"。
原型阶段宁可少而准，需要时再讨论自动提炼。

文件是纯文本、每行一条，用户可以直接手工编辑。
"""

from __future__ import annotations

import posixpath

from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.snapshots import AGENT_STATE_DIRNAME

MEMORY_RELPATH = f"{AGENT_STATE_DIRNAME}/memory.md"
MAX_FACTS = 50
MAX_FACT_CHARS = 300

HEADER = """\
# 项目记忆
#
# 由 agent 维护，也可以手工编辑：每行一条事实，以 "- " 开头。
# 内容会注入每次运行的上下文，请写短句，只记稳定的项目约定。
"""

PREFIX = "- "


class MemoryError(RuntimeError):
    """记忆文件读写失败。"""


def normalize_fact(text: str) -> str:
    """压成单行：多行事实会让文件格式与解析都变复杂。"""
    return " ".join(text.split())[:MAX_FACT_CHARS]


class LongTermMemory:
    def __init__(self, fs: SandboxFs, root: str) -> None:
        self._fs = fs
        self.root = root

    @property
    def path(self) -> str:
        return posixpath.join(self.root, MEMORY_RELPATH)

    # ------------------------------------------------------------------

    def load(self) -> list[str]:
        try:
            raw = self._fs.read_text(self.path, max_bytes=1_000_000)
        except (SandboxFsError, SandboxPathError):
            return []  # 文件不存在就是「还没有记忆」
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> list[str]:
        facts: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(PREFIX):
                fact = stripped[len(PREFIX) :].strip()
                if fact:
                    facts.append(fact)
        return facts

    def _save(self, facts: list[str]) -> None:
        body = "\n".join(f"{PREFIX}{fact}" for fact in facts)
        content = f"{HEADER}\n{body}\n" if body else f"{HEADER}\n"
        try:
            self._fs.write_text(self.path, content)
        except (SandboxFsError, SandboxPathError) as exc:
            raise MemoryError(f"记忆文件写入失败：{exc}") from exc

    # ------------------------------------------------------------------

    def add(self, text: str) -> list[str]:
        fact = normalize_fact(text)
        if not fact:
            raise MemoryError("记忆内容不能为空。")

        facts = self.load()
        if fact in facts:
            return facts  # 幂等：重复记同一条不产生第二行
        if len(facts) >= MAX_FACTS:
            raise MemoryError(
                f"记忆条数已达上限 {MAX_FACTS}。请先删除不再适用的条目（/forget）。"
            )

        facts.append(fact)
        self._save(facts)
        return facts

    def remove(self, index: int) -> list[str]:
        """按 1 起的序号删除。"""
        facts = self.load()
        if index < 1 or index > len(facts):
            raise MemoryError(f"序号超出范围：{index}（当前共 {len(facts)} 条）")
        facts.pop(index - 1)
        self._save(facts)
        return facts

    def clear(self) -> list[str]:
        self._save([])
        return []

    def render(self) -> str:
        """给系统提示用的文本；没有记忆时返回空串。"""
        facts = self.load()
        if not facts:
            return ""
        lines = "\n".join(f"- {fact}" for fact in facts)
        return f"关于这个项目的长期记忆（跨会话保留）：\n{lines}"
