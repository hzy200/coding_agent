"""AST 符号检索（LSP-lite）。"""

from __future__ import annotations

from coding_agent.codeintel.symbols import (
    extract_symbols,
    render_symbols,
    search_symbols,
)

SOURCES = {
    "app/service.py": (
        "class DiscountService:\n"
        "    def apply(self):\n"
        "        return 1\n"
        "\n"
        "def helper():\n"
        "    return 2\n"
    ),
    "app/other.py": "async def fetch():\n    return 3\n",
}


def test_extracts_classes_methods_and_functions() -> None:
    symbols = extract_symbols(SOURCES)
    names = {symbol.qualified for symbol in symbols}

    assert names == {
        "DiscountService",
        "DiscountService.apply",
        "helper",
        "fetch",
    }


def test_search_matches_name_fragment_and_counts_line() -> None:
    matches = search_symbols(SOURCES, "discount")

    # 类本身和它的方法都会命中（qualified name 包含 DiscountService）。
    assert [item.qualified for item in matches] == [
        "DiscountService",
        "DiscountService.apply",
    ]
    assert matches[0].path == "app/service.py"
    assert matches[0].line == 1
    assert matches[0].kind == "class"


def test_search_is_case_insensitive_and_limited() -> None:
    matches = search_symbols(SOURCES, "e", limit=2)

    assert len(matches) == 2


def test_render_symbols_includes_location_and_kind() -> None:
    text = render_symbols(search_symbols(SOURCES, "apply"))

    assert "app/service.py:2" in text
    assert "[method]" in text
    assert "DiscountService.apply" in text
