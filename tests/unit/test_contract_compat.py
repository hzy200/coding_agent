"""契约兼容性规则的单测。

`gen_contracts.py --check-compat` 的判定逻辑是安全/兼容门禁的一部分，
不能只靠手工跑一次验证。这里直接测纯函数，不依赖 git 基线。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "gen_contracts", ROOT / "scripts" / "gen_contracts.py"
)
assert _SPEC is not None and _SPEC.loader is not None
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)


def test_removing_an_event_or_field_is_breaking() -> None:
    old = {"events": [{"event_type": "a", "fields": [{"name": "x", "type": "str"}]}]}
    assert gen._diff_events(old, {"events": []})
    assert gen._diff_events(old, {"events": [{"event_type": "a", "fields": []}]})


def test_changing_an_event_field_type_is_breaking() -> None:
    old = {"events": [{"event_type": "a", "fields": [{"name": "x", "type": "str"}]}]}
    new = {"events": [{"event_type": "a", "fields": [{"name": "x", "type": "int"}]}]}
    assert any("类型" in item for item in gen._diff_events(old, new))


def test_removing_a_config_item_is_breaking_and_default_change_warns() -> None:
    old = {"env_vars": ["A"], "fields": [{"env": "A", "type": "str", "default": "1"}]}
    problems, _ = gen._diff_config(old, {"env_vars": [], "fields": []})
    assert problems

    changed_default = {
        "env_vars": ["A"],
        "fields": [{"env": "A", "type": "str", "default": "2"}],
    }
    problems, warnings = gen._diff_config(old, changed_default)
    assert not problems
    assert warnings


def test_lowering_a_tool_level_is_breaking_but_raising_warns() -> None:
    old = {"tools": [{"tool": "t", "level": 3}]}
    problems, _ = gen._diff_tools(old, {"tools": [{"tool": "t", "level": 0}]})
    assert problems

    old_permissive = {"tools": [{"tool": "t", "level": 0}]}
    problems, warnings = gen._diff_tools(
        old_permissive, {"tools": [{"tool": "t", "level": 3}]}
    )
    assert not problems
    assert warnings


def test_removing_audit_kinds_decisions_or_fields_is_breaking() -> None:
    old = {
        "kinds": [{"kind": "k"}],
        "decisions": [{"value": "auto"}],
        "field_groups": [{"group": "g", "fields": [{"name": "f"}]}],
    }
    new = {"kinds": [], "decisions": [], "field_groups": []}
    assert len(gen._diff_audit(old, new)) == 3
