"""评测任务集 v0：20 个任务，三类能力。

分类对应「理解 + 生产」的两个阶段与排障：

- `single_file`  单文件改动：读懂一个函数，改对（7 个）
- `cross_file`   跨文件重构：理解模块间关系，一致地改（7 个）
- `debug`        排障修复：从症状定位到根因，症状常在 A 文件、根因在 B 文件（6 个）

每个任务的种子都是**小而真**的 Python 项目：自带测试、初始必然失败、判定用
项目自带的测试（判定前会被还原成纯净副本，见 harness.py）。

写新任务的规矩
--------------
1. **种子必须失败**：`--verify-seeds` 会断言这一点。判定在种子状态就通过的任务
   是假阳性，会把基线抬高到没有意义。
2. **判定只看外部行为**：断言导入面与函数输出，不断言内部实现（除了任务本身
   要求的结构变化，例如"旧名字不再存在"）。
"""

from __future__ import annotations

from harness import CROSS_FILE, DEBUG, SINGLE_FILE, EvalTask
from tasks_deep import DEEP_TASKS
from tasks_replan import REPLAN_TASKS

__all__ = ["TASKS"]


def _p(problem: str) -> str:
    return (
        f"项目就在当前工作目录。{problem}\n"
        "请让项目自带的测试全部通过。测试文件不要改（判定前会被还原），改动请落在源码上。"
    )


BASIC_TASKS: tuple[EvalTask, ...] = (
    # ------------------------------------------------------------------
    # 单文件改动
    # ------------------------------------------------------------------
    EvalTask(
        id="single_file/off_by_one",
        category=SINGLE_FILE,
        prompt=_p("test_calc.py 里的平均值断言失败了。"),
        sources={
            "calc.py": '''\
"""简单统计工具。"""


def average(nums):
    """返回这组数的平均值。"""
    return sum(nums) / (len(nums) + 1)


def total(nums):
    """返回这组数的总和。"""
    return sum(nums)
''',
        },
        tests={
            "test_calc.py": '''\
import unittest

from calc import average, total


class CalcTest(unittest.TestCase):
    def test_average_of_three(self):
        self.assertEqual(average([1, 2, 3]), 2.0)

    def test_average_of_single(self):
        self.assertEqual(average([5]), 5.0)

    def test_total(self):
        self.assertEqual(total([1, 2, 3]), 6)
''',
        },
    ),
    EvalTask(
        id="single_file/mutable_default",
        category=SINGLE_FILE,
        prompt=_p(
            "inventory.add_item 多次调用之间会互相影响，第二次调用拿到了上一次的结果。"
        ),
        sources={
            "inventory.py": '''\
"""库存清单。"""


def add_item(item, bucket=[]):
    """把 item 加进 bucket 并返回它。"""
    bucket.append(item)
    return bucket
''',
        },
        tests={
            "test_inventory.py": '''\
import unittest

from inventory import add_item


class InventoryTest(unittest.TestCase):
    def test_first_call(self):
        self.assertEqual(add_item("apple"), ["apple"])

    def test_calls_do_not_share_state(self):
        self.assertEqual(add_item("pear"), ["pear"])

    def test_explicit_bucket_is_used(self):
        bucket = ["x"]
        self.assertEqual(add_item("y", bucket), ["x", "y"])
        self.assertEqual(bucket, ["x", "y"])
''',
        },
    ),
    EvalTask(
        id="single_file/port_validation",
        category=SINGLE_FILE,
        prompt=_p(
            "parse_port 对非法输入不设防。端口范围是 1–65535，"
            "超出范围或无法解析成整数的输入都必须抛 ValueError。"
        ),
        sources={
            "ports.py": '''\
"""端口解析。"""


def parse_port(text):
    """把字符串解析成端口号。"""
    return int(text)
''',
        },
        tests={
            "test_ports.py": '''\
import unittest

from ports import parse_port


class ParsePortTest(unittest.TestCase):
    def test_valid(self):
        for text, expected in [("80", 80), ("1", 1), ("65535", 65535)]:
            with self.subTest(text=text):
                self.assertEqual(parse_port(text), expected)

    def test_invalid(self):
        for text in ["0", "-1", "65536", "abc", ""]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_port(text)
''',
        },
    ),
    EvalTask(
        id="single_file/todo_stub",
        category=SINGLE_FILE,
        prompt=_p(
            "textstats.word_count 还没实现（调用会抛 NotImplementedError）。"
            "单词按空白切分，空文本算 0 个词。"
        ),
        sources={
            "textstats.py": '''\
"""文本统计。"""


def word_count(text):
    """返回文本中的单词数（按空白切分）。"""
    raise NotImplementedError("word_count 还没实现")


def longest_word(text):
    """返回最长的单词；没有单词时返回空串。"""
    words = text.split()
    return max(words, key=len) if words else ""
''',
        },
        tests={
            "test_textstats.py": '''\
import unittest

from textstats import longest_word, word_count


class TextStatsTest(unittest.TestCase):
    def test_count(self):
        self.assertEqual(word_count("a b c"), 3)

    def test_count_empty(self):
        self.assertEqual(word_count(""), 0)

    def test_count_extra_spaces(self):
        self.assertEqual(word_count("  a   b "), 2)

    def test_longest(self):
        self.assertEqual(longest_word("hi there world"), "there")

    def test_longest_empty(self):
        self.assertEqual(longest_word(""), "")
''',
        },
    ),
    EvalTask(
        id="single_file/slugify",
        category=SINGLE_FILE,
        prompt=_p(
            "slugify 生成的 slug 不符合要求。规则：转小写；去掉首尾空白；"
            "把连续的空白压成一个连字符；去掉所有非字母数字的字符（连字符本身除外）；"
            "结果里不能出现连续连字符、也不能以连字符开头或结尾。"
        ),
        sources={
            "slugify.py": '''\
"""把标题转成 URL slug。"""


def slugify(title):
    """转小写、空格换连字符。"""
    return title.lower().replace(" ", "-")
''',
        },
        tests={
            "test_slugify.py": '''\
import unittest

from slugify import slugify


class SlugifyTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_collapses_repeated_spaces(self):
        self.assertEqual(slugify("Hello   World"), "hello-world")

    def test_strips_edges(self):
        self.assertEqual(slugify("  Hello World  "), "hello-world")

    def test_strips_punctuation(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
''',
        },
    ),
    EvalTask(
        id="single_file/money_rounding",
        category=SINGLE_FILE,
        prompt=_p(
            "金额计算出现了浮点误差（例如 0.1 + 0.2 不等于 0.3）。"
            "add 与 total 的结果都必须四舍五入到两位小数。"
        ),
        sources={
            "money.py": '''\
"""金额计算（单位：元）。"""


def add(a, b):
    """相加，结果保留两位小数。"""
    return a + b


def total(prices):
    """累加，结果保留两位小数。"""
    result = 0
    for price in prices:
        result += price
    return result
''',
        },
        tests={
            "test_money.py": '''\
import unittest

from money import add, total


class MoneyTest(unittest.TestCase):
    def test_add_two_decimals(self):
        self.assertEqual(add(0.1, 0.2), 0.3)

    def test_total_rounds(self):
        self.assertEqual(total([0.1, 0.2, 0.3]), 0.6)

    def test_total_of_many(self):
        self.assertEqual(total([0.01] * 100), 1.0)
''',
        },
    ),
    EvalTask(
        id="single_file/email_validation",
        category=SINGLE_FILE,
        prompt=_p(
            "is_email 的校验太松，很多非法地址也被判为合法。要求："
            "恰好一个 @；本地部分非空且不含空格；"
            "域名部分必须含点号，且最后一段（顶级域）至少两个字母。"
        ),
        sources={
            "validators.py": '''\
"""输入校验。"""

import re

_EMAIL = re.compile(r".+@.+")


def is_email(text):
    """判断是否是合法的邮箱地址。"""
    return bool(_EMAIL.fullmatch(text))
''',
        },
        tests={
            "test_validators.py": '''\
import unittest

from validators import is_email


class EmailValidationTest(unittest.TestCase):
    def test_valid(self):
        for text in ["a@b.com", "user.name@example.co.uk", "x1@y2.io"]:
            with self.subTest(text=text):
                self.assertTrue(is_email(text))

    def test_invalid(self):
        for text in ["a@b", "a b@c.com", "@b.com", "a@", "a@@b.com", "", "a@b.c"]:
            with self.subTest(text=text):
                self.assertFalse(is_email(text))
''',
        },
    ),

    # ------------------------------------------------------------------
    # 跨文件重构
    # ------------------------------------------------------------------
    EvalTask(
        id="cross_file/rename_function",
        category=CROSS_FILE,
        prompt=_p(
            "把 pkg.geometry.area_circle 重命名为 circle_area，并更新所有调用点。"
            "旧名字必须彻底消失（不能再通过 area_circle 访问到它）。"
        ),
        sources={
            "pkg/__init__.py": "from pkg.geometry import area_circle\n",
            "pkg/geometry.py": '''\
"""几何计算。"""

import math


def area_circle(radius):
    """圆的面积。"""
    return math.pi * radius**2
''',
            "pkg/report.py": '''\
"""报表。"""

from pkg.geometry import area_circle


def describe(radius):
    return f"半径 {radius} 的圆面积约 {area_circle(radius):.2f}"
''',
            "pkg/cli.py": '''\
"""命令行入口。"""

from pkg.geometry import area_circle


def main(argv):
    radius = float(argv[0])
    return f"{area_circle(radius):.2f}"
''',
        },
        tests={
            "test_rename.py": '''\
import unittest

import pkg.cli
import pkg.geometry
import pkg.report


class RenameTest(unittest.TestCase):
    def test_new_name_works(self):
        self.assertGreater(pkg.geometry.circle_area(2), 12)

    def test_old_name_is_gone(self):
        self.assertFalse(hasattr(pkg.geometry, "area_circle"))

    def test_callers_follow(self):
        self.assertTrue(pkg.report.describe(2).startswith("半径 2"))
        self.assertEqual(pkg.cli.main(["2"]), "12.57")
''',
        },
    ),
    EvalTask(
        id="cross_file/extract_helper",
        category=CROSS_FILE,
        prompt=_p(
            "pkg/a.py、pkg/b.py、pkg/c.py 里各有一份一模一样的 _normalize 私有函数。"
            "把它抽到新模块 pkg/textutil.py，函数名改为公开的 normalize，"
            "三个模块都改成调用它，本地副本必须删掉。"
        ),
        sources={
            "pkg/__init__.py": "",
            "pkg/a.py": '''\
def _normalize(text):
    return " ".join(text.split()).strip().lower()


def render_a(text):
    return f"A:{_normalize(text)}"
''',
            "pkg/b.py": '''\
def _normalize(text):
    return " ".join(text.split()).strip().lower()


def render_b(text):
    return f"B:{_normalize(text)}"
''',
            "pkg/c.py": '''\
def _normalize(text):
    return " ".join(text.split()).strip().lower()


def render_c(text):
    return f"C:{_normalize(text)}"
''',
        },
        tests={
            "test_extract.py": '''\
import unittest

import pkg.a
import pkg.b
import pkg.c
import pkg.textutil


class ExtractHelperTest(unittest.TestCase):
    def test_helper_lives_in_one_place(self):
        self.assertEqual(pkg.textutil.normalize("  Hello   World "), "hello world")

    def test_local_copies_are_gone(self):
        for module in (pkg.a, pkg.b, pkg.c):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "_normalize"))

    def test_behaviour_unchanged(self):
        self.assertEqual(pkg.a.render_a("  Hi  There "), "A:hi there")
        self.assertEqual(pkg.b.render_b("  Hi  There "), "B:hi there")
        self.assertEqual(pkg.c.render_c("  Hi  There "), "C:hi there")
''',
        },
    ),
    EvalTask(
        id="cross_file/move_class",
        category=CROSS_FILE,
        prompt=_p(
            "把 pkg/models.py 里的 User 类搬到 pkg/entities/user.py，"
            "并要求 pkg.models.User 仍然可用、且与 pkg.entities.user.User 是**同一个类对象**"
            "（pkg/service.py 不希望被改动）。"
        ),
        sources={
            "pkg/__init__.py": "",
            "pkg/entities/__init__.py": "",
            "pkg/models.py": '''\
"""数据模型。"""


class User:
    def __init__(self, name, age):
        self.name = name
        self.age = age

    def label(self):
        return f"{self.name}({self.age})"
''',
            "pkg/service.py": '''\
from pkg.models import User


def make(name, age):
    return User(name, age)
''',
        },
        tests={
            "test_move.py": '''\
import unittest

import pkg.entities.user
import pkg.models
import pkg.service


class MoveClassTest(unittest.TestCase):
    def test_new_location(self):
        user = pkg.entities.user.User("amy", 30)
        self.assertEqual(user.label(), "amy(30)")

    def test_old_import_is_the_same_class(self):
        self.assertIs(pkg.models.User, pkg.entities.user.User)

    def test_service_unaffected(self):
        self.assertEqual(pkg.service.make("bob", 20).label(), "bob(20)")
''',
        },
    ),
    EvalTask(
        id="cross_file/signature_change",
        category=CROSS_FILE,
        prompt=_p(
            "pkg/http.fetch 需要支持超时参数：fetch(url, timeout=30)，"
            "并把实际使用的超时记录进 pkg.http.CALLS（形如 (url, timeout)）。"
            "pkg/api.get_user 也要能透传 timeout，默认 30。"
        ),
        sources={
            "pkg/__init__.py": "",
            "pkg/http.py": '''\
"""极简请求封装。"""

CALLS = []


def fetch(url):
    CALLS.append(url)
    return {"url": url}
''',
            "pkg/api.py": '''\
from pkg.http import fetch


def get_user(uid):
    return fetch(f"/users/{uid}")
''',
        },
        tests={
            "test_signature.py": '''\
import unittest

import pkg.api
import pkg.http


class SignatureChangeTest(unittest.TestCase):
    def test_default_timeout(self):
        pkg.http.CALLS.clear()
        pkg.api.get_user(7)
        self.assertEqual(pkg.http.CALLS, [("/users/7", 30)])

    def test_explicit_timeout_is_forwarded(self):
        pkg.http.CALLS.clear()
        self.assertEqual(pkg.api.get_user(7, timeout=5)["timeout"], 5)
        self.assertEqual(pkg.http.CALLS, [("/users/7", 5)])

    def test_fetch_default(self):
        pkg.http.CALLS.clear()
        self.assertEqual(pkg.http.fetch("/x")["timeout"], 30)
''',
        },
    ),
    EvalTask(
        id="cross_file/split_module",
        category=CROSS_FILE,
        prompt=_p(
            "pkg/big.py 把两件不相关的事混在一起。"
            "把圆的面积搬到 pkg/geometry.py、把 slugify 搬到 pkg/text.py，"
            "并让 pkg/big.py 仍然导出这两个名字（旧调用点不要失效）。"
        ),
        sources={
            "pkg/__init__.py": "",
            "pkg/big.py": '''\
"""混在一起的两件事。"""

import math


def circle_area(radius):
    return math.pi * radius**2


def slugify(text):
    return "-".join(text.lower().split())
''',
        },
        tests={
            "test_split.py": '''\
import unittest

import pkg.big
import pkg.geometry
import pkg.text


class SplitModuleTest(unittest.TestCase):
    def test_new_modules(self):
        self.assertGreater(pkg.geometry.circle_area(2), 12)
        self.assertEqual(pkg.text.slugify("A B"), "a-b")

    def test_old_module_still_exports(self):
        self.assertGreater(pkg.big.circle_area(2), 12)
        self.assertEqual(pkg.big.slugify("A B"), "a-b")
''',
        },
    ),
    EvalTask(
        id="cross_file/config_single_source",
        category=CROSS_FILE,
        prompt=_p(
            "INDEX_NAMES 在 pkg/store.py 和 pkg/report.py 里各写了一份，改一处不会影响另一处。"
            "把它收敛成 pkg/config.py 里的唯一定义，两个模块都从那里引用。"
        ),
        sources={
            "pkg/__init__.py": "",
            "pkg/config.py": '''\
"""全局配置。"""
''',
            "pkg/store.py": '''\
"""索引存储。"""

INDEX_NAMES = ["docs", "faq"]


def names():
    return len(INDEX_NAMES)
''',
            "pkg/report.py": '''\
"""报表。"""

INDEX_NAMES = ["docs", "faq"]


def describe():
    return f"索引数 {len(INDEX_NAMES)}"
''',
        },
        tests={
            "test_config.py": '''\
import unittest

import pkg.config
import pkg.report
import pkg.store


class ConfigSingleSourceTest(unittest.TestCase):
    def test_single_source(self):
        self.assertEqual(pkg.config.INDEX_NAMES, ["docs", "faq"])
        self.assertIs(pkg.store.INDEX_NAMES, pkg.config.INDEX_NAMES)
        self.assertIs(pkg.report.INDEX_NAMES, pkg.config.INDEX_NAMES)

    def test_behaviour_unchanged(self):
        self.assertEqual(pkg.store.names(), 2)
        self.assertEqual(pkg.report.describe(), "索引数 2")
''',
        },
    ),
    EvalTask(
        id="cross_file/shape_mismatch",
        category=CROSS_FILE,
        prompt=_p(
            "reader → transform → writer 这条流水线跑不通：transform 产出的数据结构"
            "和 writer 期望的对不上。请统一两侧的约定，并让 render 的输出按 value 升序、"
            "形如 a=1|b=2|c=3。"
        ),
        sources={
            "pkg/__init__.py": "",
            "pkg/reader.py": '''\
"""读取原始行。"""


def read_rows():
    return ["b,2", "a,1", "c,3"]
''',
            "pkg/transform.py": '''\
"""把原始行转成记录。"""


def to_records(rows):
    records = []
    for row in rows:
        name, value = row.split(",")
        records.append({"name": name, "value": int(value)})
    return records
''',
            "pkg/writer.py": '''\
"""渲染记录。"""


def render(records):
    return "|".join(f"{name}={value}" for name, value in records)
''',
        },
        tests={
            "test_pipeline.py": '''\
import unittest

import pkg.reader
import pkg.transform
import pkg.writer


class PipelineTest(unittest.TestCase):
    def test_pipeline(self):
        records = pkg.transform.to_records(pkg.reader.read_rows())
        self.assertEqual(pkg.writer.render(records), "a=1|b=2|c=3")
''',
        },
    ),

    # ------------------------------------------------------------------
    # 排障修复
    # ------------------------------------------------------------------
    EvalTask(
        id="debug/misleading_error",
        category=DEBUG,
        prompt=_p(
            "api.describe 对缺失的键会抛出难以理解的 TypeError。"
            "要求：缺失的键必须抛 KeyError，并且消息里要包含那个键名。"
        ),
        sources={
            "store.py": '''\
"""键值存储。"""

_DATA = {"a": 1}


def get(key):
    """取值；不存在时返回 None。"""
    return _DATA.get(key)


def put(key, value):
    _DATA[key] = value
''',
            "api.py": '''\
from store import get


def describe(key):
    """返回 '键=值'，值加一后输出。"""
    value = get(key)
    return f"{key}={value + 1}"
''',
        },
        tests={
            "test_api.py": '''\
import unittest

import api


class DescribeTest(unittest.TestCase):
    def test_describe_existing(self):
        self.assertEqual(api.describe("a"), "a=2")

    def test_describe_missing_key(self):
        with self.assertRaises(KeyError) as caught:
            api.describe("missing")
        self.assertIn("missing", str(caught.exception))
''',
        },
    ),
    EvalTask(
        id="debug/circular_import",
        category=DEBUG,
        prompt=_p(
            "a 和 b 互相导入导致 import a 直接失败。"
            "请修好循环依赖，并保持最终结果：a.VALUE == 1、b.VALUE == 2、a.TOTAL == 3。"
        ),
        sources={
            "a.py": '''\
"""模块 a。"""

import b

VALUE = 1
TOTAL = b.VALUE + VALUE
''',
            "b.py": '''\
"""模块 b。"""

import a

VALUE = a.VALUE + 1
''',
        },
        tests={
            "test_imports.py": '''\
import unittest

import a
import b


class ValuesTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(a.VALUE, 1)
        self.assertEqual(b.VALUE, 2)
        self.assertEqual(a.TOTAL, 3)
''',
        },
    ),
    EvalTask(
        id="debug/leaked_counter",
        category=DEBUG,
        prompt=_p(
            "counter.handle 的返回值会跨调用累加，导致第二个测试失败。"
            "要求：每次调用只返回**本批**的数量，不带历史累计。"
        ),
        sources={
            "counter.py": '''\
"""请求计数器。"""

_CALLS = 0


def handle(requests):
    """处理一批请求，返回处理总数。"""
    global _CALLS
    _CALLS += len(requests)
    return _CALLS
''',
        },
        tests={
            "test_counter.py": '''\
import unittest

import counter


class CounterTest(unittest.TestCase):
    def test_first_batch(self):
        self.assertEqual(counter.handle(["a", "b"]), 2)

    def test_second_batch_is_independent(self):
        self.assertEqual(counter.handle(["c"]), 1)

    def test_empty_batch(self):
        self.assertEqual(counter.handle([]), 0)
''',
        },
    ),
    EvalTask(
        id="debug/output_order",
        category=DEBUG,
        prompt=_p(
            "report.tag_line 输出的标签顺序不对。要求按字母序升序输出，逗号分隔。"
        ),
        sources={
            "report.py": '''\
"""生成标签行。"""

TAGS = ["gamma", "alpha", "beta"]


def tag_line():
    return ",".join(TAGS)
''',
        },
        tests={
            "test_report.py": '''\
import unittest

from report import tag_line


class ReportTest(unittest.TestCase):
    def test_sorted_alphabetically(self):
        self.assertEqual(tag_line(), "alpha,beta,gamma")
''',
        },
    ),
    EvalTask(
        id="debug/unflushed_write",
        category=DEBUG,
        prompt=_p(
            "journal.append 写完之后立刻读，读不到内容 —— 写入没有真正落到文件。"
            "请修好 append，同时保证多次追加都能被读到。"
        ),
        sources={
            "journal.py": '''\
"""简单的日志追加。"""

_HANDLES = []


def append(path, line):
    handle = open(path, "a")
    _HANDLES.append(handle)
    handle.write(line + "\\n")


def read_all(path):
    with open(path) as handle:
        return handle.read()
''',
        },
        tests={
            "test_journal.py": '''\
import tempfile
import unittest
from pathlib import Path

import journal


class JournalTest(unittest.TestCase):
    def test_append_then_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "j.txt"
            journal.append(str(path), "first")
            self.assertEqual(journal.read_all(str(path)), "first\\n")

    def test_multiple_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k.txt"
            journal.append(str(path), "a")
            journal.append(str(path), "b")
            self.assertEqual(journal.read_all(str(path)), "a\\nb\\n")
''',
        },
    ),
    EvalTask(
        id="debug/empty_edge_case",
        category=DEBUG,
        prompt=_p(
            "stats 里的两个函数在空列表上会崩（IndexError / ValueError）。"
            "要求：空输入一律返回 None，非空输入行为不变。"
        ),
        sources={
            "stats.py": '''\
"""统计。"""


def median(nums):
    """返回中位数。"""
    ordered = sorted(nums)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def spread(nums):
    """返回最大值减最小值。"""
    return max(nums) - min(nums)
''',
        },
        tests={
            "test_stats.py": '''\
import unittest

from stats import median, spread


class StatsTest(unittest.TestCase):
    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_median_even(self):
        self.assertEqual(median([1, 2, 3, 4]), 2.5)

    def test_median_empty(self):
        self.assertIsNone(median([]))

    def test_spread(self):
        self.assertEqual(spread([1, 5, 3]), 4)

    def test_spread_empty(self):
        self.assertIsNone(spread([]))
''',
        },
    ),
)

# 进阶档见 tasks_deep.py；重规划族（可做 A/B 对照）见 tasks_replan.py。
# 分文件只是为了各自可读。
TASKS: tuple[EvalTask, ...] = BASIC_TASKS + DEEP_TASKS + REPLAN_TASKS
