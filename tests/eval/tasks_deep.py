"""评测任务集 · 进阶档：6 个为**区分度**设计的任务。

基础档（tasks.py 里的 20 个）首轮跑出 20/20 —— 每个都是 10 行文件里的一个明显
bug，且有一个失败的测试直接指出位置，平均只用了 15% 的预算。那种任务量不出差别，
等于白测。这一档针对计划里的四个 P0 设计：

| 任务 | 压测什么 |
| --- | --- |
| `deep/config_precedence` | 症状与根因相隔 5 个模块（P1-1 理解） |
| `deep/import_chain` | 跨 3 模块的循环依赖，要重排依赖而不是打补丁（P1-1） |
| `deep/field_propagation` | 新字段端到端贯通 parser→store→render（P0-1 长程） |
| `deep/spec_*` / `deep/request_payload` | **可见测试全绿、需求未满足**（P0-2 / P1-6 可信度） |

**spec 类任务的关键**：种子里可见测试是**通过**的（虽然需求没满足），所以"让测试
变绿"这个策略在这里毫无作用。判定靠 `hidden_tests` —— 它们从不写进工作区，
只在判分时加入。这是唯一能测出"是否照着需求做对、而不是只盯着绿化测试"的办法。
"""

from __future__ import annotations

from harness import CROSS_FILE, DEBUG, DEEP, SPEC, EvalTask

__all__ = ["DEEP_TASKS"]


def _p(problem: str) -> str:
    return (
        f"项目就在当前工作目录。{problem}\n"
        "请让项目自带的测试全部通过。测试文件不要改（判定前会被还原），改动请落在源码上。"
    )


def _spec(requirement: str) -> str:
    return (
        f"项目就在当前工作目录。{requirement}\n"
        "注意：项目自带的测试**可能没有覆盖上面的全部要求**。请按需求把行为做对，"
        "不要只满足于让现有测试变绿。"
    )


DEEP_TASKS: tuple[EvalTask, ...] = (
    # ------------------------------------------------------------------
    # 症状与根因相隔多个模块
    # ------------------------------------------------------------------
    EvalTask(
        id="deep/config_precedence",
        category=DEBUG,
        tier=DEEP,
        prompt=_p(
            "app.cli.summary 连续调用会串味：先带覆盖项调用一次，"
            "之后不带覆盖项的那次也会拿到上一次的值，而不是默认值。"
            "两次调用之间应该是完全独立的。"
        ),
        sources={
            "app/__init__.py": "",
            "app/config/__init__.py": "",
            "app/config/defaults.py": '''\
"""默认配置。"""

DEFAULTS = {"retries": 3, "tags": ["base"]}
''',
            "app/config/merge.py": '''\
"""合并配置。"""


def deep_merge(base, override):
    """把 override 合并进 base 并返回。"""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base
''',
            "app/config/sources.py": '''\
"""配置来源。"""


def from_mapping(data):
    """复制一份映射，避免调用方改到我们的内部结构。"""
    return dict(data)
''',
            "app/config/loader.py": '''\
"""加载配置。"""

from app.config.defaults import DEFAULTS
from app.config.merge import deep_merge
from app.config.sources import from_mapping


def load(override=None):
    """默认值叠加覆盖项。"""
    merged = deep_merge(DEFAULTS, dict(override or {}))
    return from_mapping(merged)
''',
            "app/service.py": '''\
"""业务逻辑。"""

from app.config.loader import load


def describe(override=None):
    config = load(override)
    return config["retries"], list(config["tags"])
''',
            "app/render.py": '''\
"""渲染。"""


def line(retries, tags):
    return f"retries={retries} tags={','.join(tags)}"
''',
            "app/cli.py": '''\
"""入口。"""

from app.render import line
from app.service import describe


def summary(override=None):
    retries, tags = describe(override)
    return line(retries, tags)
''',
        },
        # 失败信息只显示 cli 的输出，离根因（merge 就地改写默认值）隔了 5 个模块
        tests={
            "test_cli.py": '''\
import unittest

import app.cli


class SummaryTest(unittest.TestCase):
    def test_first_run(self):
        self.assertEqual(app.cli.summary({"retries": 5}), "retries=5 tags=base")

    def test_second_run_falls_back_to_defaults(self):
        app.cli.summary({"retries": 5})
        self.assertEqual(app.cli.summary(None), "retries=3 tags=base")
''',
        },
        reference={
            "app/config/merge.py": '''\
"""合并配置。"""


def deep_merge(base, override):
    """把 override 合并进 base 的**副本**并返回（绝不改动入参）。"""
    merged = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in base.items()
    }
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
''',
        },
    ),
    EvalTask(
        id="deep/import_chain",
        category=DEBUG,
        tier=DEEP,
        prompt=_p("导入这个包时就会失败，报的是循环导入。请修好，保持公开接口不变。"),
        sources={
            "pkg/__init__.py": "",
            "pkg/format.py": '''\
"""格式化。"""

from pkg.units import DECIMALS


def render(value):
    return f"{value:.{DECIMALS}f}"
''',
            "pkg/units.py": '''\
"""单位换算。"""

from pkg.format import render

DECIMALS = 2
RATIOS = {"m": 1.0, "km": 1000.0}


def describe(name):
    return render(RATIOS[name])
''',
            "pkg/report.py": '''\
"""报表。"""

from pkg.units import describe


def line(names):
    return ", ".join(describe(name) for name in names)
''',
        },
        tests={
            "test_units.py": '''\
import unittest

import pkg.format
import pkg.report
import pkg.units


class UnitTest(unittest.TestCase):
    def test_decimals(self):
        self.assertEqual(pkg.units.DECIMALS, 2)

    def test_render(self):
        self.assertEqual(pkg.format.render(1.5), "1.50")

    def test_describe(self):
        self.assertEqual(pkg.units.describe("km"), "1000.00")

    def test_report(self):
        self.assertEqual(pkg.report.line(["m", "km"]), "1.00, 1000.00")
''',
        },
        reference={
            "pkg/constants.py": '''\
"""共享常量（从循环里抽出来，打破 format ↔ units 的环）。"""

DECIMALS = 2
''',
            "pkg/format.py": '''\
"""格式化。"""

from pkg.constants import DECIMALS


def render(value):
    return f"{value:.{DECIMALS}f}"
''',
            "pkg/units.py": '''\
"""单位换算。"""

from pkg.constants import DECIMALS
from pkg.format import render

RATIOS = {"m": 1.0, "km": 1000.0}


def describe(name):
    return render(RATIOS[name])
''',
        },
    ),

    # ------------------------------------------------------------------
    # 可见测试全绿、需求未满足（判定靠只在判分时加入的隐藏测试）
    # ------------------------------------------------------------------
    EvalTask(
        id="deep/spec_pagination",
        category=SPEC,
        tier=DEEP,
        prompt=_spec(
            "分页语义要求：页码从 1 开始；超出范围的页返回空列表（不抛异常）；"
            "总页数按实际页数计算 —— 空集合是 0 页，正好整除时不多算一页。"
        ),
        sources={
            "shelf/__init__.py": "",
            "shelf/paginator.py": '''\
"""分页。"""


class Paginator:
    def __init__(self, items, per_page=10):
        self.items = list(items)
        self.per_page = per_page

    def page(self, number):
        """取第 number 页（从 1 开始）。"""
        start = (number - 1) * self.per_page
        return self.items[start : start + self.per_page]

    def page_count(self):
        """总页数。"""
        return len(self.items) // self.per_page + 1
''',
        },
        tests={
            "test_paginator.py": '''\
import unittest

from shelf.paginator import Paginator


class PaginatorTest(unittest.TestCase):
    def test_first_page(self):
        paginator = Paginator(range(25), per_page=10)
        self.assertEqual(paginator.page(1), list(range(0, 10)))

    def test_last_partial_page(self):
        paginator = Paginator(range(25), per_page=10)
        self.assertEqual(paginator.page(3), [20, 21, 22, 23, 24])
''',
        },
        hidden_tests={
            "test_paginator_spec.py": '''\
import unittest

from shelf.paginator import Paginator


class PaginatorSpecTest(unittest.TestCase):
    def test_empty_collection_has_zero_pages(self):
        self.assertEqual(Paginator([], per_page=10).page_count(), 0)

    def test_exact_multiple_is_not_overcounted(self):
        self.assertEqual(Paginator(range(20), per_page=10).page_count(), 2)

    def test_partial_page_is_counted(self):
        self.assertEqual(Paginator(range(21), per_page=10).page_count(), 3)

    def test_page_beyond_the_end_is_empty(self):
        paginator = Paginator(range(5), per_page=10)
        self.assertEqual(paginator.page(2), [])
        self.assertEqual(paginator.page(99), [])

    def test_page_below_one_is_empty(self):
        paginator = Paginator(range(5), per_page=10)
        self.assertEqual(paginator.page(0), [])
''',
        },
        reference={
            "shelf/paginator.py": '''\
"""分页。"""


class Paginator:
    def __init__(self, items, per_page=10):
        self.items = list(items)
        self.per_page = per_page

    def page(self, number):
        """取第 number 页（从 1 开始）；越界返回空列表。"""
        if number < 1:
            return []
        start = (number - 1) * self.per_page
        return self.items[start : start + self.per_page]

    def page_count(self):
        """总页数；空集合是 0。"""
        if not self.items:
            return 0
        return (len(self.items) + self.per_page - 1) // self.per_page
''',
        },
    ),
    EvalTask(
        id="deep/spec_idempotent",
        category=SPEC,
        tier=DEEP,
        prompt=_spec(
            "import_records 必须**幂等**：内容完全相同的记录重复导入不应产生写入。"
            "它的返回值是**实际发生变化的条数** —— 新增与更新各算一条，"
            "内容没变的记录不计入。"
        ),
        sources={
            "sync/__init__.py": "",
            "sync/store.py": '''\
"""内存存储。"""


class Store:
    def __init__(self):
        self.rows = {}

    def upsert(self, key, value):
        self.rows[key] = value

    def get(self, key):
        return self.rows.get(key)

    def all(self):
        return dict(self.rows)
''',
            "sync/importer.py": '''\
"""导入记录。"""


def import_records(store, records):
    """把记录写入存储，返回写入条数。"""
    count = 0
    for record in records:
        store.upsert(record["id"], record)
        count += 1
    return count
''',
        },
        tests={
            "test_import.py": '''\
import unittest

from sync.importer import import_records
from sync.store import Store


class ImportTest(unittest.TestCase):
    def test_imports_all(self):
        store = Store()
        self.assertEqual(import_records(store, [{"id": "a", "v": 1}]), 1)
        self.assertEqual(store.all(), {"a": {"id": "a", "v": 1}})
''',
        },
        hidden_tests={
            "test_import_spec.py": '''\
import unittest

from sync.importer import import_records
from sync.store import Store


class ImportSpecTest(unittest.TestCase):
    def test_second_import_of_same_records_writes_nothing(self):
        store = Store()
        records = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
        self.assertEqual(import_records(store, records), 2)
        self.assertEqual(import_records(store, records), 0)

    def test_changed_record_counts_as_written(self):
        store = Store()
        import_records(store, [{"id": "a", "v": 1}])
        self.assertEqual(import_records(store, [{"id": "a", "v": 2}]), 1)
        self.assertEqual(store.get("a"), {"id": "a", "v": 2})

    def test_mixed_batch_counts_only_changes(self):
        store = Store()
        import_records(store, [{"id": "a", "v": 1}, {"id": "b", "v": 2}])
        batch = [{"id": "a", "v": 1}, {"id": "b", "v": 9}, {"id": "c", "v": 3}]
        self.assertEqual(import_records(store, batch), 2)

    def test_empty_batch(self):
        self.assertEqual(import_records(Store(), []), 0)
''',
        },
        reference={
            "sync/importer.py": '''\
"""导入记录。"""


def import_records(store, records):
    """把记录写入存储，返回实际发生变化的条数（内容相同的重复导入不计）。"""
    changed = 0
    for record in records:
        if store.get(record["id"]) != record:
            store.upsert(record["id"], record)
            changed += 1
    return changed
''',
        },
    ),
    EvalTask(
        id="deep/request_payload",
        category=SPEC,
        tier=DEEP,
        prompt=_spec(
            "构造请求体时，只发送**显式提供**的字段：没传的可选字段必须从字典里省略，"
            "而不是发一个 null 出去。显式传空列表 / 空字符串要保留。"
        ),
        sources={
            "httpc/__init__.py": "",
            "httpc/client.py": '''\
"""极简请求构造。"""


def build_payload(title, tags=None, note=None):
    """构造请求体。"""
    return {"title": title, "tags": tags, "note": note}
''',
        },
        tests={
            "test_payload.py": '''\
import unittest

from httpc.client import build_payload


class PayloadTest(unittest.TestCase):
    def test_all_fields_present(self):
        self.assertEqual(
            build_payload("t", tags=["a"], note="n"),
            {"title": "t", "tags": ["a"], "note": "n"},
        )
''',
        },
        hidden_tests={
            "test_payload_spec.py": '''\
import unittest

from httpc.client import build_payload


class PayloadSpecTest(unittest.TestCase):
    def test_unset_optionals_are_omitted(self):
        self.assertEqual(build_payload("t"), {"title": "t"})

    def test_empty_list_is_kept(self):
        self.assertEqual(build_payload("t", tags=[]), {"title": "t", "tags": []})

    def test_empty_string_is_kept(self):
        self.assertEqual(build_payload("t", note=""), {"title": "t", "note": ""})

    def test_only_one_optional(self):
        self.assertEqual(build_payload("t", note="n"), {"title": "t", "note": "n"})
''',
        },
        reference={
            "httpc/client.py": '''\
"""极简请求构造。"""


def build_payload(title, tags=None, note=None):
    """构造请求体：未显式提供的可选字段不出现在结果里。"""
    payload = {"title": title}
    if tags is not None:
        payload["tags"] = tags
    if note is not None:
        payload["note"] = note
    return payload
''',
        },
    ),

    # ------------------------------------------------------------------
    # 长程：新字段端到端贯通多个模块
    # ------------------------------------------------------------------
    EvalTask(
        id="deep/field_propagation",
        category=CROSS_FILE,
        tier=DEEP,
        prompt=_spec(
            "任务行格式是 `名称:优先级`，优先级是整数，省略冒号时按 0 处理。"
            "构建出来的任务要按优先级**从大到小**排列，优先级相同的保持输入顺序；"
            "渲染成 `名称(优先级)` 的形式。可见测试只覆盖了没有优先级的老格式。"
        ),
        sources={
            "sched/__init__.py": "",
            "sched/parser.py": '''\
"""解析任务行。"""


def parse_line(line):
    """解析一行任务，返回 {"name": ...}。"""
    return {"name": line.strip()}
''',
            "sched/store.py": '''\
"""任务存储。"""


class Store:
    def __init__(self):
        self.tasks = []

    def add(self, task):
        self.tasks.append(task)

    def ordered(self):
        """按添加顺序返回。"""
        return list(self.tasks)
''',
            "sched/render.py": '''\
"""渲染。"""


def render(task):
    return task["name"]
''',
            "sched/api.py": '''\
"""对外接口。"""

from sched.parser import parse_line
from sched.render import render
from sched.store import Store


def build(lines):
    store = Store()
    for line in lines:
        store.add(parse_line(line))
    return store


def text(store):
    return " | ".join(render(task) for task in store.ordered())
''',
        },
        # 可见测试只断言**不随需求改变**的部分。绝不能断言 `{"name": ...}` 或
        # `"a | b"` 这类精确形态 —— 新需求必然要改它们，那样任务就自相矛盾、
        # 无论怎么做都无法同时满足。（这个坑踩过一次：智能体做对了却被判失败。）
        tests={
            "test_sched.py": '''\
import unittest

from sched.api import build, text
from sched.parser import parse_line


class ParseTest(unittest.TestCase):
    def test_name_is_extracted(self):
        self.assertEqual(parse_line("write docs")["name"], "write docs")


class BuildTest(unittest.TestCase):
    def test_names_survive_the_pipeline(self):
        store = build(["a", "b"])
        self.assertEqual([task["name"] for task in store.ordered()], ["a", "b"])

    def test_text_mentions_every_name(self):
        rendered = text(build(["a", "b"]))
        self.assertIn("a", rendered)
        self.assertIn("b", rendered)
''',
        },
        hidden_tests={
            "test_sched_spec.py": '''\
import unittest

from sched.api import build, text
from sched.parser import parse_line


class PriorityParsingTest(unittest.TestCase):
    def test_priority_is_parsed(self):
        self.assertEqual(parse_line("write docs:3"), {"name": "write docs", "priority": 3})

    def test_missing_priority_defaults_to_zero(self):
        self.assertEqual(parse_line("no priority")["priority"], 0)

    def test_name_may_contain_spaces(self):
        self.assertEqual(parse_line("  write docs : 7 ")["name"], "write docs")


class OrderingTest(unittest.TestCase):
    def test_sorted_by_priority_descending(self):
        store = build(["low:1", "high:9", "mid:5"])
        self.assertEqual(text(store), "high(9) | mid(5) | low(1)")

    def test_same_priority_keeps_input_order(self):
        store = build(["first:2", "second:2", "third:2"])
        self.assertEqual(text(store), "first(2) | second(2) | third(2)")

    def test_default_priority_sorts_last(self):
        store = build(["explicit:1", "implicit"])
        self.assertEqual(text(store), "explicit(1) | implicit(0)")
''',
        },
        reference={
            "sched/parser.py": '''\
"""解析任务行。"""


def parse_line(line):
    """解析 `名称:优先级`；省略冒号时优先级为 0。"""
    name, sep, raw = line.rpartition(":")
    if not sep or not name.strip():
        return {"name": line.strip(), "priority": 0}
    return {"name": name.strip(), "priority": int(raw.strip())}
''',
            "sched/store.py": '''\
"""任务存储。"""


class Store:
    def __init__(self):
        self.tasks = []

    def add(self, task):
        self.tasks.append(task)

    def ordered(self):
        """按优先级从大到小；同优先级保持输入顺序（sorted 是稳定排序）。"""
        return sorted(self.tasks, key=lambda task: task.get("priority", 0), reverse=True)
''',
            "sched/render.py": '''\
"""渲染。"""


def render(task):
    return f"{task['name']}({task.get('priority', 0)})"
''',
        },
    ),
)
