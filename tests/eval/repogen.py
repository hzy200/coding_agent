"""确定性仓库生成器：给长程评测任务造一个"几十文件"的真实感项目。

为什么是生成器，而不是 vendor 一个真实仓库
------------------------------------------

`docs/IMPROVEMENT_PLAN.md` 的风险对策写的是「任务源自真实仓库而非自造」。这里
**刻意偏离**，理由要摆明：

| 动机 | 说明 |
|---|---|
| 可复现 | 同一份规格永远生成同一份仓库，不需要把几十个文件钉进库 |
| 零依赖 | 判定不能被第三方的 requirements 绑架（沙箱里没有 pip） |
| **问题位置可控** | 要测的是「在 40 个文件里定位并贯通修改」，问题的位置必须是我们设计的 |
| 仓库体积 | 进库的是几百行生成器，不是几十个文件 |

那条对策真正要防的是「任务太简单」与「任务与实现耦合」。前者由**规模**挡
（30–45 个文件、上千行，定位本身就要花掉 10+ 次工具调用），后者由既有的判定
原则挡（只看外部可观察结果 + 判分前用纯净副本覆盖测试）。这条偏离已写进
`tests/eval/README.md`。

生成器只产"健康"仓库；每个任务用 `_patch()` 注入自己的破坏点或功能缺口。
`_patch()` 要求被替换的片段**恰好出现一次** —— 否则说明模板改了而破坏点没跟着
改，那样「种子必挂」这条不变量会**静默失效**（破坏没注入，种子反而健康，
任务退化成永远通过）。这条断言是整个生成方案的结构性保障。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 规格
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoSpec:
    """一个生成仓库的规格。字段都是内容性的，没有随机性 —— 生成必须可复现。"""

    package: str = "app"
    # 领域名词：决定 handler / util 模块的命名。**必须彼此区分度高** ——
    # search_code 默认只回 50 条（tools/search.py），命名雷同时一次 grep 就被截断，
    # 大仓库的导航价值会归零。
    handlers: tuple[str, ...] = (
        "order_intake",
        "shipment_dispatch",
        "invoice_settlement",
        "return_authorization",
        "stock_replenishment",
        "customer_statement",
        "price_adjustment",
        "reconciliation",
        "discount_approval",
        "inventory_audit",
        "carrier_booking",
        "loyalty_accrual",
    )
    utils: tuple[str, ...] = (
        "money_format",
        "identifier_alloc",
        "calendar_windows",
        "text_slugging",
        "collection_grouping",
        "pagination_bounds",
        "percent_split",
        "retry_backoff",
        "validation_regex",
    )
    items: tuple[tuple[str, str, int], ...] = (
        ("SKU-1001", "钢制书立", 1299),
        ("SKU-1002", "陶瓷马克杯", 2450),
        ("SKU-1003", "亚麻围裙", 3890),
        ("SKU-1004", "橡木砧板", 7990),
        ("SKU-1005", "玻璃收纳罐", 1590),
    )


@dataclass(slots=True)
class RepoFiles:
    """生成的仓库：相对路径 → 内容。`field` 让每个实例各持一份，不共享。"""

    files: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, path: str) -> str:
        return self.files[path]

    def __setitem__(self, path: str, content: str) -> None:
        self.files[path] = content

    def patch(self, path: str, old: str, new: str) -> None:
        """把 `path` 里的一段精确替换掉；`old` 必须恰好出现一次。

        出现 0 次 → 模板改了而破坏点没跟着改，必须当场炸：否则种子会是健康的，
        而「种子必挂」失效是**静默**的，任务会退化成永远通过。
        出现多次 → 替换哪个不确定，同样不能猜。
        """
        content = self.files[path]
        count = content.count(old)
        if count != 1:
            raise AssertionError(
                f"补丁锚点在 {path} 里出现了 {count} 次（要求恰好 1 次）：{old[:60]!r}"
            )
        self.files[path] = content.replace(old, new)


# --------------------------------------------------------------------------
# 模板
# --------------------------------------------------------------------------

_MODELS = '''\
"""领域模型。

金额一律用**分**存整数：浮点在做累加与取整时会漂，而账目对不上是最难查的一类
问题。展示层负责换算（见 util/money_format.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    """可售商品。"""

    sku: str
    name: str
    unit_price_cents: int
    stock: int = 0

    def price_for(self, quantity: int) -> int:
        return self.unit_price_cents * quantity


@dataclass
class OrderLine:
    """订单里的一行。下单时会把当时的单价**快照**下来，之后改价不影响已下的单。"""

    sku: str
    quantity: int
    unit_price_cents: int

    def total_cents(self) -> int:
        return self.unit_price_cents * self.quantity


@dataclass
class Order:
    """一张订单。"""

    order_id: str
    customer: str
    lines: list[OrderLine] = field(default_factory=list)
    status: str = "draft"
    note: str = ""

    def total_cents(self) -> int:
        return sum(line.total_cents() for line in self.lines)

    def contains(self, sku: str) -> bool:
        return any(line.sku == sku for line in self.lines)


@dataclass
class StockMovement:
    """库存变动流水。审计与对账都靠它。"""

    sku: str
    delta: int
    reason: str = ""
    order_id: str = ""
'''

_CONFIG = '''\
"""全局常量。

状态用常量而不是散落的字面量：`"shipped"` 写错成 `"shiped"` 时不会报错，
只会让按状态聚合的报表**悄悄少掉一批单子**。
"""

from __future__ import annotations

STATUS_DRAFT = "draft"
STATUS_PENDING = "pending"
STATUS_SHIPPED = "shipped"
STATUS_CANCELLED = "cancelled"

OPEN_STATUSES = (STATUS_DRAFT, STATUS_PENDING)
CLOSED_STATUSES = (STATUS_SHIPPED, STATUS_CANCELLED)
# 计入营收的状态。**与"已关闭"不是一回事**：已关闭包含取消，而取消的单子
# 不该出现在成交额里 —— 混用会让营收多算一笔取消单。
REVENUE_STATUSES = (STATUS_SHIPPED,)

TAX_RATE_BPS = 600
MAX_LINES_PER_ORDER = 50
MAX_QUANTITY_PER_LINE = 999
'''

_STORAGE = '''\
"""内存仓储。

真实项目这里是数据库；评测里用字典，因为判定只看外部可观察结果，
不该被"装不装得上某个驱动"绑架。
"""

from __future__ import annotations

from {package}.models import Item, Order, StockMovement


class Repository:
    """全部状态的持有者。"""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {{}}
        self.items: dict[str, Item] = {{}}
        self.movements: list[StockMovement] = []

    # ---- 商品 ----

    def save_item(self, item: Item) -> None:
        self.items[item.sku] = item

    def get_item(self, sku: str) -> Item | None:
        return self.items.get(sku)

    def all_items(self) -> list[Item]:
        return sorted(self.items.values(), key=lambda item: item.sku)

    # ---- 订单 ----

    def add_order(self, order: Order) -> None:
        self.orders[order.order_id] = order

    def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    def all_orders(self) -> list[Order]:
        return sorted(self.orders.values(), key=lambda order: order.order_id)

    def orders_with_status(self, status: str) -> list[Order]:
        return [order for order in self.all_orders() if order.status == status]

    # ---- 库存流水 ----

    def record_movement(self, movement: StockMovement) -> None:
        self.movements.append(movement)

    def movements_for(self, sku: str) -> list[StockMovement]:
        return [movement for movement in self.movements if movement.sku == sku]
'''

_VALIDATION = '''\
"""下单前的校验。返回问题清单而不是抛异常 —— 调用方要能把它们一次性回给用户。"""

from __future__ import annotations

from {package}.config import MAX_LINES_PER_ORDER, MAX_QUANTITY_PER_LINE
from {package}.models import Order


def validate_order(order: Order) -> list[str]:
    problems: list[str] = []
    if not order.customer.strip():
        problems.append("客户名不能为空")
    if not order.lines:
        problems.append("订单至少要有一行")
    if len(order.lines) > MAX_LINES_PER_ORDER:
        problems.append(f"单张订单最多 {{MAX_LINES_PER_ORDER}} 行")
    for line in order.lines:
        if line.quantity <= 0:
            problems.append(f"{{line.sku}} 的数量必须为正")
        elif line.quantity > MAX_QUANTITY_PER_LINE:
            problems.append(f"{{line.sku}} 的数量超过上限 {{MAX_QUANTITY_PER_LINE}}")
        if line.unit_price_cents < 0:
            problems.append(f"{{line.sku}} 的单价不能为负")
    return problems
'''

_SERVICE = '''\
"""业务编排：下单、发货、取消，以及库存变动的记账。

服务层是唯一允许改订单状态的地方 —— handler 只做入参适配与结果包装。
这条约束让"状态从哪来"只有一个答案，也正因为如此，绕过它（比如在 handler 里
直接写状态）会立刻表现为报表对不上。
"""

from __future__ import annotations

from {package}.config import STATUS_CANCELLED, STATUS_PENDING, STATUS_SHIPPED
from {package}.models import Order, OrderLine, StockMovement
from {package}.storage import Repository
from {package}.validation import validate_order


class OrderError(Exception):
    """业务规则不满足。"""


class OrderService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    # ---- 下单 ----

    def create_order(self, order_id: str, customer: str, raw_lines) -> Order:
        lines: list[OrderLine] = []
        for sku, quantity in raw_lines:
            item = self.repo.get_item(sku)
            if item is None:
                raise OrderError(f"未知商品：{{sku}}")
            lines.append(
                OrderLine(sku=sku, quantity=int(quantity), unit_price_cents=item.unit_price_cents)
            )

        order = Order(order_id=order_id, customer=customer, lines=lines)
        problems = validate_order(order)
        if problems:
            raise OrderError("；".join(problems))

        order.status = STATUS_PENDING
        self.repo.add_order(order)
        for line in order.lines:
            self.repo.record_movement(
                StockMovement(sku=line.sku, delta=-line.quantity, reason="order", order_id=order_id)
            )
        return order

    # ---- 状态流转 ----

    def ship_order(self, order_id: str) -> Order:
        order = self._require(order_id)
        if order.status != STATUS_PENDING:
            raise OrderError(f"只有待发货的订单能发货，当前是 {{order.status}}")
        order.status = STATUS_SHIPPED
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self._require(order_id)
        if order.status == STATUS_CANCELLED:
            raise OrderError("订单已经是取消状态")
        order.status = STATUS_CANCELLED
        for line in order.lines:
            self.repo.record_movement(
                StockMovement(sku=line.sku, delta=line.quantity, reason="cancel", order_id=order_id)
            )
        return order

    def add_note(self, order_id: str, note: str) -> Order:
        order = self._require(order_id)
        order.note = note
        return order

    def _require(self, order_id: str) -> Order:
        order = self.repo.get_order(order_id)
        if order is None:
            raise OrderError(f"订单不存在：{{order_id}}")
        return order
'''

_REPORTING = '''\
"""报表：按状态、按客户、按日聚合。"""

from __future__ import annotations

from {package}.config import (
    CLOSED_STATUSES,
    OPEN_STATUSES,
    REVENUE_STATUSES,
    TAX_RATE_BPS,
)
from {package}.storage import Repository


def orders_by_status(repo: Repository) -> dict[str, int]:
    counts: dict[str, int] = {{}}
    for order in repo.all_orders():
        counts[order.status] = counts.get(order.status, 0) + 1
    return counts


def open_order_count(repo: Repository) -> int:
    return sum(1 for order in repo.all_orders() if order.status in OPEN_STATUSES)


def closed_order_count(repo: Repository) -> int:
    return sum(1 for order in repo.all_orders() if order.status in CLOSED_STATUSES)


def revenue_cents(repo: Repository) -> int:
    """只算已成交的单子。草稿与**取消的**都不计入。"""
    return sum(
        order.total_cents() for order in repo.all_orders() if order.status in REVENUE_STATUSES
    )


def revenue_with_tax_cents(repo: Repository) -> int:
    gross = revenue_cents(repo)
    return gross + gross * TAX_RATE_BPS // 10_000


def revenue_by_customer(repo: Repository) -> dict[str, int]:
    totals: dict[str, int] = {{}}
    for order in repo.all_orders():
        if order.status not in REVENUE_STATUSES:
            continue
        totals[order.customer] = totals.get(order.customer, 0) + order.total_cents()
    return totals


def stock_on_hand(repo: Repository) -> dict[str, int]:
    """期初库存 + 全部流水 = 现有库存。流水是唯一事实来源。"""
    return {{
        item.sku: item.stock + sum(m.delta for m in repo.movements_for(item.sku))
        for item in repo.all_items()
    }}


def daily_summary(repo: Repository) -> dict[str, object]:
    return {{
        "by_status": orders_by_status(repo),
        "open": open_order_count(repo),
        "closed": closed_order_count(repo),
        "revenue_cents": revenue_cents(repo),
        "revenue_with_tax_cents": revenue_with_tax_cents(repo),
        "stock": stock_on_hand(repo),
    }}
'''

_HANDLER = '''\
"""{title}。

handler 只做两件事：把入参整理成服务层要的形状、把结果包装成可返回的字典。
它**不碰订单状态** —— 状态流转归服务层，绕过它会让报表与流水对不上。
"""

from __future__ import annotations

from {package}.service import OrderError, OrderService
{imports}

def {func}(service: OrderService, payload: dict) -> dict:
    """{doc}"""
    try:
        result = _run(service, payload)
    except OrderError as exc:
        return {{"ok": False, "error": str(exc)}}
    return {{"ok": True, "data": result}}


def _run(service: OrderService, payload: dict):
{body}
'''

_UTIL = '''\
"""{title}"""

from __future__ import annotations

{body}
'''

_PRICING = '''\
"""定价：折扣、税、以及整数分摊。

全部用整数分运算，并且**余数显式落到第一份**（`percent_split`）—— 浮点分摊
会让"明细加起来不等于合计"，那是最难查的一类对账问题。
"""

from __future__ import annotations

from {package}.config import TAX_RATE_BPS


def apply_discount_bps(cents: int, discount_bps: int) -> int:
    """按基点打折。基点 = 万分之一，1200 表示 12%。"""
    if discount_bps < 0:
        raise ValueError("折扣基点不能为负")
    if discount_bps > 10_000:
        raise ValueError("折扣不能超过 100%")
    return cents - cents * discount_bps // 10_000


def add_tax_cents(cents: int) -> int:
    return cents + cents * TAX_RATE_BPS // 10_000


def percent_split(total_cents: int, weights: list[int]) -> list[int]:
    """按权重把金额分摊成整数份，**保证各份之和恰好等于 total**。

    余数按最大余数法分配；不做这一步的话 `sum(parts) != total`，
    对账时会出现一分钱的缺口。
    """
    if not weights or any(weight < 0 for weight in weights):
        raise ValueError("权重必须非负且非空")
    denominator = sum(weights)
    if denominator == 0:
        return [0 for _ in weights]

    raw = [total_cents * weight for weight in weights]
    parts = [value // denominator for value in raw]
    remainders = sorted(
        range(len(weights)), key=lambda index: (-(raw[index] % denominator), index)
    )
    shortfall = total_cents - sum(parts)
    for index in remainders[:shortfall]:
        parts[index] += 1
    return parts
'''

_EVENTS = '''\
"""领域事件日志。

与订单解耦：报表、审计、通知都订阅它，而不是各自去翻仓储。
写在服务层（状态变更的地方），这样"状态改了但没记事件"不可能发生。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DomainEvent:
    kind: str
    order_id: str
    detail: str = ""


@dataclass
class EventLog:
    events: list[DomainEvent] = field(default_factory=list)

    def record(self, kind: str, order_id: str, detail: str = "") -> DomainEvent:
        event = DomainEvent(kind=kind, order_id=order_id, detail=detail)
        self.events.append(event)
        return event

    def kinds_for(self, order_id: str) -> list[str]:
        return [event.kind for event in self.events if event.order_id == order_id]

    def count_of(self, kind: str) -> int:
        return sum(1 for event in self.events if event.kind == kind)
'''

_ADAPTER = '''\
"""{title}"""

from __future__ import annotations

{body}
'''

_ADAPTERS: dict[str, tuple[str, str]] = {
    "json_payloads": (
        "外部 JSON 载荷的收窄与展开",
        '''\
def order_lines_from(payload: dict) -> list[tuple[str, int]]:
    """把外部载荷里的行项目收窄成 (sku, 数量) 列表。

    **只接受这两种键名**：外部系统偶尔会送 `qty` / `amount` 这类别名，
    静默接受它们会让数量算错而不报错，所以这里显式拒绝。
    """
    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise ValueError("lines 必须是列表")
    narrowed: list[tuple[str, int]] = []
    for line in lines:
        if not isinstance(line, dict) or "sku" not in line or "quantity" not in line:
            raise ValueError(f"行项目缺少 sku/quantity：{line!r}")
        narrowed.append((str(line["sku"]), int(line["quantity"])))
    return narrowed


def envelope(ok: bool, data=None, error: str = "") -> dict:
    """统一响应外壳。"""
    if ok:
        return {"ok": True, "data": data}
    return {"ok": False, "error": error}
''',
    ),
    "csv_export": (
        "把报表导出成 CSV 文本",
        '''\
def rows_to_csv(rows: list[list], header: list[str] | None = None) -> str:
    """把二维数据渲染成 CSV。字段里出现逗号或引号时按 RFC 4180 加引号。"""
    out: list[str] = []
    if header:
        out.append(",".join(_escape(cell) for cell in header))
    for row in rows:
        out.append(",".join(_escape(cell) for cell in row))
    return "\\n".join(out) + "\\n"


def _escape(cell) -> str:
    text = "" if cell is None else str(cell)
    if any(char in text for char in (",", '"', "\\n")):
        return '"' + text.replace('"', '""') + '"'
    return text
''',
    ),
    "http_stub": (
        "一个最小的请求/响应模型，给上层测试用（不做真实网络）",
        '''\
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Request:
    path: str
    method: str = "POST"
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def not_found(what: str) -> Response:
    return Response(status=404, body={"error": f"{what} 不存在"})


def bad_request(reason: str) -> Response:
    return Response(status=400, body={"error": reason})
''',
    ),
}

_TEST_CORE = '''\
"""核心模块的回归测试（模型 / 仓储 / 工具）。

**这一份在任务的任何变体下都必须是绿的** —— 它只覆盖与任务无关的那部分，
所以"种子态测试是红的"这件事只可能来自任务自己引入的破坏点。
生成器改动后请重跑 tests/unit/test_eval_harness.py 里的对应断言。
"""

from __future__ import annotations

import unittest

from {package}.models import Item, OrderLine
from {package}.storage import Repository
from {package}.util.money_format import format_cents, parse_cents
from {package}.util.collection_grouping import group_by
from {package}.util.pagination_bounds import page_bounds
from {package}.util.text_slugging import slugify


class MoneyTest(unittest.TestCase):
    def test_format_and_parse_round_trip(self):
        self.assertEqual(format_cents(1299), "12.99")
        self.assertEqual(parse_cents("12.99"), 1299)

    def test_negative_amounts(self):
        self.assertEqual(format_cents(-500), "-5.00")


class GroupingTest(unittest.TestCase):
    def test_groups_by_key(self):
        self.assertEqual(
            group_by([1, 2, 3, 4], lambda value: value % 2),
            {{1: [1, 3], 0: [2, 4]}},
        )


class PaginationTest(unittest.TestCase):
    def test_page_bounds(self):
        self.assertEqual(page_bounds(total=100, page=1, size=10), (0, 10))
        self.assertEqual(page_bounds(total=100, page=3, size=10), (20, 30))

    def test_page_beyond_the_end_is_empty(self):
        self.assertEqual(page_bounds(total=5, page=9, size=10), (5, 5))


class SlugTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Hello World"), "hello-world")
        self.assertEqual(slugify("  spaced  out "), "spaced-out")


class OrderLineTest(unittest.TestCase):
    def test_line_total(self):
        line = OrderLine(sku="SKU-1", quantity=3, unit_price_cents=250)
        self.assertEqual(line.total_cents(), 750)


class RepositoryTest(unittest.TestCase):
    def test_items_are_listed_in_sku_order(self):
        repo = Repository()
        repo.save_item(Item(sku="SKU-2", name="b", unit_price_cents=1))
        repo.save_item(Item(sku="SKU-1", name="a", unit_price_cents=1))
        self.assertEqual([item.sku for item in repo.all_items()], ["SKU-1", "SKU-2"])
'''

# util 模块：每个都是"真实但与本任务无关"的小工具。命名各不相同，
# 免得 search_code 一次 grep 就被上限截断。
_UTILS: dict[str, str] = {
    "money_format": '''\
def format_cents(cents: int) -> str:
    """把分格式化成两位小数的字符串。"""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_cents(text: str) -> int:
    """把 "12.99" 解析成分。只接受两位小数。"""
    text = text.strip()
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if "." not in text:
        return -int(text) * 100 if negative else int(text) * 100
    whole, _, frac = text.partition(".")
    if len(frac) != 2:
        raise ValueError(f"小数位必须恰好两位：{text}")
    value = int(whole) * 100 + int(frac)
    return -value if negative else value
''',
    "identifier_alloc": '''\
def order_id_for(sequence: int, prefix: str = "ORD") -> str:
    """分配订单号。补零到 6 位，保证字典序与数值序一致。"""
    if sequence <= 0:
        raise ValueError("序号必须为正")
    return f"{prefix}-{sequence:06d}"


def customer_ref(name: str) -> str:
    """客户引用号：取每个词的首字母大写。"""
    parts = [part for part in name.replace("-", " ").split() if part]
    return "".join(part[0].upper() for part in parts) or "?"
''',
    "calendar_windows": '''\
def month_window(year: int, month: int) -> tuple[int, int]:
    """返回该月的 [起, 止) 日序（以一年中的第几天计）。"""
    lengths = [31, 29 if _leap(year) else 28] + [31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    start = sum(lengths[: month - 1])
    return start, start + lengths[month - 1]


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
''',
    "text_slugging": '''\
def slugify(text: str) -> str:
    """转成小写连字符形式，折叠多余空白。"""
    return "-".join(part.lower() for part in text.split())


def truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
''',
    "collection_grouping": '''\
def group_by(values, key):
    """按 key 函数分组，保持首次出现的顺序。"""
    grouped: dict = {}
    for value in values:
        grouped.setdefault(key(value), []).append(value)
    return grouped


def chunk(values: list, size: int) -> list[list]:
    if size <= 0:
        raise ValueError("分块大小必须为正")
    return [values[index : index + size] for index in range(0, len(values), size)]
''',
    "pagination_bounds": '''\
def page_bounds(total: int, page: int, size: int) -> tuple[int, int]:
    """返回该页的 [起, 止) 下标。越界页返回空区间而不是报错。"""
    if page < 1 or size < 1:
        raise ValueError("页码与每页条数都必须为正")
    start = min((page - 1) * size, total)
    return start, min(start + size, total)


def page_count(total: int, size: int) -> int:
    if size < 1:
        raise ValueError("每页条数必须为正")
    return (total + size - 1) // size
''',
    "percent_split": '''\
def remainder_split(total: int, parts: int) -> list[int]:
    """把整数均分成 parts 份，余数落在前面几份。各份之和恒等于 total。"""
    if parts < 1:
        raise ValueError("份数必须为正")
    base, remainder = divmod(total, parts)
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def clamp(value: int, low: int, high: int) -> int:
    if low > high:
        raise ValueError("下界不能大于上界")
    return max(low, min(high, value))
''',
    "retry_backoff": '''\
def backoff_seconds(attempt: int, base: int = 1, cap: int = 60) -> int:
    """指数退避。attempt 从 1 开始。"""
    if attempt < 1:
        raise ValueError("尝试次数从 1 开始")
    if base < 1:
        raise ValueError("基数必须为正")
    return min(base * 2 ** (attempt - 1), cap)


def should_retry(attempt: int, limit: int) -> bool:
    return attempt < limit
''',
    "validation_regex": '''\
import re

SKU_PATTERN = re.compile(r"^SKU-\\d{4}$")
ORDER_ID_PATTERN = re.compile(r"^ORD-\\d{6}$")


def is_valid_sku(sku: str) -> bool:
    return bool(SKU_PATTERN.match(sku))


def is_valid_order_id(order_id: str) -> bool:
    return bool(ORDER_ID_PATTERN.match(order_id))
''',
}

# handler 主题 → HandlerSpec。函数体**每一行都已带 4 空格缩进**
# （模板不再补缩进，补了会叠加成 8 格 → IndentationError）。
@dataclass(frozen=True, slots=True)
class HandlerSpec:
    func: str
    title: str
    doc: str
    imports: str
    body: str


_HANDLERS: dict[str, HandlerSpec] = {
    "order_intake": HandlerSpec(
        func="handle_order_intake",
        title="订单录入入口",
        doc="把一笔新订单写进仓储。",
        imports="",
        body=(
            '    lines = [(line["sku"], line["quantity"]) for line in payload.get("lines", [])]\n'
            '    order = service.create_order(payload["order_id"], payload["customer"], lines)\n'
            '    return {"order_id": order.order_id, "total_cents": order.total_cents()}'
        ),
    ),
    "shipment_dispatch": HandlerSpec(
        func="handle_shipment_dispatch",
        title="发货处理",
        doc="把待发货订单推进到已发货。",
        imports="",
        body=(
            '    order = service.ship_order(payload["order_id"])\n'
            '    return {"order_id": order.order_id, "status": order.status}'
        ),
    ),
    "invoice_settlement": HandlerSpec(
        func="handle_invoice_settlement",
        title="开票结算",
        doc="按客户汇总应收。",
        imports="from {package}.reporting import revenue_by_customer\n",
        body=(
            "    totals = revenue_by_customer(service.repo)\n"
            '    customer = payload["customer"]\n'
            '    return {"customer": customer, "due_cents": totals.get(customer, 0)}'
        ),
    ),
    "return_authorization": HandlerSpec(
        func="handle_return_authorization",
        title="退货受理",
        doc="取消订单并回补库存。",
        imports="",
        body=(
            '    order = service.cancel_order(payload["order_id"])\n'
            '    return {"order_id": order.order_id, "status": order.status}'
        ),
    ),
    "stock_replenishment": HandlerSpec(
        func="handle_stock_replenishment",
        title="补货",
        doc="把进货数量记进库存流水。",
        imports=(
            "from {package}.models import StockMovement\n"
            "from {package}.reporting import stock_on_hand\n"
        ),
        body=(
            '    for sku, quantity in payload.get("arrivals", []):\n'
            "        service.repo.record_movement(\n"
            '            StockMovement(sku=sku, delta=int(quantity), reason="restock")\n'
            "        )\n"
            '    return {"stock": stock_on_hand(service.repo)}'
        ),
    ),
    "customer_statement": HandlerSpec(
        func="handle_customer_statement",
        title="客户对账单",
        doc="给出该客户的订单明细。",
        imports="",
        body=(
            '    customer = payload["customer"]\n'
            "    order_ids = [\n"
            "        order.order_id\n"
            "        for order in service.repo.all_orders()\n"
            "        if order.customer == customer\n"
            "    ]\n"
            '    return {"customer": customer, "orders": order_ids}'
        ),
    ),
    "price_adjustment": HandlerSpec(
        func="handle_price_adjustment",
        title="调价",
        doc="改商品单价。不动已下订单里的单价快照。",
        imports="",
        body=(
            '    item = service.repo.get_item(payload["sku"])\n'
            "    if item is None:\n"
            '        return {"error": "未知商品"}\n'
            '    item.unit_price_cents = int(payload["unit_price_cents"])\n'
            '    return {"sku": item.sku, "unit_price_cents": item.unit_price_cents}'
        ),
    ),
    "reconciliation": HandlerSpec(
        func="handle_reconciliation",
        title="对账",
        doc="给出当日汇总，供对账使用。",
        imports="from {package}.reporting import daily_summary\n",
        body="    return daily_summary(service.repo)",
    ),
    "discount_approval": HandlerSpec(
        func="handle_discount_approval",
        title="折扣审批",
        doc="算出一笔订单折后应收。",
        imports=(
            "from {package}.pricing import apply_discount_bps\n"
            "from {package}.service import OrderError\n"
        ),
        body=(
            '    order = service.repo.get_order(payload["order_id"])\n'
            "    if order is None:\n"
            '        raise OrderError("订单不存在")\n'
            "    gross = order.total_cents()\n"
            "    discounted = apply_discount_bps(gross, int(payload.get(\"discount_bps\", 0)))\n"
            '    return {"gross_cents": gross, "net_cents": discounted}'
        ),
    ),
    "inventory_audit": HandlerSpec(
        func="handle_inventory_audit",
        title="库存盘点",
        doc="给出每个 SKU 的现有库存与流水条数。",
        imports="from {package}.reporting import stock_on_hand\n",
        body=(
            "    on_hand = stock_on_hand(service.repo)\n"
            "    return {\n"
            '        "on_hand": on_hand,\n'
            '        "movement_counts": {item.sku: len(service.repo.movements_for(item.sku))\n'
            "                            for item in service.repo.all_items()},\n"
            "    }"
        ),
    ),
    "carrier_booking": HandlerSpec(
        func="handle_carrier_booking",
        title="承运预约",
        doc="发货并记录承运商。",
        imports="",
        body=(
            '    order = service.ship_order(payload["order_id"])\n'
            "    service.add_note(order.order_id, str(payload.get(\"carrier\", \"\")))\n"
            '    return {"order_id": order.order_id, "carrier": order.note}'
        ),
    ),
    "loyalty_accrual": HandlerSpec(
        func="handle_loyalty_accrual",
        title="积分累计",
        doc="按已成交金额给客户累计积分（每元一分）。",
        imports="from {package}.reporting import revenue_by_customer\n",
        body=(
            "    totals = revenue_by_customer(service.repo)\n"
            '    customer = payload["customer"]\n'
            "    points = totals.get(customer, 0) // 100\n"
            '    return {"customer": customer, "points": points}'
        ),
    ),
}


def _handler_module(spec: RepoSpec, topic: str) -> str:
    handler = _HANDLERS[topic]
    # imports 里带 `{{package}}`：它要经过一次 .format，所以先在这里展开
    imports = handler.imports.format(package=spec.package) if handler.imports else ""
    return _HANDLER.format(
        package=spec.package,
        func=handler.func,
        title=handler.title,
        doc=handler.doc,
        imports=imports,
        body=handler.body,
    )


def _util_module(spec: RepoSpec, topic: str) -> str:
    return _UTIL.format(title=topic.replace("_", " ").capitalize(), body=_UTILS[topic])


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

HEALTHY = "healthy"


def build_repo(spec: RepoSpec | None = None) -> RepoFiles:
    """生成一份**健康**仓库：全部测试通过、功能完整。

    破坏点与功能缺口由任务侧用 `RepoFiles.patch()` 注入 —— 生成器不关心任务。
    """
    spec = spec or RepoSpec()
    package = spec.package
    files = RepoFiles()

    files[f"{package}/__init__.py"] = '"""示例订单服务。"""\n'
    files[f"{package}/models.py"] = _MODELS
    files[f"{package}/config.py"] = _CONFIG
    files[f"{package}/storage.py"] = _STORAGE.format(package=package)
    files[f"{package}/validation.py"] = _VALIDATION.format(package=package)
    files[f"{package}/events.py"] = _EVENTS
    files[f"{package}/pricing.py"] = _PRICING.format(package=package)
    files[f"{package}/service.py"] = _SERVICE.format(package=package)
    files[f"{package}/reporting.py"] = _REPORTING.format(package=package)

    files[f"{package}/handlers/__init__.py"] = '"""入参适配层。"""\n'
    for topic in spec.handlers:
        files[f"{package}/handlers/{topic}.py"] = _handler_module(spec, topic)

    files[f"{package}/util/__init__.py"] = '"""与领域无关的小工具。"""\n'
    for topic in spec.utils:
        files[f"{package}/util/{topic}.py"] = _util_module(spec, topic)

    files[f"{package}/adapters/__init__.py"] = '"""外部载荷与导出格式的适配。"""\n'
    for topic, (title, body) in _ADAPTERS.items():
        files[f"{package}/adapters/{topic}.py"] = _ADAPTER.format(title=title, body=body)

    # 判定按模块名导入，所以 tests/ 必须是包
    files["tests/__init__.py"] = ""
    files["tests/test_core.py"] = _TEST_CORE.format(package=package)
    return files


def seed_items(spec: RepoSpec | None = None) -> list[tuple[str, str, int, int]]:
    """给测试用的期初商品：(sku, 名称, 单价分, 期初库存)。"""
    spec = spec or RepoSpec()
    return [(sku, name, price, 100) for sku, name, price in spec.items]


def file_stats(files: RepoFiles) -> dict[str, int]:
    """生成物的规模（给单测断言"确实是一个量级"用）。"""
    return {
        "files": len(files.files),
        "lines": sum(content.count("\n") for content in files.files.values()),
    }


# --------------------------------------------------------------------------
# 功能增量
# --------------------------------------------------------------------------
#
# 长程任务要的是「一个功能缺在各层之间」，而不是「某处拼错一个字符串」——
# 后者一两行就改完了，量不出长程。所以这里的功能都是**贯通式**的：少做一层
# （比如漏了报表层）可见测试就会挂，而要做对必须同时改 6 个文件。
#
# 用 `patch()` 而不是给模板加开关：模板保持单一形态更好读，功能线在一处写全，
# 而 patch 的"锚点必须恰好出现一次"会把"模板改了、功能线没跟着改"当场炸出来。

Feature = "Callable[[RepoFiles, str], None]"


def _discount_model_layer(files: RepoFiles, package: str) -> None:
    """折扣的**模型层**：订单记住折扣字段，并能算出折后金额。

    单独拆出来是为了让"功能只做了一半"这个任务形态与完整形态共用同一份补丁 ——
    两份各写一遍必然漂移，而漂移的后果是任务语义悄悄变了。
    """
    files.patch(
        f"{package}/models.py",
        "from dataclasses import dataclass, field\n",
        "from dataclasses import dataclass, field\n"
        "\n"
        f"from {package}.pricing import apply_discount_bps\n",
    )
    files.patch(
        f"{package}/models.py",
        '    status: str = "draft"\n    note: str = ""\n',
        '    status: str = "draft"\n    note: str = ""\n    discount_bps: int = 0\n',
    )
    files.patch(
        f"{package}/models.py",
        "    def contains(self, sku: str) -> bool:\n"
        "        return any(line.sku == sku for line in self.lines)\n",
        "    def contains(self, sku: str) -> bool:\n"
        "        return any(line.sku == sku for line in self.lines)\n"
        "\n"
        "    def net_total_cents(self) -> int:\n"
        '        """折后金额。折扣按整单算，逐行算会因为取整产生分差。"""\n'
        "        return apply_discount_bps(self.total_cents(), self.discount_bps)\n",
    )
    files.patch(
        f"{package}/config.py",
        "MAX_LINES_PER_ORDER = 50\n",
        "MAX_LINES_PER_ORDER = 50\nMAX_DISCOUNT_BPS = 3000\n",
    )


def _discount_wiring_layer(files: RepoFiles, package: str) -> None:
    """折扣的**接线层**：服务接参并校验、两个 handler 各自透传与读取、报表改用折后金额。

    这一层才是"长程"的来源 —— 少任何一处，跨层的可见测试就会挂。
    """
    files.patch(
        f"{package}/service.py",
        f"from {package}.config import STATUS_CANCELLED, STATUS_PENDING, STATUS_SHIPPED\n",
        f"from {package}.config import (\n"
        f"    MAX_DISCOUNT_BPS,\n"
        f"    STATUS_CANCELLED,\n"
        f"    STATUS_PENDING,\n"
        f"    STATUS_SHIPPED,\n"
        f")\n",
    )
    files.patch(
        f"{package}/service.py",
        "    def create_order(self, order_id: str, customer: str, raw_lines) -> Order:\n",
        "    def create_order(\n"
        "        self, order_id: str, customer: str, raw_lines, discount_bps: int = 0\n"
        "    ) -> Order:\n",
    )
    files.patch(
        f"{package}/service.py",
        "        order = Order(order_id=order_id, customer=customer, lines=lines)\n"
        "        problems = validate_order(order)\n",
        "        if not 0 <= int(discount_bps) <= MAX_DISCOUNT_BPS:\n"
        '            raise OrderError(f"折扣超出允许范围（0–{MAX_DISCOUNT_BPS} 基点）")\n'
        "        order = Order(\n"
        "            order_id=order_id,\n"
        "            customer=customer,\n"
        "            lines=lines,\n"
        "            discount_bps=int(discount_bps),\n"
        "        )\n"
        "        problems = validate_order(order)\n",
    )
    files.patch(
        f"{package}/service.py",
        "    def add_note(self, order_id: str, note: str) -> Order:\n",
        "    def set_discount(self, order_id: str, discount_bps: int) -> Order:\n"
        '        """改折扣。同样要过范围校验 —— 绕过它会让报表出现负收入。"""\n'
        "        if not 0 <= int(discount_bps) <= MAX_DISCOUNT_BPS:\n"
        '            raise OrderError("折扣超出允许范围")\n'
        "        order = self._require(order_id)\n"
        "        order.discount_bps = int(discount_bps)\n"
        "        return order\n"
        "\n"
        "    def add_note(self, order_id: str, note: str) -> Order:\n",
    )
    files.patch(
        f"{package}/reporting.py",
        "    return sum(\n"
        "        order.total_cents() for order in repo.all_orders() "
        "if order.status in REVENUE_STATUSES\n"
        "    )\n",
        "    return sum(\n"
        "        order.net_total_cents() for order in repo.all_orders() "
        "if order.status in REVENUE_STATUSES\n"
        "    )\n",
    )
    files.patch(
        f"{package}/reporting.py",
        "        totals[order.customer] = totals.get(order.customer, 0) + order.total_cents()\n",
        "        totals[order.customer] = (\n"
        "            totals.get(order.customer, 0) + order.net_total_cents()\n"
        "        )\n",
    )
    files.patch(
        f"{package}/handlers/order_intake.py",
        '    order = service.create_order(payload["order_id"], payload["customer"], lines)\n',
        "    order = service.create_order(\n"
        '        payload["order_id"],\n'
        '        payload["customer"],\n'
        "        lines,\n"
        '        discount_bps=int(payload.get("discount_bps", 0)),\n'
        "    )\n",
    )
    files.patch(
        f"{package}/handlers/discount_approval.py",
        "    gross = order.total_cents()\n"
        '    discounted = apply_discount_bps(gross, int(payload.get("discount_bps", 0)))\n'
        '    return {"gross_cents": gross, "net_cents": discounted}\n',
        "    gross = order.total_cents()\n"
        "    return {\n"
        '        "gross_cents": gross,\n'
        '        "net_cents": order.net_total_cents(),\n'
        '        "discount_bps": order.discount_bps,\n'
        "    }\n",
    )


def _apply_discount(files: RepoFiles, package: str) -> None:
    """折扣**完整**贯通：模型 → 配置 → 服务 → 报表 → 两个 handler。"""
    _discount_model_layer(files, package)
    _discount_wiring_layer(files, package)


def _apply_discount_partial(files: RepoFiles, package: str) -> None:
    """折扣**只做了一半**：模型记住了字段与折后算法，但没有任何一层用它。

    真实项目里最常见的一种半成品 —— 上一个做的人只写完底层就被叫走了。
    要做对必须自己找出"缺在哪几层"，而不是照着需求从零实现：这是与
    `long/discount_pipeline` 不同的能力（诊断既有代码，不是新建）。
    """
    _discount_model_layer(files, package)


FEATURES: dict[str, object] = {
    "discount": _apply_discount,
    "discount_partial": _apply_discount_partial,
}


def build_variant(
    spec: RepoSpec | None = None, *, features: tuple[str, ...] = ()
) -> RepoFiles:
    """生成仓库并接上指定的功能增量。

    `features=()` 就是**种子形态**（功能缺失）；接上全部功能即参考解形态。
    未知功能名当场报错，不静默忽略 —— 静默忽略会让参考解缺功能，
    而"参考解也过不了"会表现为**任务不可满足**，很难查。
    """
    spec = spec or RepoSpec()
    files = build_repo(spec)
    for name in features:
        apply = FEATURES.get(name)
        if apply is None:
            raise KeyError(f"未知的功能增量：{name}（可用：{sorted(FEATURES)}）")
        apply(files, spec.package)  # type: ignore[operator]
    return files


def sources_only(files: RepoFiles) -> dict[str, str]:
    """只要源码（去掉 tests/）—— `EvalTask.sources` 不该包含测试文件。"""
    return {path: content for path, content in files.files.items() if not path.startswith("tests/")}


def tests_only(files: RepoFiles) -> dict[str, str]:
    return {path: content for path, content in files.files.items() if path.startswith("tests/")}


def changed_sources(seed: RepoFiles, target: RepoFiles) -> dict[str, str]:
    """从种子到目标之间**改动过的源码文件**（不含 tests/）—— 即 `EvalTask.reference`。

    参考解只列改动过的文件：全量塞进去会让任务定义比仓库还大，而且掩盖了
    "这次到底要改哪些地方"这个本身就有信息量的事实。
    """
    return {
        path: content
        for path, content in target.files.items()
        if not path.startswith("tests/") and seed.files.get(path) != content
    }


def repo_tests(files: RepoFiles) -> dict[str, str]:
    return tests_only(files)


# 事件类型常量放在 events.py 而不是 config.py：它们属于事件层，
# 而且这样 audit 这个功能线不会与折扣那条抢同一处 config 锚点。
_EVENTS_KINDS = '''
# 事件类型常量。散落的字面量在多处订阅时会对不上（"order_placed" vs "order:placed"）。
ORDER_PLACED = "order_placed"
ORDER_SHIPPED = "order_shipped"
ORDER_CANCELLED = "order_cancelled"
'''


def _apply_audit(files: RepoFiles, package: str) -> None:
    """事件审计贯通：把已经是**死代码**的 `EventLog` 接进服务层与对账接口。

    这个形态测的是"发现既有但没人用的设施，并把它接对"——与从零实现不同。
    """
    files.patch(
        f"{package}/events.py",
        "@dataclass(frozen=True, slots=True)\nclass DomainEvent:",
        _EVENTS_KINDS.strip() + "\n\n\n@dataclass(frozen=True, slots=True)\nclass DomainEvent:",
    )
    files.patch(
        f"{package}/events.py",
        "    def count_of(self, kind: str) -> int:\n"
        "        return sum(1 for event in self.events if event.kind == kind)\n",
        "    def count_of(self, kind: str) -> int:\n"
        "        return sum(1 for event in self.events if event.kind == kind)\n"
        "\n"
        "    def counts(self) -> dict[str, int]:\n"
        '        """按类型计数，给对账用。"""\n'
        "        summary: dict[str, int] = {}\n"
        "        for event in self.events:\n"
        "            summary[event.kind] = summary.get(event.kind, 0) + 1\n"
        "        return summary\n",
    )
    files.patch(
        f"{package}/service.py",
        f"from {package}.models import Order, OrderLine, StockMovement\n",
        f"from {package}.events import (\n"
        f"    ORDER_CANCELLED,\n"
        f"    ORDER_PLACED,\n"
        f"    ORDER_SHIPPED,\n"
        f"    EventLog,\n"
        f")\n"
        f"from {package}.models import Order, OrderLine, StockMovement\n",
    )
    files.patch(
        f"{package}/service.py",
        "    def __init__(self, repo: Repository) -> None:\n        self.repo = repo\n",
        "    def __init__(self, repo: Repository) -> None:\n"
        "        self.repo = repo\n"
        "        self.events = EventLog()\n"
        "\n"
        "    def event_kinds(self, order_id: str) -> list[str]:\n"
        '        """这张订单上发生过什么。审计与对账都读它，而不是各自去猜。"""\n'
        "        return self.events.kinds_for(order_id)\n",
    )
    files.patch(
        f"{package}/service.py",
        "        order.status = STATUS_PENDING\n        self.repo.add_order(order)\n",
        "        order.status = STATUS_PENDING\n"
        "        self.repo.add_order(order)\n"
        "        self.events.record(ORDER_PLACED, order_id)\n",
    )
    files.patch(
        f"{package}/service.py",
        "        order.status = STATUS_SHIPPED\n        return order\n",
        "        order.status = STATUS_SHIPPED\n"
        "        self.events.record(ORDER_SHIPPED, order_id)\n"
        "        return order\n",
    )
    files.patch(
        f"{package}/service.py",
        "        order.status = STATUS_CANCELLED\n",
        "        order.status = STATUS_CANCELLED\n"
        "        self.events.record(ORDER_CANCELLED, order_id)\n",
    )
    files.patch(
        f"{package}/handlers/reconciliation.py",
        "    return daily_summary(service.repo)\n",
        "    summary = daily_summary(service.repo)\n"
        '    summary["events"] = service.events.counts()\n'
        "    return summary\n",
    )


FEATURES["audit"] = _apply_audit
