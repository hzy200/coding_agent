"""100+ 全功能 capability 任务集。

与 `tasks.py` / `tasks_long.py` 的关系：不改动既有 canonical 基线，新增
`full` 套件供 LangSmith 全量评测使用。任务按能力族分组，尽量覆盖：

- 单文件算法/字符串/集合/解析/校验
- 有状态对象、缓存、事件、重试
- 跨文件数据流、配置、工厂、适配器
- 排障：导入环、全局状态、缓存、编码、路径、异常
- spec：可见测试全绿、隐藏需求未满足
- 并发/IO、长程多模块功能

所有任务都提供参考解；`runner.py --suite full --verify-seeds` 会验证
「种子必失败、参考解必通过」。
"""

from __future__ import annotations

import re

from harness import (
    BASIC,
    CROSS_FILE,
    DEBUG,
    DEEP,
    LONG,
    SINGLE_FILE,
    SPEC,
    EvalTask,
)

TASKS_FULL: list[EvalTask] = []


def _prompt(text: str) -> str:
    return (
        f"项目就在当前工作目录。{text}\n"
        "请让项目自带的测试全部通过。测试文件不要改"
        "（判定前会被还原），改动请落在源码上。"
    )


def _body(body: str, *, indent: int = 4) -> str:
    prefix = " " * indent
    return "\n".join(prefix + line if line else "" for line in body.splitlines())


def _render_args(args: object) -> str:
    """把 case 里的参数渲染成调用表达式。

    - tuple/list 等值用 repr，作为**单个**参数；
    - 含逗号/括号的字符串按原始表达式处理（多参数）；
    - 普通字符串用 repr（单个字符串参数）。
    """
    if isinstance(args, str):
        if (
            "," in args
            or args.startswith(("'", '"', "[", "{", "("))
            or "__import__" in args
            or re.fullmatch(r"-?\d+(?:\.\d+)?", args)
            or args in {"True", "False", "None"}
        ):
            return args
        return repr(args)
    return repr(args)


def _test_file(
    name: str, cases: list[tuple[object, object]], raises: list[tuple[object, str]]
) -> str:
    lines = [
        "import unittest",
        "",
        f"from {name} import {name}",
        "",
        "",
        "class T(unittest.TestCase):",
    ]
    for index, (args, expected) in enumerate(cases, 1):
        lines.extend(
            [
                f"    def test_case_{index}(self):",
                f"        self.assertEqual({name}({_render_args(args)}), {expected!r})",
            ]
        )
    for index, (args, exc) in enumerate(raises, 1):
        lines.extend(
            [
                f"    def test_raises_{index}(self):",
                f"        with self.assertRaises({exc}):",
                f"            {name}({_render_args(args)})",
            ]
        )
    return "\n".join(lines) + "\n"


def _simple(
    task_id: str,
    category: str,
    name: str,
    signature: str,
    seed: str,
    ref: str,
    cases: list[tuple[str, object]],
    raises: list[tuple[str, str]] | None = None,
    *,
    tier: str = BASIC,
    prompt: str = "",
) -> None:
    source = f"def {name}({signature}):\n{_body(seed)}\n"
    reference = f"def {name}({signature}):\n{_body(ref)}\n"
    TASKS_FULL.append(
        EvalTask(
            id=task_id,
            category=category,
            prompt=_prompt(prompt or f"{name} 的行为不符合测试期望。"),
            sources={f"{name}.py": source},
            tests={f"test_{name}.py": _test_file(name, cases, raises or [])},
            reference={f"{name}.py": reference},
            tier=tier,
        )
    )


# ---------------------------------------------------------------------------
# 单文件：字符串（10）
# ---------------------------------------------------------------------------

_simple(
    "full/string/slugify", SINGLE_FILE, "slugify", "text",
    "return text.replace(' ', '-')",
    "return '-'.join(text.strip().lower().split())",
    [("Hello World", "hello-world"), ("  A  B ", "a-b"), ("Already-slug", "already-slug")],
)
_simple(
    "full/string/snake_case", SINGLE_FILE, "snake_case", "text",
    "return text.lower().replace(' ', '_')",
    "return '_'.join(text.strip().lower().split())",
    [("Hello World", "hello_world"), ("A  B", "a_b")],
)
_simple(
    "full/string/camel_case", SINGLE_FILE, "camel_case", "text",
    "return ''.join(word.capitalize() for word in text.split())",
    "words = text.split()\nreturn words[0].lower() + ''.join(w.capitalize() for w in words[1:]) if words else ''",
    [("hello world", "helloWorld"), ("API response code", "apiResponseCode")],
)
_simple(
    "full/string/initials", SINGLE_FILE, "initials", "name",
    "return ''.join(word[0] for word in name.split())",
    "return ''.join(word[0].upper() for word in name.split())",
    [("ada lovelace", "AL"), ("grace brewster hopper", "GBH")],
)
_simple(
    "full/string/truncate", SINGLE_FILE, "truncate", "text, limit",
    "return text[:limit]",
    "return text if len(text) <= limit else text[: max(0, limit - 3)] + '...'",
    [("'abcdef', 5", "ab..."), ("'abc', 5", "abc"), ("'abcdef', 3", "...")],
)
_simple(
    "full/string/normalize_space", SINGLE_FILE, "normalize_space", "text",
    "return text.strip()",
    "return ' '.join(text.split())",
    [("  a   b  ", "a b"), ("a\n b\tc", "a b c")],
)
_simple(
    "full/string/strip_suffix", SINGLE_FILE, "strip_suffix", "text, suffix",
    "return text.replace(suffix, '')",
    "return text[:-len(suffix)] if suffix and text.endswith(suffix) else text",
    [("'report.csv', '.csv'", "report"), ("'a.csv.csv', '.csv'", "a.csv")],
)
_simple(
    "full/string/escape_html", SINGLE_FILE, "escape_html", "text",
    "return text.replace('<', '&lt;')",
    "return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')",
    [("<b>", "&lt;b&gt;"), ("a & b", "a &amp; b")],
)
_simple(
    "full/string/word_count", SINGLE_FILE, "word_count", "text",
    "return len(text.split(' '))",
    "return len(text.split())",
    [("a b c", 3), ("a  b", 2), ("   ", 0)],
)
_simple(
    "full/string/reverse_words", SINGLE_FILE, "reverse_words", "text",
    "return text[::-1]",
    "return ' '.join(reversed(text.split()))",
    [("one two three", "three two one"), ("abc", "abc")],
)


# ---------------------------------------------------------------------------
# 单文件：数值（10）
# ---------------------------------------------------------------------------

_simple(
    "full/numeric/average", SINGLE_FILE, "average", "nums",
    "return sum(nums) / (len(nums) + 1)",
    "return sum(nums) / len(nums)",
    [("[1, 2, 3]", 2.0), ("[5]", 5.0)],
)
_simple(
    "full/numeric/median", SINGLE_FILE, "median", "nums",
    "return sorted(nums)[len(nums) // 2]",
    "ordered = sorted(nums)\nmid = len(ordered) // 2\nreturn ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2",
    [("[3, 1, 2]", 2), ("[1, 2, 3, 4]", 2.5)],
)
_simple(
    "full/numeric/clamp", SINGLE_FILE, "clamp", "value, low, high",
    "return max(value, low)",
    "return min(max(value, low), high)",
    [("5, 0, 3", 3), ("-1, 0, 3", 0), ("2, 0, 3", 2)],
)
_simple(
    "full/numeric/percentage", SINGLE_FILE, "percentage", "part, total",
    "return part / total * 100",
    "return 0.0 if total == 0 else part / total * 100",
    [("1, 4", 25.0), ("1, 0", 0.0)],
)
_simple(
    "full/numeric/round_half_up", SINGLE_FILE, "round_half_up", "value",
    "return round(value)",
    "import math\nreturn math.floor(value + 0.5)",
    [("2.5", 3), ("2.4", 2), ("-2.5", -2)],
)
_simple(
    "full/numeric/safe_div", SINGLE_FILE, "safe_div", "a, b",
    "return a / b",
    "return 0.0 if b == 0 else a / b",
    [("4, 2", 2.0), ("4, 0", 0.0)],
)
_simple(
    "full/numeric/factorial", SINGLE_FILE, "factorial", "n",
    "result = 1\nfor value in range(1, n):\n    result *= value\nreturn result",
    "result = 1\nfor value in range(1, n + 1):\n    result *= value\nreturn result",
    [("4", 24), ("1", 1)],
)
_simple(
    "full/numeric/gcd", SINGLE_FILE, "gcd", "a, b",
    "return a",
    "while b:\n    a, b = b, a % b\nreturn abs(a)",
    [("12, 18", 6), ("7, 13", 1), ("0, 5", 5)],
)
_simple(
    "full/numeric/is_prime", SINGLE_FILE, "is_prime", "n",
    "return n > 1",
    "if n < 2:\n    return False\nfor divisor in range(2, int(n ** 0.5) + 1):\n    if n % divisor == 0:\n        return False\nreturn True",
    [("2", True), ("9", False), ("13", True)],
)
_simple(
    "full/numeric/fibonacci", SINGLE_FILE, "fibonacci", "n",
    "return n",
    "a, b = 0, 1\nfor _ in range(n):\n    a, b = b, a + b\nreturn a",
    [("6", 8), ("1", 1), ("0", 0)],
)


# ---------------------------------------------------------------------------
# 单文件：集合（10）
# ---------------------------------------------------------------------------

_simple(
    "full/collections/chunk", SINGLE_FILE, "chunk", "items, size",
    "return [items[i:i + size + 1] for i in range(0, len(items), size + 1)]",
    "return [items[i:i + size] for i in range(0, len(items), size)]",
    [("[1,2,3,4,5], 2", [[1, 2], [3, 4], [5]])],
)
_simple(
    "full/collections/flatten", SINGLE_FILE, "flatten", "items",
    "return items",
    "return [item for group in items for item in group]",
    [("[[1, 2], [3], [4, 5]]", [1, 2, 3, 4, 5])],
)
_simple(
    "full/collections/unique", SINGLE_FILE, "unique", "items",
    "return list(set(items))",
    "seen = set()\nreturn [item for item in items if not (item in seen or seen.add(item))]",
    [("[3, 1, 3, 2, 1]", [3, 1, 2])],
)
_simple(
    "full/collections/group_by_kind", SINGLE_FILE, "group_by_kind", "items",
    "result = {}\nfor item in items:\n    result[item['name']] = item\nreturn result",
    "result = {}\nfor item in items:\n    result.setdefault(item['kind'], []).append(item)\nreturn result",
    [("[{'kind': 'a', 'name': 'x'}, {'kind': 'a', 'name': 'y'}]", {"a": [{"kind": "a", "name": "x"}, {"kind": "a", "name": "y"}]})],
)
_simple(
    "full/collections/rotate", SINGLE_FILE, "rotate", "items, steps",
    "return items[steps:] + items[:steps]",
    "if not items:\n    return []\nsteps %= len(items)\nreturn items[-steps:] + items[:-steps] if steps else list(items)",
    [("[1,2,3], 1", [3, 1, 2]), ("[1,2,3], 0", [1, 2, 3])],
)
_simple(
    "full/collections/window", SINGLE_FILE, "window", "items, size",
    "return [items[i:i + size] for i in range(len(items) - size)]",
    "return [items[i:i + size] for i in range(len(items) - size + 1)]",
    [("[1,2,3], 2", [[1, 2], [2, 3]])],
)
_simple(
    "full/collections/partition_even", SINGLE_FILE, "partition_even", "items",
    "return ([item for item in items if item % 2 == 1], [item for item in items if item % 2 == 0])",
    "return ([item for item in items if item % 2 == 0], [item for item in items if item % 2 == 1])",
    [("[1,2,3,4]", ([2, 4], [1, 3]))],
)
_simple(
    "full/collections/merge_dicts", SINGLE_FILE, "merge_dicts", "left, right",
    "return left",
    "result = dict(left)\nresult.update(right)\nreturn result",
    [("{'a': 1}, {'b': 2, 'a': 3}", {"a": 3, "b": 2})],
)
_simple(
    "full/collections/most_common", SINGLE_FILE, "most_common", "items",
    "return items[0]",
    "return max(items, key=items.count)",
    [("['b', 'a', 'a']", "a")],
)
_simple(
    "full/collections/running_total", SINGLE_FILE, "running_total", "items",
    "return items[0] if items else 0",
    "total = 0\nresult = []\nfor item in items:\n    total += item\n    result.append(total)\nreturn result",
    [("[1,2,3]", [1, 3, 6])],
)


# ---------------------------------------------------------------------------
# 单文件：解析（10）
# ---------------------------------------------------------------------------

_simple(
    "full/parsing/parse_int", SINGLE_FILE, "parse_int", "text",
    "return int(text or 0)",
    "if not str(text).strip():\n    raise ValueError('empty')\nreturn int(text)",
    [("'12'", 12)],
    [("''", "ValueError")],
)
_simple(
    "full/parsing/parse_bool", SINGLE_FILE, "parse_bool", "text",
    "return text == 'true'",
    "value = str(text).strip().lower()\nif value in {'true', '1', 'yes'}:\n    return True\nif value in {'false', '0', 'no'}:\n    return False\nraise ValueError(f'bad bool: {text}')",
    [("'TRUE'", True), ("'no'", False)],
    [("'maybe'", "ValueError")],
)
_simple(
    "full/parsing/parse_duration", SINGLE_FILE, "parse_duration", "text",
    "parts = text.split(':')\nreturn int(parts[0]) * 60 + int(parts[1])",
    "hours, minutes, seconds = 0, 0, 0\nparts = [int(part) for part in text.split(':')]\nif len(parts) == 3:\n    hours, minutes, seconds = parts\nelif len(parts) == 2:\n    minutes, seconds = parts\nelse:\n    seconds = parts[0]\nreturn hours * 3600 + minutes * 60 + seconds",
    [("'01:02:03'", 3723), ("'02:03'", 123)],
)
_simple(
    "full/parsing/parse_size", SINGLE_FILE, "parse_size", "text",
    "return int(text.lower().replace('kb', '').strip()) * 1024",
    "units = {'kb': 1024, 'mb': 1024 ** 2, 'gb': 1024 ** 3}\nvalue = text.strip().lower()\nfor unit, factor in sorted(units.items(), key=lambda item: -len(item[0])):\n    if value.endswith(unit):\n        return int(float(value[:-len(unit)].strip()) * factor)\nreturn int(value)",
    [("'1kb'", 1024), ("'2mb'", 2 * 1024 ** 2)],
)
_simple(
    "full/parsing/parse_port", SINGLE_FILE, "parse_port", "text",
    "return int(text)",
    "port = int(text)\nif not 1 <= port <= 65535:\n    raise ValueError('bad port')\nreturn port",
    [("'80'", 80)],
    [("'0'", "ValueError"), ("'70000'", "ValueError")],
)
_simple(
    "full/parsing/parse_version", SINGLE_FILE, "parse_version", "text",
    "return text.split('.')",
    "return tuple(int(part) for part in text.split('.'))",
    [("'1.2.3'", (1, 2, 3))],
)
_simple(
    "full/parsing/parse_csv_line", SINGLE_FILE, "parse_csv_line", "line",
    "return line.split(',')",
    "import csv, io\nreturn next(csv.reader(io.StringIO(line)))",
    [("'a,\"b,c\",d'", ["a", "b,c", "d"])],
)
_simple(
    "full/parsing/parse_query", SINGLE_FILE, "parse_query", "query",
    "result = {}\nfor pair in query.split('&'):\n    key, _, value = pair.partition('=')\n    result[key] = value\nreturn result",
    "from urllib.parse import parse_qs\nreturn {key: values[0] for key, values in parse_qs(query).items()}",
    [("'a=1&b=hello%20world'", {"a": "1", "b": "hello world"})],
)
_simple(
    "full/parsing/parse_date", SINGLE_FILE, "parse_date", "text",
    "return text.split('-')",
    "return tuple(int(part) for part in text.split('-'))",
    [("'2024-03-05'", (2024, 3, 5))],
)
_simple(
    "full/parsing/parse_env", SINGLE_FILE, "parse_env", "line",
    "key, _, value = line.partition('=')\nreturn key.strip(), value.strip()",
    "text = line.strip()\nif text.startswith('export '):\n    text = text[len('export '):]\nkey, _, value = text.partition('=')\nreturn key.strip(), value.strip().strip('\"\\'')",
    [("'export TOKEN=abc'", ("TOKEN", "abc")), ("'A=\"x y\"'", ("A", "x y"))],
)


# ---------------------------------------------------------------------------
# 单文件：校验（8）
# ---------------------------------------------------------------------------

_simple(
    "full/validation/email", SINGLE_FILE, "valid_email", "text",
    "return '@' in text",
    "text = str(text)\nlocal, sep, domain = text.rpartition('@')\nreturn bool(local and sep and '.' in domain and ' ' not in text)",
    [("'a@b.com'", True), ("'a@b'", False)],
)
_simple(
    "full/validation/url", SINGLE_FILE, "valid_url", "text",
    "return text.startswith('http')",
    "from urllib.parse import urlparse\nparsed = urlparse(str(text))\nreturn parsed.scheme in {'http', 'https'} and bool(parsed.netloc)",
    [("'https://example.com/x'", True), ("'example.com'", False), ("'httpfoo'", False)],
)
_simple(
    "full/validation/ipv4", SINGLE_FILE, "valid_ipv4", "text",
    "return len(text.split('.')) == 4",
    "parts = str(text).split('.')\nreturn len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)",
    [("'192.168.1.1'", True), ("'999.1.1.1'", False)],
)
_simple(
    "full/validation/password_strength", SINGLE_FILE, "password_strength", "text",
    "return len(text) >= 8",
    "text = str(text)\nreturn len(text) >= 8 and any(c.isdigit() for c in text) and any(c.isalpha() for c in text)",
    [("'abcd1234'", True), ("'abcdefgh'", False)],
)
_simple(
    "full/validation/username", SINGLE_FILE, "valid_username", "text",
    "return ' ' not in text",
    "text = str(text)\nreturn bool(text) and text.replace('_', '').isalnum() and text[0].isalpha()",
    [("'alice_1'", True), ("'1alice'", False)],
)
_simple(
    "full/validation/hex_color", SINGLE_FILE, "valid_hex_color", "text",
    "return len(text) == 7",
    "text = str(text)\nreturn len(text) == 7 and text.startswith('#') and all(c in '0123456789abcdefABCDEF' for c in text[1:])",
    [("'#a1B2c3'", True), ("'#xyzxyz'", False)],
)
_simple(
    "full/validation/phone", SINGLE_FILE, "valid_phone", "text",
    "return len(text) >= 7",
    "digits = ''.join(c for c in str(text) if c.isdigit())\nreturn 7 <= len(digits) <= 15",
    [("'123-456-7890'", True), ("'12'", False), ("'abcdefg'", False)],
)
_simple(
    "full/validation/luhn", SINGLE_FILE, "luhn_check", "number",
    "return len(number) >= 12",
    "digits = [int(c) for c in str(number) if c.isdigit()]\ntotal = 0\nfor index, digit in enumerate(reversed(digits)):\n    if index % 2 == 1:\n        digit *= 2\n        if digit > 9:\n            digit -= 9\n    total += digit\nreturn bool(digits) and total % 10 == 0",
    [("'4539578763621486'", True), ("'1234567812345678'", False)],
)


# ---------------------------------------------------------------------------
# 单文件：有状态对象（8）
# ---------------------------------------------------------------------------

TASKS_FULL.append(EvalTask(
    id="full/state/counter",
    category=SINGLE_FILE,
    prompt=_prompt("Counter 的实例之间不应该共享计数。"),
    sources={"counter.py": "class Counter:\n    count = 0\n\n    def add(self):\n        Counter.count += 1\n        return Counter.count\n"},
    tests={"test_counter.py": "import unittest\nfrom counter import Counter\n\nclass T(unittest.TestCase):\n    def test_instances_independent(self):\n        a, b = Counter(), Counter()\n        self.assertEqual(a.add(), 1)\n        self.assertEqual(b.add(), 1)\n        self.assertEqual(a.add(), 2)\n"},
    reference={"counter.py": "class Counter:\n    def __init__(self):\n        self.count = 0\n\n    def add(self):\n        self.count += 1\n        return self.count\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/state/memo_cache_key",
    category=SINGLE_FILE,
    prompt=_prompt("memo 缓存忽略了参数，不同输入拿到了同一个结果。"),
    sources={"memo.py": "def memo(func, cache={}):\n    def wrapper(value):\n        if 'x' in cache:\n            return cache['x']\n        cache['x'] = func(value)\n        return cache['x']\n    return wrapper\n"},
    tests={"test_memo.py": "import unittest\nfrom memo import memo\n\n@memo\ndef double(value):\n    return value * 2\n\nclass T(unittest.TestCase):\n    def test_different_inputs(self):\n        self.assertEqual(double(2), 4)\n        self.assertEqual(double(5), 10)\n"},
    reference={"memo.py": "def memo(func, cache=None):\n    cache = {} if cache is None else cache\n    def wrapper(value):\n        if value not in cache:\n            cache[value] = func(value)\n        return cache[value]\n    return wrapper\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/state/cache_invalidation",
    category=SINGLE_FILE,
    prompt=_prompt("set 之后 get 仍然返回旧缓存。"),
    sources={"store.py": "class Store:\n    def __init__(self):\n        self._values = {}\n        self._cache = {}\n\n    def set(self, key, value):\n        self._values[key] = value\n\n    def get(self, key):\n        if key not in self._cache:\n            self._cache[key] = self._values.get(key)\n        return self._cache[key]\n"},
    tests={"test_store.py": "import unittest\nfrom store import Store\n\nclass T(unittest.TestCase):\n    def test_update_visible(self):\n        s = Store()\n        s.set('a', 1)\n        self.assertEqual(s.get('a'), 1)\n        s.set('a', 2)\n        self.assertEqual(s.get('a'), 2)\n"},
    reference={"store.py": "class Store:\n    def __init__(self):\n        self._values = {}\n        self._cache = {}\n\n    def set(self, key, value):\n        self._values[key] = value\n        self._cache.pop(key, None)\n\n    def get(self, key):\n        if key not in self._cache:\n            self._cache[key] = self._values.get(key)\n        return self._cache[key]\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/state/buffer_flush",
    category=SINGLE_FILE,
    prompt=_prompt("Buffer.flush 没有真的把内容交出去。"),
    sources={"buffer.py": "class Buffer:\n    def __init__(self):\n        self._items = []\n\n    def add(self, item):\n        self._items.append(item)\n\n    def flush(self):\n        return ''\n"},
    tests={"test_buffer.py": "import unittest\nfrom buffer import Buffer\n\nclass T(unittest.TestCase):\n    def test_flush_returns_and_clears(self):\n        b = Buffer()\n        b.add('a'); b.add('b')\n        self.assertEqual(b.flush(), 'ab')\n        self.assertEqual(b.flush(), '')\n"},
    reference={"buffer.py": "class Buffer:\n    def __init__(self):\n        self._items = []\n\n    def add(self, item):\n        self._items.append(item)\n\n    def flush(self):\n        text = ''.join(self._items)\n        self._items.clear()\n        return text\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/state/event_bus",
    category=SINGLE_FILE,
    prompt=_prompt("EventBus 订阅第二个 handler 时丢掉了第一个。"),
    sources={"bus.py": "class EventBus:\n    def __init__(self):\n        self.handlers = {}\n\n    def subscribe(self, event, handler):\n        self.handlers[event] = handler\n\n    def emit(self, event, value):\n        handler = self.handlers.get(event)\n        if handler:\n            handler(value)\n"},
    tests={"test_bus.py": "import unittest\nfrom bus import EventBus\n\nclass T(unittest.TestCase):\n    def test_all_handlers(self):\n        bus, seen = EventBus(), []\n        bus.subscribe('x', seen.append)\n        bus.subscribe('x', lambda v: seen.append(v * 2))\n        bus.emit('x', 3)\n        self.assertEqual(seen, [3, 6])\n"},
    reference={"bus.py": "class EventBus:\n    def __init__(self):\n        self.handlers = {}\n\n    def subscribe(self, event, handler):\n        self.handlers.setdefault(event, []).append(handler)\n\n    def emit(self, event, value):\n        for handler in list(self.handlers.get(event, [])):\n            handler(value)\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/state/rate_limiter",
    category=SINGLE_FILE,
    prompt=_prompt("rate_limiter 应该只统计窗口内的请求。"),
    sources={"limiter.py": "def allow(timestamps, now, limit, window):\n    return len(timestamps) < limit\n"},
    tests={"test_limiter.py": "import unittest\nfrom limiter import allow\n\nclass T(unittest.TestCase):\n    def test_window(self):\n        stamps = [1, 2, 100, 101]\n        self.assertFalse(allow(stamps, 102, 2, 5))\n        self.assertTrue(allow(stamps, 102, 3, 5))\n"},
    reference={"limiter.py": "def allow(timestamps, now, limit, window):\n    recent = [stamp for stamp in timestamps if now - stamp <= window]\n    return len(recent) < limit\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/state/retry",
    category=SINGLE_FILE,
    prompt=_prompt("run_with_retry 应该一直重试到成功或用尽次数。"),
    sources={"retry.py": "def run_with_retry(func, attempts):\n    return func()\n"},
    tests={"test_retry.py": "import unittest\nfrom retry import run_with_retry\n\nclass T(unittest.TestCase):\n    def test_retries(self):\n        calls = []\n        def flaky():\n            calls.append(1)\n            if len(calls) < 3:\n                raise RuntimeError('boom')\n            return 'ok'\n        self.assertEqual(run_with_retry(flaky, 5), 'ok')\n        self.assertEqual(len(calls), 3)\n"},
    reference={"retry.py": "def run_with_retry(func, attempts):\n    last = None\n    for _ in range(attempts):\n        try:\n            return func()\n        except Exception as exc:\n            last = exc\n    raise last\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/state/id_generator",
    category=SINGLE_FILE,
    prompt=_prompt("IdGenerator 每次 next() 应该返回不同 id。"),
    sources={"ids.py": "class IdGenerator:\n    def __init__(self):\n        self.value = 0\n\n    def next(self):\n        return 'id-1'\n"},
    tests={"test_ids.py": "import unittest\nfrom ids import IdGenerator\n\nclass T(unittest.TestCase):\n    def test_increments(self):\n        g = IdGenerator()\n        self.assertEqual(g.next(), 'id-1')\n        self.assertEqual(g.next(), 'id-2')\n"},
    reference={"ids.py": "class IdGenerator:\n    def __init__(self):\n        self.value = 0\n\n    def next(self):\n        self.value += 1\n        return f'id-{self.value}'\n"},
))


# ---------------------------------------------------------------------------
# 跨文件数据流（12）
# ---------------------------------------------------------------------------

def _multi(
    task_id: str,
    category: str,
    prompt: str,
    files: dict[str, str],
    tests: dict[str, str],
    reference: dict[str, str],
    *,
    tier: str = BASIC,
    hidden_tests: dict[str, str] | None = None,
) -> None:
    TASKS_FULL.append(
        EvalTask(
            id=task_id,
            category=category,
            prompt=_prompt(prompt),
            sources=files,
            tests=tests,
            reference=reference,
            tier=tier,
            hidden_tests=hidden_tests or {},
        )
    )


_multi(
    "full/cross/config_service", CROSS_FILE,
    "service 应该使用 config 里的默认超时，而不是引用不存在的属性。",
    {
        "config.py": "DEFAULT_TIMEOUT = 30\n",
        "service.py": "import config\n\ndef timeout():\n    return config.TIMEOUT\n",
    },
    {"test_service.py": "import unittest\nimport service\n\nclass T(unittest.TestCase):\n    def test_timeout(self):\n        self.assertEqual(service.timeout(), 30)\n"},
    {"service.py": "import config\n\ndef timeout():\n    return config.DEFAULT_TIMEOUT\n"},
)
_multi(
    "full/cross/dataclass_serializer", CROSS_FILE,
    "User 序列化时漏掉了 email 字段。",
    {
        "models.py": "from dataclasses import dataclass\n\n@dataclass\nclass User:\n    name: str\n    email: str\n",
        "serializer.py": "def to_dict(user):\n    return {'name': user.name}\n",
    },
    {"test_serializer.py": "import unittest\nfrom models import User\nfrom serializer import to_dict\n\nclass T(unittest.TestCase):\n    def test_all_fields(self):\n        self.assertEqual(to_dict(User('a', 'a@b.c')), {'name': 'a', 'email': 'a@b.c'})\n"},
    {"serializer.py": "def to_dict(user):\n    return {'name': user.name, 'email': user.email}\n"},
)
_multi(
    "full/cross/plugin_registry", CROSS_FILE,
    "registry 重复注册同名插件时应该报错。",
    {
        "registry.py": "REGISTRY = {}\n\ndef register(name, func):\n    REGISTRY[name] = func\n",
        "plugins.py": "from registry import register\n\ndef hello():\n    return 'hi'\n\nregister('hello', hello)\n",
    },
    {"test_registry.py": "import unittest\nfrom registry import REGISTRY, register\n\nclass T(unittest.TestCase):\n    def test_duplicate_rejected(self):\n        register('x', lambda: 1)\n        with self.assertRaises(ValueError):\n            register('x', lambda: 2)\n"},
    {"registry.py": "REGISTRY = {}\n\ndef register(name, func):\n    if name in REGISTRY:\n        raise ValueError(f'duplicate plugin: {name}')\n    REGISTRY[name] = func\n"},
)
_multi(
    "full/cross/repository_service", CROSS_FILE,
    "service 调用了 Repository 上不存在的方法。",
    {
        "repo.py": "class Repository:\n    def __init__(self):\n        self._data = {'a': 1}\n\n    def get(self, key):\n        return self._data.get(key)\n",
        "service.py": "class Service:\n    def __init__(self, repo):\n        self.repo = repo\n\n    def read(self, key):\n        return self.repo.load(key)\n",
    },
    {"test_service.py": "import unittest\nfrom repo import Repository\nfrom service import Service\n\nclass T(unittest.TestCase):\n    def test_read(self):\n        self.assertEqual(Service(Repository()).read('a'), 1)\n"},
    {"service.py": "class Service:\n    def __init__(self, repo):\n        self.repo = repo\n\n    def read(self, key):\n        return self.repo.get(key)\n"},
)
_multi(
    "full/cross/cli_formatter", CROSS_FILE,
    "cli 调用 formatter 的参数顺序反了。",
    {
        "formatter.py": "def format_line(name, value):\n    return f'{name}={value}'\n",
        "cli.py": "from formatter import format_line\n\ndef render(name, value):\n    return format_line(value, name)\n",
    },
    {"test_cli.py": "import unittest\nfrom cli import render\n\nclass T(unittest.TestCase):\n    def test_order(self):\n        self.assertEqual(render('a', 1), 'a=1')\n"},
    {"cli.py": "from formatter import format_line\n\ndef render(name, value):\n    return format_line(name, value)\n"},
)
_multi(
    "full/cross/pipeline_order", CROSS_FILE,
    "pipeline 应该先清理再校验。",
    {
        "stages.py": "def clean(value):\n    return value.strip()\n\ndef validate(value):\n    if not value:\n        raise ValueError('empty')\n    return value\n",
        "pipeline.py": "from stages import clean, validate\n\ndef run(value):\n    return clean(validate(value))\n",
    },
    {"test_pipeline.py": "import unittest\nfrom pipeline import run\n\nclass T(unittest.TestCase):\n    def test_order(self):\n        self.assertEqual(run('  x  '), 'x')\n        with self.assertRaises(ValueError):\n            run('   ')\n"},
    {"pipeline.py": "from stages import clean, validate\n\ndef run(value):\n    return validate(clean(value))\n"},
)
_multi(
    "full/cross/error_type", CROSS_FILE,
    "api 应该抛出领域错误 AppError。",
    {
        "errors.py": "class AppError(Exception):\n    pass\n",
        "api.py": "def fail():\n    raise ValueError('bad')\n",
    },
    {"test_api.py": "import unittest\nfrom errors import AppError\nfrom api import fail\n\nclass T(unittest.TestCase):\n    def test_domain_error(self):\n        with self.assertRaises(AppError):\n            fail()\n"},
    {"api.py": "from errors import AppError\n\ndef fail():\n    raise AppError('bad')\n"},
)
_multi(
    "full/cross/settings_precedence", CROSS_FILE,
    "loader 只返回覆盖值，丢了 defaults。",
    {
        "defaults.py": "DEFAULTS = {'a': 1, 'b': 2}\n",
        "loader.py": "from defaults import DEFAULTS\n\ndef load(overrides):\n    return dict(overrides)\n",
    },
    {"test_loader.py": "import unittest\nfrom loader import load\n\nclass T(unittest.TestCase):\n    def test_merge(self):\n        self.assertEqual(load({'b': 3}), {'a': 1, 'b': 3})\n"},
    {"loader.py": "from defaults import DEFAULTS\n\ndef load(overrides):\n    result = dict(DEFAULTS)\n    result.update(overrides)\n    return result\n"},
)
_multi(
    "full/cross/shared_constant", CROSS_FILE,
    "calc 硬编码了税率，应该使用 constants.TAX_RATE。",
    {
        "constants.py": "TAX_RATE = 0.1\n",
        "calc.py": "def total_with_tax(value):\n    return value * 1.2\n",
    },
    {"test_calc.py": "import unittest\nfrom calc import total_with_tax\n\nclass T(unittest.TestCase):\n    def test_rate(self):\n        self.assertAlmostEqual(total_with_tax(100), 110.0)\n"},
    {"calc.py": "from constants import TAX_RATE\n\ndef total_with_tax(value):\n    return value * (1 + TAX_RATE)\n"},
)
_multi(
    "full/cross/adapter_mapping", CROSS_FILE,
    "adapter 把 dict 当成对象属性访问。",
    {
        "legacy.py": "def old_get():\n    return {'value': 7}\n",
        "adapter.py": "from legacy import old_get\n\ndef get_value():\n    return old_get().value\n",
    },
    {"test_adapter.py": "import unittest\nfrom adapter import get_value\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(get_value(), 7)\n"},
    {"adapter.py": "from legacy import old_get\n\ndef get_value():\n    return old_get()['value']\n"},
)
_multi(
    "full/cross/factory", CROSS_FILE,
    "factory 无论名字是什么都返回 Circle。",
    {
        "shapes.py": "class Circle:\n    pass\n\nclass Square:\n    pass\n",
        "factory.py": "from shapes import Circle\n\ndef make(name):\n    return Circle()\n",
    },
    {"test_factory.py": "import unittest\nfrom factory import make\nfrom shapes import Circle, Square\n\nclass T(unittest.TestCase):\n    def test_types(self):\n        self.assertIsInstance(make('circle'), Circle)\n        self.assertIsInstance(make('square'), Square)\n"},
    {"factory.py": "from shapes import Circle, Square\n\ndef make(name):\n    if name == 'circle':\n        return Circle()\n    if name == 'square':\n        return Square()\n    raise ValueError(name)\n"},
)
_multi(
    "full/cross/di_singleton", CROSS_FILE,
    "container 每次都新建实例，应该按名字缓存。",
    {
        "container.py": "class Container:\n    def __init__(self, factory):\n        self.factory = factory\n\n    def get(self, name):\n        return self.factory(name)\n",
    },
    {"test_container.py": "import unittest\nfrom container import Container\n\nclass T(unittest.TestCase):\n    def test_singleton(self):\n        c = Container(lambda name: object())\n        self.assertIs(c.get('x'), c.get('x'))\n"},
    {"container.py": "class Container:\n    def __init__(self, factory):\n        self.factory = factory\n        self._cache = {}\n\n    def get(self, name):\n        if name not in self._cache:\n            self._cache[name] = self.factory(name)\n        return self._cache[name]\n"},
)


# ---------------------------------------------------------------------------
# 排障（12）
# ---------------------------------------------------------------------------

_multi(
    "full/debug/import_cycle", DEBUG,
    "模块之间循环导入，导入 a 应该成功。",
    {
        "a.py": "import b\n\ndef value():\n    return 1\n",
        "b.py": "import a\n\nVALUE = a.value()\n\ndef other():\n    return a.value()\n",
    },
    {"test_a.py": "import unittest\nimport a\n\nclass T(unittest.TestCase):\n    def test_import(self):\n        self.assertEqual(a.value(), 1)\n"},
    {
        "a.py": "def value():\n    return 1\n",
        "b.py": "def other():\n    from a import value\n    return value()\n",
    },
)
_simple(
    "full/debug/mutable_default", DEBUG, "add_item", "item, bucket=None",
    "bucket.append(item)\nreturn bucket",
    "bucket = [] if bucket is None else bucket\nbucket.append(item)\nreturn bucket",
    # reference must use None default as well

    [("'a'", ["a"]), ("'b'", ["b"])],
)
TASKS_FULL.append(EvalTask(
    id="full/debug/global_leak",
    category=DEBUG,
    prompt=_prompt("两次调用应该互不影响，reset 后计数归零。"),
    sources={"counter.py": "TOTAL = []\n\ndef add(value):\n    TOTAL.append(value)\n    return list(TOTAL)\n\ndef reset():\n    pass\n"},
    tests={"test_counter.py": "import unittest\nimport counter\n\nclass T(unittest.TestCase):\n    def test_reset(self):\n        self.assertEqual(counter.add(1), [1])\n        counter.reset()\n        self.assertEqual(counter.add(2), [2])\n"},
    reference={"counter.py": "TOTAL = []\n\ndef add(value):\n    TOTAL.append(value)\n    return list(TOTAL)\n\ndef reset():\n    TOTAL.clear()\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/debug/encoding",
    category=DEBUG,
    prompt=_prompt("read_text 读取 UTF-8 文件时不应该报错。"),
    sources={"text_io.py": "def read_text(path):\n    with open(path, 'r', encoding='ascii') as fh:\n        return fh.read()\n"},
    tests={"test_text_io.py": "import os, tempfile, unittest\nfrom text_io import read_text\n\nclass T(unittest.TestCase):\n    def test_utf8(self):\n        fd, path = tempfile.mkstemp()\n        os.close(fd)\n        try:\n            with open(path, 'w', encoding='utf-8') as fh:\n                fh.write('h\\u00e9llo')\n            self.assertEqual(read_text(path), 'h\\u00e9llo')\n        finally:\n            os.unlink(path)\n"},
    reference={"text_io.py": "def read_text(path):\n    with open(path, 'r', encoding='utf-8') as fh:\n        return fh.read()\n"},
))
_simple(
    "full/debug/path_join", DEBUG, "join_path", "root, name",
    "return root + name",
    "import os\nreturn os.path.join(root, name)",
    [("'/tmp/root', 'a.py'", "/tmp/root/a.py")],
)
_simple(
    "full/debug/exception_swallow", DEBUG, "safe_int", "text",
    "try:\n    return int(text)\nexcept Exception:\n    return None",
    "return int(text)",
    [("'12'", 12)],
    [("'x'", "ValueError")],
)
_simple(
    "full/debug/loop_boundary", DEBUG, "sum_to", "n",
    "return sum(range(n))",
    "return sum(range(n + 1))",
    [("3", 6), ("0", 0)],
)
TASKS_FULL.append(EvalTask(
    id="full/debug/json_set",
    category=DEBUG,
    prompt=_prompt("serialize 应该支持 set。"),
    sources={"json_util.py": "import json\n\ndef serialize(value):\n    return json.dumps(value)\n"},
    tests={"test_json_util.py": "import unittest\nfrom json_util import serialize\n\nclass T(unittest.TestCase):\n    def test_set(self):\n        self.assertEqual(serialize({1, 2}), '[1, 2]')\n"},
    reference={"json_util.py": "import json\n\ndef serialize(value):\n    return json.dumps(sorted(value) if isinstance(value, set) else value)\n"},
))
_simple(
    "full/debug/timezone", DEBUG, "to_utc", "hour",
    "return f'{hour}:00'",
    "from datetime import datetime, timezone\nreturn datetime(2024, 1, 1, hour, tzinfo=timezone.utc).isoformat()",
    [("0", "2024-01-01T00:00:00+00:00")],
    prompt="to_utc 应该返回明确的 UTC 时间。",
)
TASKS_FULL.append(EvalTask(
    id="full/debug/cache_stale",
    category=DEBUG,
    prompt=_prompt("更新文件后 read_cached 仍返回旧内容。"),
    sources={"reader.py": "CACHE = {}\n\ndef read_cached(path, loader):\n    if path not in CACHE:\n        CACHE[path] = loader(path)\n    return CACHE[path]\n"},
    tests={"test_reader.py": "import unittest\nfrom reader import read_cached\n\nclass T(unittest.TestCase):\n    def test_update_visible(self):\n        data = {'x': 'old'}\n        self.assertEqual(read_cached('x', lambda p: data[p]), 'old')\n        data['x'] = 'new'\n        self.assertEqual(read_cached('x', lambda p: data[p]), 'new')\n"},
    reference={"reader.py": "def read_cached(path, loader):\n    return loader(path)\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/debug/buffer_flush",
    category=DEBUG,
    prompt=_prompt("Buffer.flush 应该返回并清空内容。"),
    sources={"buffer.py": "class Buffer:\n    def __init__(self):\n        self.items = []\n\n    def add(self, value):\n        self.items.append(value)\n\n    def flush(self):\n        return ''\n"},
    tests={"test_buffer.py": "import unittest\nfrom buffer import Buffer\n\nclass T(unittest.TestCase):\n    def test_flush(self):\n        b = Buffer(); b.add('a')\n        self.assertEqual(b.flush(), 'a')\n        self.assertEqual(b.flush(), '')\n"},
    reference={"buffer.py": "class Buffer:\n    def __init__(self):\n        self.items = []\n\n    def add(self, value):\n        self.items.append(value)\n\n    def flush(self):\n        text = ''.join(self.items)\n        self.items.clear()\n        return text\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/debug/async_order",
    category=DEBUG,
    prompt=_prompt("gather_values 应该保持输入顺序。"),
    sources={"async_util.py": "import asyncio\n\nasync def gather_values(values):\n    results = []\n    for value in reversed(values):\n        results.append(value * 2)\n    return results\n"},
    tests={"test_async_util.py": "import asyncio, unittest\nfrom async_util import gather_values\n\nclass T(unittest.TestCase):\n    def test_order(self):\n        self.assertEqual(asyncio.run(gather_values([1, 2, 3])), [2, 4, 6])\n"},
    reference={"async_util.py": "import asyncio\n\nasync def gather_values(values):\n    async def one(value):\n        return value * 2\n    return list(await asyncio.gather(*(one(v) for v in values)))\n"},
))


# ---------------------------------------------------------------------------
# spec：可见测试全绿，隐藏需求未满足（8）
# ---------------------------------------------------------------------------

def _spec(
    task_id: str,
    prompt: str,
    files: dict[str, str],
    visible: dict[str, str],
    hidden: dict[str, str],
    reference: dict[str, str],
) -> None:
    TASKS_FULL.append(
        EvalTask(
            id=task_id,
            category=SPEC,
            prompt=_prompt(prompt),
            sources=files,
            tests=visible,
            hidden_tests=hidden,
            reference=reference,
            tier=DEEP,
        )
    )


_spec(
    "full/spec/pagination",
    "分页函数应该支持任意页，而不只是第一页。",
    {"paging.py": "def page(items, index, size):\n    return items[:size]\n"},
    {"test_paging.py": "import unittest\nfrom paging import page\n\nclass T(unittest.TestCase):\n    def test_first_page(self):\n        self.assertEqual(page([1,2,3,4], 0, 2), [1,2])\n"},
    {"test_paging_hidden.py": "import unittest\nfrom paging import page\n\nclass T(unittest.TestCase):\n    def test_second_page(self):\n        self.assertEqual(page([1,2,3,4], 1, 2), [3,4])\n"},
    {"paging.py": "def page(items, index, size):\n    start = index * size\n    return items[start:start + size]\n"},
)
_spec(
    "full/spec/idempotent",
    "create 重复调用同一 key 不应重复追加。",
    {"store.py": "def create(items, value):\n    items.append(value)\n    return items\n"},
    {"test_store.py": "import unittest\nfrom store import create\n\nclass T(unittest.TestCase):\n    def test_once(self):\n        items = []\n        self.assertEqual(create(items, 'a'), ['a'])\n"},
    {"test_store_hidden.py": "import unittest\nfrom store import create\n\nclass T(unittest.TestCase):\n    def test_twice(self):\n        items = []\n        create(items, 'a')\n        self.assertEqual(create(items, 'a'), ['a'])\n"},
    {"store.py": "def create(items, value):\n    if value not in items:\n        items.append(value)\n    return items\n"},
)
_spec(
    "full/spec/case_insensitive_sort",
    "sort_names 应该大小写不敏感。",
    {"sorting.py": "def sort_names(names):\n    return sorted(names)\n"},
    {"test_sorting.py": "import unittest\nfrom sorting import sort_names\n\nclass T(unittest.TestCase):\n    def test_lower(self):\n        self.assertEqual(sort_names(['b','a']), ['a','b'])\n"},
    {"test_sorting_hidden.py": "import unittest\nfrom sorting import sort_names\n\nclass T(unittest.TestCase):\n    def test_mixed_case(self):\n        self.assertEqual(sort_names(['B','a']), ['a','B'])\n"},
    {"sorting.py": "def sort_names(names):\n    return sorted(names, key=str.lower)\n"},
)
_spec(
    "full/spec/stable_order",
    "按 score 排序时应保持同分记录的原始顺序。",
    {"records.py": "def by_score(records):\n    return sorted(records, key=lambda item: (item['score'], item['id']))\n"},
    {"test_records.py": "import unittest\nfrom records import by_score\n\nclass T(unittest.TestCase):\n    def test_distinct(self):\n        data = [{'score': 2, 'id': 'a'}, {'score': 1, 'id': 'b'}]\n        self.assertEqual([r['id'] for r in by_score(data)], ['b','a'])\n"},
    {"test_records_hidden.py": "import unittest\nfrom records import by_score\n\nclass T(unittest.TestCase):\n    def test_ties(self):\n        data = [{'score': 1, 'id': 'b'}, {'score': 1, 'id': 'a'}]\n        self.assertEqual([r['id'] for r in by_score(data)], ['b','a'])\n"},
    {"records.py": "def by_score(records):\n    return sorted(records, key=lambda item: item['score'])\n"},
)
_spec(
    "full/spec/error_message",
    "非法输入的错误信息必须包含 invalid input。",
    {"validate.py": "def parse(value):\n    raise ValueError('bad')\n"},
    {"test_validate.py": "import unittest\nfrom validate import parse\n\nclass T(unittest.TestCase):\n    def test_raises(self):\n        with self.assertRaises(ValueError):\n            parse('x')\n"},
    {"test_validate_hidden.py": "import unittest\nfrom validate import parse\n\nclass T(unittest.TestCase):\n    def test_message(self):\n        with self.assertRaisesRegex(ValueError, 'invalid input'):\n            parse('x')\n"},
    {"validate.py": "def parse(value):\n    raise ValueError(f'invalid input: {value}')\n"},
)
_spec(
    "full/spec/backward_compat",
    "get 应兼容旧字段 title。",
    {"record.py": "def get(record):\n    return record['name']\n"},
    {"test_record.py": "import unittest\nfrom record import get\n\nclass T(unittest.TestCase):\n    def test_new(self):\n        self.assertEqual(get({'name': 'a'}), 'a')\n"},
    {"test_record_hidden.py": "import unittest\nfrom record import get\n\nclass T(unittest.TestCase):\n    def test_old(self):\n        self.assertEqual(get({'title': 'a'}), 'a')\n"},
    {"record.py": "def get(record):\n    return record.get('name', record.get('title'))\n"},
)
_spec(
    "full/spec/empty_input",
    "summarize 对空输入返回空串。",
    {"summary_util.py": "def summarize(items):\n    return items[0]\n"},
    {"test_summary_util.py": "import unittest\nfrom summary_util import summarize\n\nclass T(unittest.TestCase):\n    def test_nonempty(self):\n        self.assertEqual(summarize(['a']), 'a')\n"},
    {"test_summary_hidden.py": "import unittest\nfrom summary_util import summarize\n\nclass T(unittest.TestCase):\n    def test_empty(self):\n        self.assertEqual(summarize([]), '')\n"},
    {"summary_util.py": "def summarize(items):\n    return items[0] if items else ''\n"},
)
_spec(
    "full/spec/rounding",
    "金额应按两位小数四舍五入。",
    {"money.py": "def format_money(value):\n    return str(value)\n"},
    {"test_money.py": "import unittest\nfrom money import format_money\n\nclass T(unittest.TestCase):\n    def test_integer(self):\n        self.assertEqual(format_money(1), '1')\n"},
    {"test_money_hidden.py": "import unittest\nfrom money import format_money\n\nclass T(unittest.TestCase):\n    def test_rounding(self):\n        self.assertEqual(format_money(1.235), '1.24')\n"},
    {"money.py": "def format_money(value):\n    if float(value).is_integer():\n        return str(int(value))\n    return f'{float(value) + 1e-9:.2f}'\n"},
)


# ---------------------------------------------------------------------------
# 长程多模块（6）
# ---------------------------------------------------------------------------

_multi(
    "full/long/discount_pipeline", CROSS_FILE,
    "折扣流水线应该按 rate 减免金额。",
    {
        "rules.py": "def apply_discount(amount, rate):\n    return amount * rate\n",
        "pipeline.py": "from rules import apply_discount\n\ndef checkout(amount, rate):\n    return apply_discount(amount, rate)\n",
    },
    {"test_pipeline.py": "import unittest\nfrom pipeline import checkout\n\nclass T(unittest.TestCase):\n    def test_checkout(self):\n        self.assertEqual(checkout(100, 0.1), 90.0)\n"},
    {
        "rules.py": "def apply_discount(amount, rate):\n    return amount * (1 - rate)\n",
        "pipeline.py": "from rules import apply_discount\n\ndef checkout(amount, rate):\n    return apply_discount(amount, rate)\n",
    },
    tier=LONG,
)
_multi(
    "full/long/audit_store", CROSS_FILE,
    "query 应该只返回指定 kind 的事件。",
    {
        "store.py": "EVENTS = []\n\ndef record(kind, detail):\n    EVENTS.append({'kind': kind, 'detail': detail})\n",
        "query.py": "from store import EVENTS\n\ndef by_kind(kind):\n    return list(EVENTS)\n",
    },
    {"test_query.py": "import unittest\nimport store\nfrom query import by_kind\n\nclass T(unittest.TestCase):\n    def test_filter(self):\n        store.EVENTS[:] = [{'kind': 'a', 'detail': 1}, {'kind': 'b', 'detail': 2}]\n        self.assertEqual(by_kind('a'), [{'kind': 'a', 'detail': 1}])\n"},
    {"query.py": "from store import EVENTS\n\ndef by_kind(kind):\n    return [item for item in EVENTS if item['kind'] == kind]\n"},
    tier=LONG,
)
_multi(
    "full/long/config_loader", CROSS_FILE,
    "loader 应该叠加 defaults、文件值和环境变量。",
    {
        "defaults.py": "DEFAULTS = {'host': 'localhost', 'port': 80}\n",
        "loader.py": "from defaults import DEFAULTS\n\ndef load(file_values, env_values):\n    return dict(file_values)\n",
        "app.py": "from loader import load\n\ndef config():\n    return load({'port': 8080}, {'host': 'example.com'})\n",
    },
    {"test_app.py": "import unittest\nfrom app import config\n\nclass T(unittest.TestCase):\n    def test_merge(self):\n        self.assertEqual(config(), {'host': 'example.com', 'port': 8080})\n"},
    {"loader.py": "from defaults import DEFAULTS\n\ndef load(file_values, env_values):\n    result = dict(DEFAULTS)\n    result.update(file_values)\n    result.update(env_values)\n    return result\n"},
    tier=LONG,
)
_multi(
    "full/long/inventory_service", CROSS_FILE,
    "restock 应该增加库存。",
    {
        "models.py": "from dataclasses import dataclass\n\n@dataclass\nclass Item:\n    name: str\n    count: int = 0\n",
        "repo.py": "class Repo:\n    def __init__(self):\n        self.items = {}\n\n    def save(self, item):\n        self.items[item.name] = item\n",
        "service.py": "class Service:\n    def __init__(self, repo):\n        self.repo = repo\n\n    def restock(self, name, amount):\n        item = self.repo.items[name]\n        item.count = item.count\n        self.repo.save(item)\n",
    },
    {"test_service.py": "import unittest\nfrom models import Item\nfrom repo import Repo\nfrom service import Service\n\nclass T(unittest.TestCase):\n    def test_restock(self):\n        repo = Repo(); repo.save(Item('a', 1))\n        Service(repo).restock('a', 4)\n        self.assertEqual(repo.items['a'].count, 5)\n"},
    {"service.py": "class Service:\n    def __init__(self, repo):\n        self.repo = repo\n\n    def restock(self, name, amount):\n        item = self.repo.items[name]\n        item.count += amount\n        self.repo.save(item)\n"},
    tier=LONG,
)
_multi(
    "full/long/notify_dispatch", CROSS_FILE,
    "dispatcher 应该按 channel 选择模板。",
    {
        "templates.py": "TEMPLATES = {'email': 'mail:{text}', 'sms': 'sms:{text}'}\n",
        "dispatcher.py": "from templates import TEMPLATES\n\ndef render(channel, text):\n    return TEMPLATES['email'].format(text=text)\n",
    },
    {"test_dispatcher.py": "import unittest\nfrom dispatcher import render\n\nclass T(unittest.TestCase):\n    def test_channel(self):\n        self.assertEqual(render('sms', 'hi'), 'sms:hi')\n"},
    {"dispatcher.py": "from templates import TEMPLATES\n\ndef render(channel, text):\n    return TEMPLATES[channel].format(text=text)\n"},
    tier=LONG,
)
_multi(
    "full/long/workflow_state", CROSS_FILE,
    "状态机应该拒绝非法迁移。",
    {
        "state.py": "VALID = {('new', 'paid'), ('paid', 'shipped')}\n\ndef transition(current, target):\n    return target\n",
        "service.py": "from state import transition\n\ndef advance(order, target):\n    order['status'] = transition(order['status'], target)\n    return order\n",
    },
    {"test_workflow.py": "import unittest\nfrom service import advance\n\nclass T(unittest.TestCase):\n    def test_valid(self):\n        self.assertEqual(advance({'status': 'new'}, 'paid')['status'], 'paid')\n    def test_invalid(self):\n        with self.assertRaises(ValueError):\n            advance({'status': 'new'}, 'shipped')\n"},
    {
        "state.py": "VALID = {('new', 'paid'), ('paid', 'shipped')}\n\ndef transition(current, target):\n    if (current, target) not in VALID:\n        raise ValueError(f'invalid transition: {current} -> {target}')\n    return target\n",
        "service.py": "from state import transition\n\ndef advance(order, target):\n    order['status'] = transition(order['status'], target)\n    return order\n",
    },
    tier=LONG,
)


# ---------------------------------------------------------------------------
# 并发 / IO（6）
# ---------------------------------------------------------------------------

TASKS_FULL.append(EvalTask(
    id="full/concurrency/counter_lock",
    category=DEBUG,
    prompt=_prompt("Counter.increment 在多线程下不能丢增量。"),
    sources={"thread_counter.py": "import threading\n\nclass Counter:\n    def __init__(self):\n        self.value = 0\n\n    def increment(self):\n        current = self.value\n        self.value = current\n"},
    tests={"test_thread_counter.py": "import threading, unittest\nfrom thread_counter import Counter\n\nclass T(unittest.TestCase):\n    def test_threads(self):\n        counter = Counter()\n        threads = [threading.Thread(target=lambda: [counter.increment() for _ in range(2000)]) for _ in range(4)]\n        for t in threads: t.start()\n        for t in threads: t.join()\n        self.assertEqual(counter.value, 8000)\n"},
    reference={"thread_counter.py": "import threading\n\nclass Counter:\n    def __init__(self):\n        self.value = 0\n        self._lock = threading.Lock()\n\n    def increment(self):\n        with self._lock:\n            self.value += 1\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/concurrency/retry_backoff",
    category=DEBUG,
    prompt=_prompt("retry 应该在失败后重试。"),
    sources={"retry_util.py": "def retry(func, attempts):\n    return func()\n"},
    tests={"test_retry_util.py": "import unittest\nfrom retry_util import retry\n\nclass T(unittest.TestCase):\n    def test_retry(self):\n        calls = []\n        def flaky():\n            calls.append(1)\n            if len(calls) < 3:\n                raise RuntimeError('boom')\n            return 'ok'\n        self.assertEqual(retry(flaky, 5), 'ok')\n        self.assertEqual(len(calls), 3)\n"},
    reference={"retry_util.py": "def retry(func, attempts):\n    last = None\n    for _ in range(attempts):\n        try:\n            return func()\n        except Exception as exc:\n            last = exc\n    raise last\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/concurrency/bounded_queue",
    category=DEBUG,
    prompt=_prompt("Queue.push 超过容量时应该抛错。"),
    sources={"queue_util.py": "class Queue:\n    def __init__(self, capacity):\n        self.capacity = capacity\n        self.items = []\n\n    def push(self, value):\n        self.items.append(value)\n"},
    tests={"test_queue_util.py": "import unittest\nfrom queue_util import Queue\n\nclass T(unittest.TestCase):\n    def test_capacity(self):\n        q = Queue(1)\n        q.push(1)\n        with self.assertRaises(ValueError):\n            q.push(2)\n"},
    reference={"queue_util.py": "class Queue:\n    def __init__(self, capacity):\n        self.capacity = capacity\n        self.items = []\n\n    def push(self, value):\n        if len(self.items) >= self.capacity:\n            raise ValueError('full')\n        self.items.append(value)\n"},
))
TASKS_FULL.append(EvalTask(
    id="full/concurrency/append_newline",
    category=DEBUG,
    prompt=_prompt("append_line 每行都应该以换行结尾。"),
    sources={"append_util.py": "def append_line(path, text):\n    with open(path, 'a', encoding='utf-8') as fh:\n        fh.write(text)\n"},
    tests={"test_append_util.py": "import os, tempfile, unittest\nfrom append_util import append_line\n\nclass T(unittest.TestCase):\n    def test_lines(self):\n        fd, path = tempfile.mkstemp()\n        os.close(fd)\n        try:\n            append_line(path, 'a')\n            append_line(path, 'b')\n            with open(path, encoding='utf-8') as fh:\n                self.assertEqual(fh.read(), 'a\\nb\\n')\n        finally:\n            os.unlink(path)\n"},
    reference={"append_util.py": "def append_line(path, text):\n    with open(path, 'a', encoding='utf-8') as fh:\n        fh.write(text + '\\n')\n"},
))
_simple(
    "full/concurrency/merge_results", DEBUG, "merge_results", "left, right",
    "return left",
    "result = dict(left)\nresult.update(right)\nreturn result",
    [("{'a': 1}, {'b': 2}", {"a": 1, "b": 2})],
)
_simple(
    "full/concurrency/rate_window", DEBUG, "allow", "timestamps, now, limit, window",
    "return len(timestamps) < limit",
    "recent = [stamp for stamp in timestamps if now - stamp <= window]\nreturn len(recent) < limit",
    [("[1, 2, 100, 101], 102, 2, 5", False), ("[1, 2, 100, 101], 102, 3, 5", True)],

)


# ---------------------------------------------------------------------------
# 重构（4）
# ---------------------------------------------------------------------------

_multi(
    "full/refactor/rename_function", CROSS_FILE,
    "把 old_name 重命名为 new_name，并同步调用方。",
    {
        "lib.py": "def old_name(value):\n    return value + 1\n",
        "app.py": "from lib import old_name\n\ndef run(value):\n    return old_name(value)\n",
    },
    {"test_app.py": "import unittest\nfrom app import run\nfrom lib import new_name\n\nclass T(unittest.TestCase):\n    def test_new_name(self):\n        self.assertEqual(run(1), 2)\n        self.assertEqual(new_name(1), 2)\n        with self.assertRaises(ImportError):\n            from lib import old_name  # noqa: F401\n"},
    {
        "lib.py": "def new_name(value):\n    return value + 1\n",
        "app.py": "from lib import new_name\n\ndef run(value):\n    return new_name(value)\n",
    },
)
_multi(
    "full/refactor/extract_helper", CROSS_FILE,
    "把重复计算抽成 helper，并保持行为不变。",
    {
        "calc.py": "def area(width, height):\n    return width * height\n\ndef volume(width, height, depth):\n    return width * height * depth\n",
    },
    {"test_calc.py": "import unittest\nimport calc\n\nclass T(unittest.TestCase):\n    def test_behavior(self):\n        self.assertEqual(calc.area(2, 3), 6)\n        self.assertEqual(calc.volume(2, 3, 4), 24)\n        self.assertTrue(hasattr(calc, 'base_area'))\n"},
    {"calc.py": "def base_area(width, height):\n    return width * height\n\ndef area(width, height):\n    return base_area(width, height)\n\ndef volume(width, height, depth):\n    return base_area(width, height) * depth\n"},
)
_multi(
    "full/refactor/move_class", CROSS_FILE,
    "把 Engine 移到 engines.py，并更新导入。",
    {
        "engine.py": "class Engine:\n    def run(self):\n        return 'ok'\n",
        "app.py": "from engine import Engine\n\ndef start():\n    return Engine().run()\n",
    },
    {"test_app.py": "import unittest\nfrom app import start\nfrom engines import Engine\n\nclass T(unittest.TestCase):\n    def test_move(self):\n        self.assertEqual(start(), 'ok')\n        self.assertEqual(Engine().run(), 'ok')\n"},
    {
        "engines.py": "class Engine:\n    def run(self):\n        return 'ok'\n",
        "app.py": "from engines import Engine\n\ndef start():\n    return Engine().run()\n",
    },
)
_multi(
    "full/refactor/signature_change", CROSS_FILE,
    "format_user 改为关键字参数并更新调用方。",
    {
        "formatting.py": "def format_user(name, age):\n    return f'{name}:{age}'\n",
        "app.py": "from formatting import format_user\n\ndef show(name, age):\n    return format_user(age, name)\n",
    },
    {"test_app.py": "import unittest\nfrom app import show\nfrom formatting import format_user\n\nclass T(unittest.TestCase):\n    def test_keywords(self):\n        self.assertEqual(show('a', 1), 'a:1')\n        self.assertEqual(format_user(name='a', age=1), 'a:1')\n"},
    {
        "formatting.py": "def format_user(*, name, age):\n    return f'{name}:{age}'\n",
        "app.py": "from formatting import format_user\n\ndef show(name, age):\n    return format_user(name=name, age=age)\n",
    },
)
