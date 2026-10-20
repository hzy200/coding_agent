"""上下文压缩与长期记忆的确定性基准。

不调用模型、不依赖 WSL，因此可以进 CI，也适合做 A/B 的固定标尺。

回答两个问题：

1. **长会话压缩后，任务关键信息还在不在？**
   把「目标 / 约束 / 最近结论 / 最近改过的文件」混进多轮工具历史，按预算裁剪，
   检查这些信息是否仍出现在「摘要 + 裁剪后历史」里，同时报告压缩率。
2. **记忆检索准不准？**
   用一组事实与查询计算 recall@K、无关查询的回退行为，
   并验证冲突替换与过期过滤。

用法：

    python tests/eval/context_memory_bench.py
    python tests/eval/context_memory_bench.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    ToolMessage,
)

from coding_agent.llm.context import (  # noqa: E402
    ContextBudget,
    summarize_messages,
    total_chars,
    total_tokens,
    trim_messages,
)
from coding_agent.memory.longterm import LongTermMemory  # noqa: E402

GOAL = "修复 discount 计算，并保持旧接口兼容"
CONSTRAINT = "不要修改 legacy/ 目录"
CONCLUSION = "最近结论：discount.py 的边界已改，还差一个零值测试"


def _long_history(pairs: int = 30, size: int = 4_000) -> list[Any]:
    messages: list[Any] = [
        HumanMessage(content=f"目标：{GOAL}\n约束：{CONSTRAINT}")
    ]
    for index in range(pairs):
        call_id = f"call-{index}"
        messages.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "file_read",
                        "args": {"path": f"src/module_{index}.py"},
                        "id": call_id,
                    }
                ],
            )
        )
        messages.append(
            ToolMessage(
                content=f"# module_{index}\n" + "x" * size,
                tool_call_id=call_id,
                name="file_read",
            )
        )
    messages.append(AIMessage(content=CONCLUSION))
    return messages


def _visible_text(messages: list[Any], summary: str) -> str:
    parts = [summary]
    for message in messages:
        content = message.content
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict)
            )
    return "\n".join(parts)


def trim_benchmark() -> dict[str, Any]:
    messages = _long_history()
    budget = ContextBudget(max_chars=6_000, max_tokens=2_000, keep_recent=6, tool_chars=400)
    summary = summarize_messages(messages)
    trimmed, report = trim_messages(messages, budget)
    visible = _visible_text(trimmed, summary)

    must_keep = {
        "goal": GOAL,
        "constraint": CONSTRAINT,
        "recent_conclusion": CONCLUSION,
        "recent_file": "src/module_29.py",
    }
    kept = [name for name, text in must_keep.items() if text in visible]
    missing = [name for name in must_keep if name not in kept]

    before = total_chars(messages)
    after = total_chars(trimmed)
    return {
        "messages": len(messages),
        "before_chars": before,
        "after_chars": after,
        "before_tokens": total_tokens(messages),
        "after_tokens": total_tokens(trimmed),
        "compression": round(1 - after / before, 4) if before else 0.0,
        "trimmed_messages": report.trimmed,
        "escalated": report.escalated,
        "retention": round(len(kept) / len(must_keep), 4),
        "kept": kept,
        "missing": missing,
        "under_token_budget": total_tokens(trimmed) <= budget.max_tokens,
    }


# ---------------------------------------------------------------------------
# 记忆检索
# ---------------------------------------------------------------------------

_MEMORY_FACTS = [
    ("这个仓库用 pytest -q 跑测试", "pytest 怎么跑"),
    ("依赖用 uv 管理，不要用 pip", "依赖管理方式"),
    ("不要修改 legacy/ 目录", "legacy 目录能不能动"),
    ("提交信息使用中文", "提交信息语言"),
    ("验证命令是 make check", "验证命令是什么"),
    ("所有新增函数必须有类型标注", "类型标注要求"),
    ("API 错误统一抛 DomainError", "错误类型约定"),
    ("日期一律存 UTC", "时区约定"),
    ("前端状态用 zustand", "前端状态管理"),
    ("数据库迁移用 alembic", "数据库迁移工具"),
    ("日志不要打印用户手机号", "日志隐私要求"),
    ("分页默认每页 20 条", "分页默认值"),
    ("缓存键必须以环境名为前缀", "缓存键规范"),
    ("新增配置必须写入 .env.example", "配置文档要求"),
    ("测试文件放在 tests/ 下", "测试文件位置"),
    ("禁止提交 .env", "能否提交 .env"),
    ("CI 使用 Python 3.11", "CI Python 版本"),
    ("接口变更需要更新 docs/API.md", "接口文档要求"),
    ("金额计算使用 Decimal", "金额精度要求"),
    ("并发写文件必须加锁", "并发写要求"),
    ("回滚使用 file_restore", "如何回滚文件"),
    ("搜索优先用 search_code", "搜索工具选择"),
    ("符号定位用 symbol_search", "符号定位工具"),
    ("代码审查会检查被改弱的断言", "审查关注点"),
    ("验证失败必须先修再继续", "验证失败怎么办"),
    ("长任务要定期 replan", "长任务规划要求"),
    ("worker 不能修改 write_scope 外的文件", "worker 边界"),
    ("合并补丁前必须跑相关测试", "合并前检查"),
    ("不要读取 .agent/ 目录", "能否读 .agent"),
    ("所有输出使用中文", "输出语言要求"),
]


class _MemoryFs:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def read_text(self, path: str, *, max_bytes: int) -> str:
        if path not in self.files:
            from coding_agent.sandbox.fs import SandboxFsError

            raise SandboxFsError("not found")
        return self.files[path]

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content


def _new_memory() -> tuple[LongTermMemory, _MemoryFs]:
    fs = _MemoryFs()
    return LongTermMemory(fs, "/work"), fs  # type: ignore[arg-type]


def memory_benchmark(limit: int = 5) -> dict[str, Any]:
    memory, _ = _new_memory()
    for fact, _query in _MEMORY_FACTS:
        memory.add(fact, source="bench")

    hits = 0
    misses: list[dict[str, str]] = []
    for fact, query in _MEMORY_FACTS:
        results = memory.search(query, limit)
        if fact in results:
            hits += 1
        else:
            misses.append({"query": query, "expected": fact, "got": results[:2]})

    # 无关查询应回退到最早的 K 条，而不是硬凑一个“相似”结果
    fallback = memory.search("量子纠缠 zzzz", limit)
    fallback_ok = fallback == memory.load()[:limit]

    # 冲突：同一件事的新版本应替换旧版本
    memory.add("提交信息使用英文", source="bench")
    conflict_ok = (
        "提交信息使用英文" in memory.load()
        and "提交信息使用中文" not in memory.load()
    )

    # 过期：expires_at 已过的事实不再出现在 load/search 中
    expired_memory, fs = _new_memory()
    facts = ["这条临时事实已过期"]
    fs.files["/work/.agent/memory.md"] = "# 项目记忆\n- " + facts[0] + "\n"
    fs.files["/work/.agent/memory.meta.json"] = json.dumps(
        {
            "version": 1,
            "facts": {
                facts[0]: {
                    "source": "bench",
                    "created_at": "2000-01-01T00:00:00+00:00",
                    "expires_at": "2000-01-02T00:00:00+00:00",
                }
            },
        },
        ensure_ascii=False,
    )
    expiry_ok = expired_memory.load() == []

    total = len(_MEMORY_FACTS)
    return {
        "facts": total,
        "limit": limit,
        "recall_at_k": round(hits / total, 4),
        "misses": misses,
        "fallback_on_no_match": fallback_ok,
        "conflict_replaced": conflict_ok,
        "expiry_filtered": expiry_ok,
    }


def run_all() -> dict[str, Any]:
    return {"trim": trim_benchmark(), "memory": memory_benchmark()}


def _print_human(result: dict[str, Any]) -> None:
    trim = result["trim"]
    memory = result["memory"]
    print("== 上下文压缩 ==")
    print(
        f"消息 {trim['messages']} 条；字符 {trim['before_chars']} -> "
        f"{trim['after_chars']}（压缩 {trim['compression']:.1%}）；"
        f"token {trim['before_tokens']} -> {trim['after_tokens']}"
    )
    print(f"关键信息保持率：{trim['retention']:.1%}；缺失：{trim['missing'] or '无'}")
    print(
        f"token 预算内：{trim['under_token_budget']}；"
        f"降档：{trim['escalated']}"
    )
    print("\n== 记忆检索 ==")
    print(
        f"recall@{memory['limit']}：{memory['recall_at_k']:.1%}"
        f"（{memory['facts']} 条事实）"
    )
    print(f"无关查询回退：{memory['fallback_on_no_match']}")
    print(f"冲突替换：{memory['conflict_replaced']}")
    print(f"过期过滤：{memory['expiry_filtered']}")
    for miss in memory["misses"]:
        print(f"  未命中：{miss['query']} -> {miss['got']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="上下文/记忆确定性基准")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    result = run_all()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)


if __name__ == "__main__":
    main()
