"""长期记忆：跨会话保留的项目事实与约定。

存放在**工作区内**的 `.agent/memory.md`，而不是宿主侧的用户目录 ——
事实是关于项目的（"这个仓库用 pytest"、"别动 legacy/"），换个工作区就该换一套。

刻意做成**显式增删**，不做 LLM 自动提炼：
自动记忆既容易积累噪声，又难以审计"这条结论是哪来的"。
原型阶段宁可少而准，需要时再讨论自动提炼。

文件是纯文本、每行一条，用户可以直接手工编辑。
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.snapshots import AGENT_STATE_DIRNAME
from coding_agent.security.secrets import secret_kinds

MEMORY_RELPATH = f"{AGENT_STATE_DIRNAME}/memory.md"
# 元数据侧车：来源、时间、过期。主文件保持纯文本一行一条，用户仍可手工编辑。
MEMORY_META_RELPATH = f"{AGENT_STATE_DIRNAME}/memory.meta.json"
MAX_FACTS = 50
MAX_FACT_CHARS = 300
# 两条事实的词语 Jaccard 相似度超过该值即视为同一件事的新旧版本。
CONFLICT_SIMILARITY = 0.55

HEADER = """\
# 项目记忆
#
# 由 agent 维护，也可以手工编辑：每行一条事实，以 "- " 开头。
# 内容会注入每次运行的上下文，请写短句，只记稳定的项目约定。
"""

PREFIX = "- "

# 英文/标识符按词切；中文没有空格，按「单字 + 相邻双字」切，足以做重叠打分。
_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")


class MemoryError(RuntimeError):
    """记忆文件读写失败。"""


def normalize_fact(text: str) -> str:
    """压成单行：多行事实会让文件格式与解析都变复杂。"""
    return " ".join(text.split())[:MAX_FACT_CHARS]


def _tokens(text: str) -> set[str]:
    """检索用的轻量词袋：英文小写词 + 中文单字/双字。"""
    lowered = text.lower()
    tokens = set(_WORD_RE.findall(lowered))
    for run in _CJK_RUN_RE.findall(lowered):
        tokens.update(run)
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _conflicts(new: str, old: str) -> bool:
    """两条事实是否在说同一件事的不同版本。

    只看词袋会把「不要动 legacy/」和「不要动 vendor/」判成冲突 ——
    两者都重要。因此当两边都含 ASCII 标识符、且标识符集合互不包含时，
    认为主语不同，不合并。中文事实没有这个信号，直接按相似度判断。
    """
    new_words = set(_WORD_RE.findall(new.lower()))
    old_words = set(_WORD_RE.findall(old.lower()))
    if new_words and old_words and not (
        new_words <= old_words or old_words <= new_words
    ):
        return False
    a, b = _tokens(new), _tokens(old)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= CONFLICT_SIMILARITY


class LongTermMemory:
    def __init__(self, fs: SandboxFs, root: str) -> None:
        self._fs = fs
        self.root = root
        # 最近一次 add 因冲突而被替换掉的旧事实（供 CLI/工具如实提示）
        self.last_replaced: list[str] = []

    @property
    def path(self) -> str:
        return posixpath.join(self.root, MEMORY_RELPATH)

    @property
    def meta_path(self) -> str:
        return posixpath.join(self.root, MEMORY_META_RELPATH)

    @contextmanager
    def _write_lock(self, timeout_s: float = 3.0) -> Iterator[None]:
        """跨进程写锁（沙箱内 `mkdir` 原子抢占）。

        记忆写入是「读全文 → 改一条 → 全量重写」，两个进程同时写会丢更新。
        锁要放在写入侧而不是读取侧：读取失败只会看到旧内容，不会破坏文件。

        拿不到锁时**仍然继续写**：记忆是辅助能力，不能因为一个残留锁把
        `/remember` 永久卡死。这个降级窗口很小，且写入仍是完整的。
        """
        sandbox = getattr(self._fs, "_sandbox", None)
        if sandbox is None:
            yield  # 测试用的假 fs：单进程，无竞争
            return
        lock_dir = posixpath.join(
            self.root, AGENT_STATE_DIRNAME, "memory.lock.d"
        )
        quoted = shlex.quote(lock_dir)
        parent = shlex.quote(posixpath.dirname(lock_dir))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            # 首次写入时 `.agent/` 可能还不存在；-p 只建父目录，锁本身仍是原子 mkdir
            if sandbox.run(
                f"mkdir -p {parent} 2>/dev/null && mkdir {quoted} 2>/dev/null"
            ).ok:
                break
            time.sleep(0.05)
        else:
            yield
            return
        try:
            yield
        finally:
            sandbox.run(f"rmdir {quoted} 2>/dev/null")

    # ------------------------------------------------------------------

    def _load_meta(self) -> dict[str, dict[str, object]]:
        try:
            raw = self._fs.read_text(self.meta_path, max_bytes=1_000_000)
        except (SandboxFsError, SandboxPathError):
            return {}
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}  # 手工改坏元数据不该让记忆整体不可用
        facts = parsed.get("facts") if isinstance(parsed, dict) else None
        return facts if isinstance(facts, dict) else {}

    def _save_meta(self, meta: dict[str, dict[str, object]]) -> None:
        content = json.dumps(
            {"version": 1, "facts": meta},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        try:
            self._fs.write_text(self.meta_path, content)
        except (SandboxFsError, SandboxPathError):
            # 元数据是增强信息：写失败不能把一次成功的记忆写入变成失败
            pass

    def load(self) -> list[str]:
        try:
            raw = self._fs.read_text(self.path, max_bytes=1_000_000)
        except (SandboxFsError, SandboxPathError):
            return []  # 文件不存在就是「还没有记忆」
        facts = self._parse(raw)
        meta = self._load_meta()
        now = _now_iso()
        alive: list[str] = []
        for fact in facts:
            expires = str((meta.get(fact) or {}).get("expires_at") or "")
            if expires and expires <= now:
                continue  # 已过期：不再注入，也不参与检索
            alive.append(fact)
        return alive

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

    def _save(
        self,
        facts: list[str],
        meta: dict[str, dict[str, object]] | None = None,
    ) -> None:
        body = "\n".join(f"{PREFIX}{fact}" for fact in facts)
        content = f"{HEADER}\n{body}\n" if body else f"{HEADER}\n"
        try:
            self._fs.write_text(self.path, content)
        except (SandboxFsError, SandboxPathError) as exc:
            raise MemoryError(f"记忆文件写入失败：{exc}") from exc
        if meta is not None:
            # 只保留仍然存在的事实，过期/删除的条目不会无限堆积
            self._save_meta({fact: meta[fact] for fact in facts if fact in meta})

    # ------------------------------------------------------------------

    def add(
        self,
        text: str,
        *,
        source: str = "user",
        ttl_days: int | None = None,
    ) -> list[str]:
        fact = normalize_fact(text)
        if not fact:
            raise MemoryError("记忆内容不能为空。")
        # 记忆会被注入后续每次运行的 system prompt；写入密钥等于主动外泄
        kinds = secret_kinds(fact)
        if kinds:
            raise MemoryError(
                f"记忆内容疑似包含密钥（{', '.join(kinds)}），已拒绝写入。"
                "请删除密钥后只记录不含敏感值的约定。"
            )
        self.last_replaced = []

        with self._write_lock():
            facts = self.load()
            if fact in facts:
                return facts  # 幂等：重复记同一条不产生第二行

            meta = self._load_meta()
            conflicts = [old for old in facts if _conflicts(fact, old)]
            for old in conflicts:
                facts.remove(old)
                meta.pop(old, None)
            self.last_replaced = conflicts

            if len(facts) >= MAX_FACTS:
                raise MemoryError(
                    f"记忆条数已达上限 {MAX_FACTS}。"
                    "请先删除不再适用的条目（/forget）。"
                )

            now = _now_iso()
            entry: dict[str, object] = {
                "source": source,
                "created_at": now,
                "updated_at": now,
            }
            if conflicts:
                entry["replaces"] = conflicts
            if ttl_days is not None and ttl_days > 0:
                entry["expires_at"] = (
                    datetime.now(UTC) + timedelta(days=ttl_days)
                ).isoformat(timespec="seconds")

            facts.append(fact)
            meta[fact] = entry
            self._save(facts, meta)
            return facts

    def remove(self, index: int) -> list[str]:
        """按 1 起的序号删除。"""
        with self._write_lock():
            facts = self.load()
            if index < 1 or index > len(facts):
                raise MemoryError(
                    f"序号超出范围：{index}（当前共 {len(facts)} 条）"
                )
            removed = facts.pop(index - 1)
            meta = self._load_meta()
            meta.pop(removed, None)
            self._save(facts, meta)
            return facts

    def clear(self) -> list[str]:
        with self._write_lock():
            self._save([], {})
        return []

    def search(self, query: str = "", limit: int = 8) -> list[str]:
        """按与 `query` 的词语重叠度取前 `limit` 条（同分保持原有顺序）。

        没有命中时回退到最早的前 `limit` 条 —— 记忆量有上限（50 条），
        全丢掉比给几条泛化约定更糟。`limit<=0` 表示不限制。
        """
        facts = self.load()
        if not facts:
            return []
        if limit <= 0 or len(facts) <= limit:
            return list(facts)

        query_tokens = _tokens(query)
        if not query_tokens:
            return facts[:limit]

        scored: list[tuple[int, int, str]] = []
        for index, fact in enumerate(facts):
            overlap = len(query_tokens & _tokens(fact))
            scored.append((-overlap, index, fact))
        scored.sort()

        hits = [fact for score, _, fact in scored if score < 0][:limit]
        return hits or facts[:limit]

    def render(self, query: str = "", limit: int = 8) -> str:
        """给系统提示用的文本；没有记忆时返回空串。

        传入 `query` 时只注入相关的前 `limit` 条，避免每条记忆都进每一轮
        system prompt —— 记忆条数有上限，但提示词的 token 成本会累积。
        """
        facts = self.search(query, limit) if query else self.search("", limit)
        if not facts:
            return ""
        lines = "\n".join(f"- {fact}" for fact in facts)
        return f"关于这个项目的长期记忆（跨会话保留）：\n{lines}"
