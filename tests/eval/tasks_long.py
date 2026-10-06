"""评测任务集 · 长程档：在**几十个文件**的仓库里做贯通式修改。

为什么要有这一档
----------------

前两档（basic 20 个、deep 9 个）的工作区是 1–9 个文件、几十行，实测两轮跑出
52% / 83% —— 单任务 15–19s、7 次工具调用，`tool_calls` 预算（60）只用了 10–25%。
那种规模量不出"自主性"：模型不需要探索，也不需要规划，一眼就能定位。

这一档按 `docs/IMPROVEMENT_PLAN.md` §3.2 W2 的要求提一个量级：**38 个文件、
近千行**的仓库，任务要求把**一个功能贯通到各层** —— 少做一层（比如漏了报表）
可见测试就会挂。要定位"折扣该在哪几层之间传"本身就要花掉十几次工具调用。

形态
----

- `long/discount_pipeline`：**可见测试描述了要求**（种子态是红的）。改对要同时动
  6 个文件：模型加字段、配置加上限、服务接参并校验、报表改用折后金额、
  两个 handler 各自透传与读取。
- `long/discount_spec`：**可见测试全绿、需求未满足**。判据在隐藏测试里，
  prompt 不点名文件也不点名测试 —— 测的是"会不会读需求，而不是只盯着绿化测试"。

判定为什么显式列用例，而不是 `unittest discover`
------------------------------------------------

几十个文件、多层包的仓库上，`discover` 从根目录递归收集会踩一串问题：同名
basename 冲突、子目录可导入性、收集顺序，以及最要命的 —— **智能体写在
`tests/` 里的临时测试会被一并收进来**（`harness.py` 的 `prepare_for_judging`
只删根级 `test_*.py`）。让判定只跑我们指定的那个模块，这一整类风险就**结构性地
消失**了：智能体再多写几个测试文件也影响不到判定。代价是判定命令不再统一，
由 `test_eval_harness.py` 单独守着（零依赖、不含 pytest、必须指向 tests/）。
"""

from __future__ import annotations

from harness import CROSS_FILE, LONG, SPEC, EvalTask
from repogen import RepoSpec, build_variant, changed_sources, sources_only

__all__ = ["LONG_TASKS"]

SPEC_SPEC = RepoSpec()

# 种子 = 功能缺失；参考解 = 功能贯通。两者只差这一个开关，
# 所以"改了什么"完全由 `changed_sources` 算出来，不靠人手抄。
_SEED = build_variant(SPEC_SPEC)
_REFERENCE = build_variant(SPEC_SPEC, features=("discount",))
_REFERENCE_SOURCES = changed_sources(_SEED, _REFERENCE)
_SEED_SOURCES = sources_only(_SEED)
_SEED_TESTS = {path: content for path, content in _SEED.files.items() if path.startswith("tests/")}


def _pipeline_prompt() -> str:
    return (
        "项目就在当前工作目录，是一个订单服务（三十多个文件，分模型、仓储、服务、"
        "报表、handler、util、adapters 几层）。\n"
        "现在要支持**整单折扣**：下单时可以带 `discount_bps`（基点，1000 = 10%），"
        "折扣要按整单算而不是逐行算（逐行取整会产生分差）。\n"
        "要求：\n"
        "1. 下单入口透传折扣；\n"
        "2. 报表里的成交额按**折后**金额算（按客户汇总的也一样）；\n"
        "3. 折扣审批接口报出该订单**实际记住的**折扣与折后金额；\n"
        "4. 超过上限（3000 基点）的折扣要拒绝。\n"
        "项目自带的测试已经描述了这些要求，请让它们全部通过。"
        "测试文件不要改（判定前会被还原），改动请落在源码上。"
    )


def _spec_prompt() -> str:
    return (
        "项目就在当前工作目录，是一个订单服务（三十多个文件，分好几层）。\n"
        "运营反馈：折扣这块儿对不上账。需求是支持**整单折扣** —— 下单时可以带 "
        "`discount_bps`（基点，1000 = 10%），折扣按整单算（逐行取整会有分差），"
        "报表里的成交额必须反映折扣，超过 3000 基点的折扣要拒绝。\n"
        "注意：项目自带的测试**可能没有覆盖上面的全部要求**。请按需求把行为做对，"
        "不要只满足于让现有测试变绿。"
    )


# --------------------------------------------------------------------------
# 判定用例
# --------------------------------------------------------------------------

# 可见：描述折扣要求 —— 种子态必然是红的
_VISIBLE_DISCOUNT = '''\
"""整单折扣的行为断言。

折扣要贯通到每一层：模型记住它、服务校验它、**报表按折后金额算**、
两个 handler 分别透传与读取。少做任何一层，下面的用例就会挂 —— 这正是
"在几十个文件里找到该改哪几处"要考的。
"""

import unittest

from app.handlers.discount_approval import handle_discount_approval
from app.handlers.order_intake import handle_order_intake
from app.models import Item
from app.reporting import revenue_cents
from app.service import OrderService
from app.storage import Repository

LINES = [{"sku": "SKU-1001", "quantity": 2}]  # 2 × 1000 分 = 2000 分


def _service() -> OrderService:
    repo = Repository()
    repo.save_item(Item(sku="SKU-1001", name="钢制书立", unit_price_cents=1000))
    return OrderService(repo)


def _intake(service: OrderService, discount_bps: int) -> dict:
    return handle_order_intake(
        service,
        {
            "order_id": "ORD-000001",
            "customer": "ACME",
            "lines": LINES,
            "discount_bps": discount_bps,
        },
    )


class DiscountPipelineTest(unittest.TestCase):
    def test_revenue_reflects_the_discount(self):
        """报表必须按折后金额算。只改下单、漏了报表层，这一条就会挂。"""
        service = _service()
        self.assertTrue(_intake(service, 1000)["ok"])
        service.ship_order("ORD-000001")
        self.assertEqual(revenue_cents(service.repo), 1800)

    def test_approval_reports_the_stored_discount(self):
        """审批接口要读订单**记住的**折扣，而不是 payload 里再传一次。"""
        service = _service()
        self.assertTrue(_intake(service, 1500)["ok"])
        result = handle_discount_approval(service, {"order_id": "ORD-000001"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["discount_bps"], 1500)
        self.assertEqual(result["data"]["net_cents"], 1700)

    def test_discount_above_the_cap_is_rejected(self):
        service = _service()
        result = _intake(service, 5000)
        self.assertFalse(result["ok"])

    def test_zero_discount_leaves_the_amount_alone(self):
        service = _service()
        self.assertTrue(_intake(service, 0)["ok"])
        service.ship_order("ORD-000001")
        self.assertEqual(revenue_cents(service.repo), 2000)
'''

# 可见（spec 版）：只描述**现状**，种子态全绿
_VISIBLE_BASELINE = '''\
"""订单服务的现有行为。

这些断言描述的是"现在就是对的"那部分，不应随新需求改变。
"""

import unittest

from app.handlers.order_intake import handle_order_intake
from app.models import Item
from app.reporting import revenue_cents, stock_on_hand
from app.service import OrderService
from app.storage import Repository


def _service() -> OrderService:
    repo = Repository()
    repo.save_item(Item(sku="SKU-1001", name="钢制书立", unit_price_cents=1000, stock=10))
    return OrderService(repo)


class BaselineBehaviourTest(unittest.TestCase):
    def test_plain_order_totals(self):
        service = _service()
        result = handle_order_intake(
            service,
            {
                "order_id": "ORD-000001",
                "customer": "ACME",
                "lines": [{"sku": "SKU-1001", "quantity": 2}],
            },
        )
        self.assertTrue(result["ok"], result)

    def test_shipping_counts_as_revenue(self):
        service = _service()
        handle_order_intake(
            service,
            {
                "order_id": "ORD-000001",
                "customer": "ACME",
                "lines": [{"sku": "SKU-1001", "quantity": 3}],
            },
        )
        service.ship_order("ORD-000001")
        self.assertEqual(revenue_cents(service.repo), 3000)

    def test_cancelled_orders_do_not_count(self):
        service = _service()
        handle_order_intake(
            service,
            {
                "order_id": "ORD-000001",
                "customer": "ACME",
                "lines": [{"sku": "SKU-1001", "quantity": 1}],
            },
        )
        service.cancel_order("ORD-000001")
        self.assertEqual(revenue_cents(service.repo), 0)

    def test_unknown_sku_is_reported(self):
        service = _service()
        result = handle_order_intake(
            service,
            {
                "order_id": "ORD-000001",
                "customer": "ACME",
                "lines": [{"sku": "SKU-9999", "quantity": 1}],
            },
        )
        self.assertFalse(result["ok"])
'''

# 隐藏（spec 版）：断言折扣行为。**从不写进工作区**，只在判分时加入。
_HIDDEN_DISCOUNT = '''\
"""折扣需求的隐藏断言（spec 版）。

可见测试描述的是现状，这些才描述需求。二者一起跑 —— 种子上必然失败。
"""

import unittest

from app.handlers.discount_approval import handle_discount_approval
from app.handlers.order_intake import handle_order_intake
from app.models import Item
from app.reporting import revenue_cents, revenue_by_customer
from app.service import OrderService
from app.storage import Repository

LINES = [{"sku": "SKU-1001", "quantity": 4}]  # 4 × 1000 = 4000 分


def _service() -> OrderService:
    repo = Repository()
    repo.save_item(Item(sku="SKU-1001", name="钢制书立", unit_price_cents=1000))
    return OrderService(repo)


def _intake(service: OrderService, discount_bps: int) -> dict:
    return handle_order_intake(
        service,
        {
            "order_id": "ORD-000001",
            "customer": "ACME",
            "lines": LINES,
            "discount_bps": discount_bps,
        },
    )


class HiddenDiscountTest(unittest.TestCase):
    def test_revenue_and_per_customer_reflect_the_discount(self):
        service = _service()
        _intake(service, 2500)  # 4000 - 25% = 3000
        service.ship_order("ORD-000001")
        self.assertEqual(revenue_cents(service.repo), 3000)
        self.assertEqual(revenue_by_customer(service.repo), {"ACME": 3000})

    def test_approval_reads_the_stored_discount(self):
        service = _service()
        _intake(service, 1000)
        result = handle_discount_approval(service, {"order_id": "ORD-000001"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["net_cents"], 3600)

    def test_cap_is_enforced_on_the_service_path(self):
        """绕过 handler 直接调服务层也要被拦住 —— 校验属于服务层，不属于 handler。"""
        from app.service import OrderError

        service = _service()
        with self.assertRaises(OrderError):
            service.create_order("ORD-000001", "ACME", [("SKU-1001", 1)], discount_bps=5000)
'''

_TEST_INIT = _SEED_TESTS["tests/__init__.py"]
_TEST_CORE = _SEED_TESTS["tests/test_core.py"]

def _base_tests(visible: str) -> dict[str, str]:
    """种子里共有的那两份测试：包标记与核心回归。"""
    return {"tests/__init__.py": _TEST_INIT, "tests/test_core.py": _TEST_CORE}


# 长程任务专用的 agent 预算。**不动全局默认值** —— 改了它会连带改掉现有 29 个
# 任务的行为，那份基线就作废了；而 per-task 覆盖正是为这件事准备的。
#
# 数值来自探路实测（不是拍脑袋）：`long/discount_pipeline` 那次跑出
# **12 次验证全失败、9 次修复、3 次重规划**，也就是说它撞的是修复与重规划的上限。
# 抬预算的目的就是把「被预算卡住」与「真做不成」这两件事分开：抬完仍失败，
# 才是能力信号。所以只加到够分辨为止，不无脑放大 —— 那只是更贵地失败。
#
# 键名必须与 `Settings` 字段一致；写错的话 `model_copy(update=...)` **不会报错**，
# 只会静默地不生效（它有单测守着）。
_LONG_BUDGET: dict[str, int] = {
    "max_plan_steps": 8,
    "max_tool_rounds": 16,
    "max_repair_rounds": 5,
    "max_replans": 3,
}


LONG_TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        id="long/discount_pipeline",
        category=CROSS_FILE,
        tier=LONG,
        prompt=_pipeline_prompt(),
        sources=dict(_SEED_SOURCES),
        tests={**_base_tests(_VISIBLE_DISCOUNT), "tests/test_discount.py": _VISIBLE_DISCOUNT},
        reference=dict(_REFERENCE_SOURCES),
        # 显式列用例：判定只跑这两个模块，智能体自己写的测试影响不到判定
        judge="python3 -m unittest -v tests.test_core tests.test_discount",
        timeout=600,
        budget=dict(_LONG_BUDGET),
    ),
    EvalTask(
        id="long/discount_spec",
        category=SPEC,
        tier=LONG,
        prompt=_spec_prompt(),
        sources=dict(_SEED_SOURCES),
        tests={**_base_tests(_VISIBLE_BASELINE), "tests/test_baseline.py": _VISIBLE_BASELINE},
        hidden_tests={"tests/test_discount_hidden.py": _HIDDEN_DISCOUNT},
        reference=dict(_REFERENCE_SOURCES),
        judge=(
            "python3 -m unittest -v tests.test_core tests.test_baseline "
            "tests.test_discount_hidden"
        ),
        timeout=600,
        budget=dict(_LONG_BUDGET),
    ),
)


# --------------------------------------------------------------------------
# 扩展样本：同一个仓库，不同的任务形态
#
# 探路实测暴露了一个更要紧的问题：**两次运行之间的方差比任何调参的效应都大**
# （同一个任务，一次改了文件、12 次验证全失败；另一次连一次写都没尝试）。
# 2 个任务的样本量下，任何"抬预算 / 改提示"的实验都无法评估。
# 所以先把样本补起来 —— 而且补的是**不同形态**，不是同一个任务的复制：
#
# | 任务 | 种子 | 要改几个文件 | 考的是什么 |
# |---|---|---|---|
# | `discount_pipeline` | 功能全缺 | 6 | 从零贯通 |
# | `discount_partial`  | 只做了模型层 | 4 | **诊断既有代码**：找出缺在哪几层 |
# | `audit_trail`       | EventLog 是死代码 | 3 | 发现既有但没人用的设施并接对 |
# | `discount_and_audit`| 两条都缺 | 8 | 最长的那个 |
# | `discount_spec`     | 功能全缺、可见测试全绿 | 6 | 会不会读需求（不点名文件与测试） |
# --------------------------------------------------------------------------

_PARTIAL_SEED = build_variant(SPEC_SPEC, features=("discount_partial",))
_AUDIT_REFERENCE = build_variant(SPEC_SPEC, features=("audit",))
_BOTH_REFERENCE = build_variant(SPEC_SPEC, features=("discount", "audit"))

_VISIBLE_AUDIT = '''\
"""事件审计的行为断言。

`app/events.py` 里的 `EventLog` 目前是**死代码**：定义了类、写了方法，
但没有任何人用它 —— 所以"订单发生过什么"这件事眼下无处可查。
需求是把它接进服务层，让每次状态变更都留下事件，并让对账接口能报出计数。
"""

import unittest

from app.handlers.order_intake import handle_order_intake
from app.handlers.reconciliation import handle_reconciliation
from app.handlers.return_authorization import handle_return_authorization
from app.handlers.shipment_dispatch import handle_shipment_dispatch
from app.models import Item
from app.service import OrderService
from app.storage import Repository


def _service() -> OrderService:
    repo = Repository()
    repo.save_item(Item(sku="SKU-1001", name="钢制书立", unit_price_cents=1000, stock=10))
    return OrderService(repo)


def _place(service: OrderService, order_id: str = "ORD-000001") -> dict:
    return handle_order_intake(
        service,
        {
            "order_id": order_id,
            "customer": "ACME",
            "lines": [{"sku": "SKU-1001", "quantity": 2}],
        },
    )


class AuditTrailTest(unittest.TestCase):
    def test_placing_an_order_is_recorded(self):
        service = _service()
        self.assertTrue(_place(service)["ok"])
        self.assertEqual(service.event_kinds("ORD-000001"), ["order_placed"])

    def test_shipping_appends_an_event(self):
        service = _service()
        _place(service)
        handle_shipment_dispatch(service, {"order_id": "ORD-000001"})
        self.assertEqual(
            service.event_kinds("ORD-000001"), ["order_placed", "order_shipped"]
        )

    def test_cancelling_appends_an_event(self):
        service = _service()
        _place(service)
        handle_return_authorization(service, {"order_id": "ORD-000001"})
        self.assertEqual(
            service.event_kinds("ORD-000001"), ["order_placed", "order_cancelled"]
        )

    def test_reconciliation_reports_event_counts(self):
        service = _service()
        _place(service)
        handle_shipment_dispatch(service, {"order_id": "ORD-000001"})
        result = handle_reconciliation(service, {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["data"]["events"], {"order_placed": 1, "order_shipped": 1}
        )
'''

LONG_TASKS = LONG_TASKS + (
    EvalTask(
        id="long/discount_partial",
        category=CROSS_FILE,
        tier=LONG,
        prompt=(
            "项目就在当前工作目录，是一个订单服务（三十多个文件，分好几层）。\n"
            "上一个同事开始做「整单折扣」，但**只做完了一半就调走了** —— 请接着把它做完。\n"
            "需求：下单时可以带 `discount_bps`（基点，1000 = 10%），折扣按整单算"
            "（逐行取整会有分差），报表里的成交额按**折后**金额算（含按客户汇总），"
            "折扣审批接口报出该订单实际记住的折扣，超过 3000 基点的折扣要拒绝。\n"
            "项目自带的测试描述了这些要求，请让它们全部通过。"
            "先看清已经做了什么、还缺哪些层，再动手。"
            "测试文件不要改（判定前会被还原），改动请落在源码上。"
        ),
        sources=dict(sources_only(_PARTIAL_SEED)),
        tests={
            "tests/__init__.py": _TEST_INIT,
            "tests/test_core.py": _TEST_CORE,
            "tests/test_discount.py": _VISIBLE_DISCOUNT,
        },
        # 参考解只列「从这个半成品到完整」还要改的部分 —— 已经做过的那两层不在里面
        reference=changed_sources(_PARTIAL_SEED, _REFERENCE),
        judge="python3 -m unittest -v tests.test_core tests.test_discount",
        timeout=600,
        budget=dict(_LONG_BUDGET),
    ),
    EvalTask(
        id="long/audit_trail",
        category=CROSS_FILE,
        tier=LONG,
        prompt=(
            "项目就在当前工作目录，是一个订单服务（三十多个文件，分好几层）。\n"
            "需求：每张订单的状态变更都要留下**事件**，并且对账接口要能报出各类事件的计数。\n"
            "项目自带的测试描述了具体要求，请让它们全部通过。"
            "注意：这个项目里可能已经有现成的设施，先看清楚再决定是自己写还是接上去。"
            "测试文件不要改（判定前会被还原），改动请落在源码上。"
        ),
        sources=dict(_SEED_SOURCES),
        tests={
            "tests/__init__.py": _TEST_INIT,
            "tests/test_core.py": _TEST_CORE,
            "tests/test_audit_trail.py": _VISIBLE_AUDIT,
        },
        reference=changed_sources(_SEED, _AUDIT_REFERENCE),
        judge="python3 -m unittest -v tests.test_core tests.test_audit_trail",
        timeout=600,
        budget=dict(_LONG_BUDGET),
    ),
    EvalTask(
        id="long/discount_and_audit",
        category=CROSS_FILE,
        tier=LONG,
        prompt=(
            "项目就在当前工作目录，是一个订单服务（三十多个文件，分好几层）。\n"
            "两件事一起做：\n"
            "1. **整单折扣**：下单时带 `discount_bps`（基点，1000 = 10%），按整单算"
            "（逐行取整会有分差），报表成交额按折后算，审批接口报出记住的折扣，"
            "超过 3000 基点拒绝；\n"
            "2. **事件审计**：每张订单的状态变更都要留下事件，对账接口报出各类事件计数。\n"
            "项目自带的测试描述了全部要求，请让它们全部通过。"
            "测试文件不要改（判定前会被还原），改动请落在源码上。"
        ),
        sources=dict(_SEED_SOURCES),
        tests={
            "tests/__init__.py": _TEST_INIT,
            "tests/test_core.py": _TEST_CORE,
            "tests/test_discount.py": _VISIBLE_DISCOUNT,
            "tests/test_audit_trail.py": _VISIBLE_AUDIT,
        },
        reference=changed_sources(_SEED, _BOTH_REFERENCE),
        judge=(
            "python3 -m unittest -v tests.test_core tests.test_discount "
            "tests.test_audit_trail"
        ),
        timeout=600,
        budget=dict(_LONG_BUDGET),
    ),
)
