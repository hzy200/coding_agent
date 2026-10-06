"""评测任务集 · 重规划族：3 个「计划必然需要修正」的任务。

为什么单独一族
--------------
`planner` 只看**用户请求**，看不到仓库（`graph/nodes/planner.py` 只把 request
交给模型）。所以计划的质量完全取决于请求里的信息量：

- 请求含糊 → 计划也含糊但通常不错 → 没什么可重规划的
- **请求明确列出了 N 件事 → 计划就会忠实地产生 N 步**

后一种是唯一能稳定制造「计划需要修正」的形状：探索之后才发现，那 N 件事
其实只需要 1 步（或指向的地方根本不对）。这时：

- **关掉重规划**：智能体在系统提示里被反复告知"现在只做第 k 步"，可能去做
  多余的、甚至有害的工作。
- **打开重规划**：第 1 步之后计划被改写，后面的步骤被砍掉或改向。

判定要同时看两栏 —— **通过率**说明做不做得成，**步数与工具调用**说明做得省不省。
重规划的主要收益本来就是"省"。跑法：

    python tests/eval/runner.py --only replan --ab
"""

from __future__ import annotations

from harness import DEBUG, DEEP, EvalTask

__all__ = ["REPLAN_TASKS"]


def _p(problem: str) -> str:
    return (
        f"项目就在当前工作目录。{problem}\n"
        "请让项目自带的测试全部通过。测试文件不要改（判定前会被还原），改动请落在源码上。"
    )


REPLAN_TASKS: tuple[EvalTask, ...] = (
    # ------------------------------------------------------------------
    # 请求点名了三个症状，实际只有一个根因
    # ------------------------------------------------------------------
    EvalTask(
        id="replan/many_symptoms_one_cause",
        category=DEBUG,
        tier=DEEP,
        prompt=_p(
            "有三个测试失败了：test_cart_line、test_invoice_line、test_coupon_price。"
            "这三个都要修好。"
        ),
        sources={
            "shop/__init__.py": "",
            "shop/pricing.py": '''\
"""折扣计算。"""


def apply_discount(price, percent):
    """按百分比打折。"""
    return round(price - price * percent / 100)
''',
            "shop/cart.py": '''\
"""购物车。"""

from shop.pricing import apply_discount


def total_after_discount(prices, percent):
    return sum(apply_discount(price, percent) for price in prices)
''',
            "shop/invoice.py": '''\
"""发票。"""

from shop.pricing import apply_discount


def line_total(price, percent):
    return apply_discount(price, percent)
''',
            "shop/coupon.py": '''\
"""优惠券。"""

from shop.pricing import apply_discount


def price_with_coupon(price, percent):
    return apply_discount(price, percent)
''',
            "shop/report.py": '''\
"""报表（可见测试没有覆盖它）。"""

from shop.pricing import apply_discount


def summary(prices, percent):
    total = sum(apply_discount(price, percent) for price in prices)
    return f"{total}"
''',
        },
        tests={
            "test_shop.py": '''\
import unittest

from shop import cart, coupon, invoice


class ShopTest(unittest.TestCase):
    def test_cart_line(self):
        self.assertEqual(cart.total_after_discount([99.99], 10), 89.99)

    def test_invoice_line(self):
        self.assertEqual(invoice.line_total(19.99, 25), 14.99)

    def test_coupon_price(self):
        self.assertEqual(coupon.price_with_coupon(49.95, 30), 34.96)
''',
        },
        hidden_tests={
            "test_shop_spec.py": '''\
import unittest

from shop import report


class UntestedCallerTest(unittest.TestCase):
    def test_report_shares_the_same_fix(self):
        """改在根因上，这个没被可见测试覆盖的调用方才会跟着对。"""
        self.assertEqual(report.summary([99.99], 10), "89.99")

    def test_keeps_two_decimals(self):
        self.assertEqual(report.summary([19.99], 25), "14.99")
''',
        },
        reference={
            "shop/pricing.py": '''\
"""折扣计算。"""


def apply_discount(price, percent):
    """按百分比打折，保留两位小数。"""
    return round(price * (1 - percent / 100), 2)
''',
        },
    ),
    # ------------------------------------------------------------------
    # 请求列了 5 项待办，其中 3 项其实早就做完了（清单过期）
    # ------------------------------------------------------------------
    EvalTask(
        id="replan/stale_todo_list",
        category=DEBUG,
        tier=DEEP,
        prompt=_p(
            "TODO.md 里列了 5 项待办，请把它们都做掉。"
        ),
        sources={
            "TODO.md": """\
# 待办

- [ ] slugify：把标题转成 URL slug
- [ ] mean：一组数的平均值，空集合返回 None
- [ ] iso_date：把日期格式化成 YYYY-MM-DD
- [ ] is_email：校验邮箱地址
- [ ] thousands：给数字加千分位
""",
            "lib/__init__.py": "",
            "lib/text.py": '''\
"""文本处理。"""

import unicodedata


def slugify(title):
    """转小写、去标点、空白换成连字符。"""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in ascii_only)
    return "-".join(cleaned.lower().split())
''',
            "lib/mathx.py": '''\
"""数学工具。"""


def mean(values):
    """平均值；空集合返回 None。"""
    if not values:
        return None
    return sum(values) / len(values)
''',
            "lib/dates.py": '''\
"""日期工具。"""


def iso_date(year, month, day):
    """格式化成 YYYY-MM-DD。"""
    return f"{year:04d}-{month:02d}-{day:02d}"
''',
            "lib/validate.py": '''\
"""校验。"""


def is_email(text):
    """校验邮箱地址。"""
    raise NotImplementedError("is_email 还没实现")
''',
            "lib/format.py": '''\
"""数字格式化。"""


def thousands(number):
    """给整数加千分位。"""
    raise NotImplementedError("thousands 还没实现")
''',
        },
        tests={
            "test_lib.py": '''\
import unittest

from lib.dates import iso_date
from lib.mathx import mean
from lib.text import slugify


class ExistingBehaviourTest(unittest.TestCase):
    """这三项其实早就做完了 —— 判定它们没有被"重做"坏。"""

    def test_slugify_handles_accents(self):
        self.assertEqual(slugify("Héllo Wörld"), "hello-world")

    def test_slugify_strips_punctuation(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_mean_of_values(self):
        self.assertEqual(mean([1, 2, 3]), 2)

    def test_mean_of_empty(self):
        self.assertIsNone(mean([]))

    def test_iso_date_pads(self):
        self.assertEqual(iso_date(2026, 3, 7), "2026-03-07")
''',
        },
        hidden_tests={
            "test_lib_spec.py": '''\
import unittest

from lib.dates import iso_date
from lib.format import thousands
from lib.mathx import mean
from lib.text import slugify
from lib.validate import is_email


class TodoListTest(unittest.TestCase):
    def test_unfinished_items_now_work(self):
        self.assertTrue(is_email("a@b.com"))
        self.assertFalse(is_email("a@b"))
        self.assertEqual(thousands(1234567), "1,234,567")

    def test_already_done_items_were_not_broken(self):
        self.assertEqual(slugify("Héllo Wörld"), "hello-world")
        self.assertEqual(mean([1, 2, 3]), 2)
        self.assertIsNone(mean([]))
        self.assertEqual(iso_date(2026, 3, 7), "2026-03-07")
''',
        },
        reference={
            "lib/validate.py": '''\
"""校验。"""

import re

_VALID = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[A-Za-z]{2,}$")


def is_email(text):
    """校验邮箱地址。"""
    return bool(_VALID.match(text or ""))
''',
            "lib/format.py": '''\
"""数字格式化。"""


def thousands(number):
    """给整数加千分位。"""
    return f"{number:,}"
''',
        },
    ),
    # ------------------------------------------------------------------
    # 请求点名了一个模块，而它是好的；根因在另一个模块
    # ------------------------------------------------------------------
    EvalTask(
        id="replan/misdirected_request",
        category=DEBUG,
        tier=DEEP,
        prompt=_p("report.py 里的金额算错了，修一下。账单上的数字不对。"),
        sources={
            "billing/__init__.py": "",
            "billing/money.py": '''\
"""金额换算。"""


def to_cents(amount):
    """把元换算成分。"""
    return int(amount * 100)
''',
            "billing/report.py": '''\
"""账单报表。"""

from billing.money import to_cents


def format_amount(amount):
    """把金额渲染成 'X.YZ 元'。"""
    cents = to_cents(amount)
    return f"{cents // 100}.{cents % 100:02d} 元"


def total(amounts):
    cents = sum(to_cents(amount) for amount in amounts)
    return f"{cents // 100}.{cents % 100:02d} 元"
''',
        },
        tests={
            "test_billing.py": '''\
import unittest

from billing.report import format_amount, total


class BillingTest(unittest.TestCase):
    def test_single_amount(self):
        self.assertEqual(format_amount(19.99), "19.99 元")

    def test_total_of_two(self):
        self.assertEqual(total([19.99, 0.01]), "20.00 元")
''',
        },
        hidden_tests={
            "test_billing_spec.py": '''\
import unittest

from billing.money import to_cents
from billing.report import format_amount, total


class MoneyTest(unittest.TestCase):
    def test_rounding_is_not_truncating(self):
        """19.99 * 100 在浮点下是 1998.9999…，截断会丢一分。"""
        self.assertEqual(to_cents(19.99), 1999)

    def test_report_still_works(self):
        self.assertEqual(format_amount(1.01), "1.01 元")
        self.assertEqual(total([1.01, 2.02]), "3.03 元")
''',
        },
        reference={
            "billing/money.py": '''\
"""金额换算。"""


def to_cents(amount):
    """把元换算成分（四舍五入，而不是截断）。"""
    return round(amount * 100)
''',
        },
    ),
)
