"""长期记忆的按需检索，以及暴露给模型的 memory 工具。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coding_agent.config import Settings
from coding_agent.memory.longterm import LongTermMemory


def test_memory_state_root_can_be_outside_the_workspace() -> None:
    """不可信仓库下记忆放在仓库之外，路径必须使用 state_root。"""
    memory = LongTermMemory(
        object(), "/repo", state_root="/home/tester/agent-memory"
    )  # type: ignore[arg-type]

    assert memory.path == "/home/tester/agent-memory/.agent/memory.md"
    assert memory.meta_path == "/home/tester/agent-memory/.agent/memory.meta.json"


class _FakeFs:
    """只实现 LongTermMemory 用到的读写接口。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def read_text(self, path: str, *, max_bytes: int) -> str:
        if path not in self.files:
            from coding_agent.sandbox.fs import SandboxFsError

            raise SandboxFsError("not found")
        return self.files[path]

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content


def _memory(*facts: str) -> LongTermMemory:
    fs = _FakeFs()
    memory = LongTermMemory(fs, "/work")  # type: ignore[arg-type]
    for fact in facts:
        memory.add(fact)
    return memory


FACTS = (
    "这个仓库用 pytest 跑测试",
    "不要动 legacy/ 目录",
    "依赖用 uv 管理，不要用 pip",
    "提交信息用中文",
    "验证命令是 make check",
)


# ---------------- 检索 ----------------

def test_search_ranks_by_overlap() -> None:
    memory = _memory(*FACTS)
    hits = memory.search("测试怎么跑，pytest？", limit=2)
    assert hits[0] == "这个仓库用 pytest 跑测试"


def test_search_matches_chinese_bigrams() -> None:
    """中文没有空格，靠单字/双字重叠命中。"""
    memory = _memory(*FACTS)
    hits = memory.search("依赖管理方式", limit=1)
    assert hits == ["依赖用 uv 管理，不要用 pip"]


def test_search_falls_back_to_oldest_when_nothing_matches() -> None:
    memory = _memory(*FACTS)
    hits = memory.search("完全无关的词汇 zzzz", limit=2)
    assert hits == list(FACTS[:2])


def test_search_without_query_keeps_order() -> None:
    memory = _memory(*FACTS)
    assert memory.search("", limit=3) == list(FACTS[:3])


def test_search_returns_everything_when_under_limit() -> None:
    memory = _memory(*FACTS)
    assert memory.search("pytest", limit=50) == list(FACTS)


def test_search_on_empty_memory() -> None:
    assert _memory().search("pytest") == []


def test_add_rejects_secrets() -> None:
    """记忆会进入每次运行的 system prompt，密钥不能写进去。"""
    from coding_agent.memory.longterm import MemoryError

    memory = _memory()
    with pytest.raises(MemoryError, match="密钥"):
        memory.add("api_key=sk-abcdefghijklmnopqrstuvwxyz")
    assert memory.load() == []


def test_add_records_provenance_and_ttl() -> None:
    memory = _memory()
    memory.add("用 pytest", source="model", ttl_days=7)
    meta = memory._load_meta()  # noqa: SLF001 - 元数据是本次改动的一部分
    entry = meta["用 pytest"]
    assert entry["source"] == "model"
    assert entry["created_at"]
    assert entry["expires_at"]


def test_add_replaces_conflicting_fact() -> None:
    memory = _memory()
    memory.add("提交信息使用中文")
    memory.add("提交信息使用英文")
    assert memory.load() == ["提交信息使用英文"]
    assert memory.last_replaced == ["提交信息使用中文"]


def test_render_only_includes_selected_facts() -> None:
    memory = _memory(*FACTS)
    rendered = memory.render("legacy 目录", limit=1)
    assert "legacy" in rendered
    assert "pytest" not in rendered


# ---------------- memory 工具 ----------------

@pytest.fixture
def memory_tools(monkeypatch):
    from coding_agent.tools import memory as memory_tools

    fs = _FakeFs()
    monkeypatch.setattr(
        memory_tools, "SandboxFs", lambda sandbox, root, cache=None: fs
    )
    settings = Settings(
        _env_file=None, deepseek_api_key="x", wsl_workspace="/work"
    )
    sandbox = SimpleNamespace()
    return (
        memory_tools.build_memory_tools(settings, sandbox, allow_write=True),  # type: ignore[arg-type]
        fs,
    )


def test_memory_search_tool_reads_file(memory_tools) -> None:
    tools, fs = memory_tools
    fs.files["/work/.agent/memory.md"] = (
        "# 项目记忆\n- 这个仓库用 pytest 跑测试\n- 依赖用 uv\n"
    )
    search = {tool.name: tool for tool in tools}["memory_search"]
    text = search.invoke({"query": "pytest", "limit": 5, "reason": "查约定"})
    assert "pytest" in text


def test_memory_save_tool_appends_fact(memory_tools) -> None:
    tools, fs = memory_tools
    save = {tool.name: tool for tool in tools}["memory_save"]
    text = save.invoke({"fact": "别动 legacy/", "reason": "用户强调"})
    assert "已记住" in text
    assert "别动 legacy/" in fs.files["/work/.agent/memory.md"]


def test_memory_save_is_not_exposed_without_write(monkeypatch) -> None:
    from coding_agent.tools import memory as memory_tools

    monkeypatch.setattr(
        memory_tools, "SandboxFs", lambda sandbox, root, cache=None: _FakeFs()
    )
    settings = Settings(
        _env_file=None, deepseek_api_key="x", wsl_workspace="/work"
    )
    tools = memory_tools.build_memory_tools(
        settings, SimpleNamespace(), allow_write=False  # type: ignore[arg-type]
    )
    assert [tool.name for tool in tools] == ["memory_search"]
