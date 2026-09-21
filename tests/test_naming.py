"""The provider name table, and the rule that keeps it the only one.

``test_no_module_writes_a_provider_name_of_its_own`` is the reason this file
exists: the six names used to be written out in nine places, and every one of
them was correct on the day it was written. Pinning the table alone would not
have caught that.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from aigauge import naming
from aigauge.config import BrowserAccount, Config, account_display_name, display_name_for_account

SRC = Path(__file__).resolve().parent.parent / "src" / "aigauge"


# id -> (full, compact, abbrev). Written out rather than derived, so a table
# edit has to be made twice and on purpose.
EXPECTED = {
    "claude": ("Anthropic · Claude", "Claude", "Cl"),
    "codex": ("OpenAI · ChatGPT + Codex", "Codex", "Cx"),
    "opencode_go": ("OpenCode", "OpenCode", "Go"),
    "azure": ("Microsoft · Azure", "Azure", "Az"),
    "copilot": ("GitHub · Copilot", "Copilot", "Cp"),
    "openrouter": ("OpenRouter", "OpenRouter", "OR"),
}


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_every_kind_has_its_three_names(kind):
    expected_full, expected_compact, expected_abbrev = EXPECTED[kind]
    assert naming.full(kind) == expected_full
    assert naming.compact(kind) == expected_compact
    assert naming.abbrev(kind) == expected_abbrev


def test_the_table_covers_exactly_the_shipped_providers():
    assert set(naming.KINDS) == set(EXPECTED)
    assert sorted(naming.COMPANY) == sorted(naming.KINDS)
    assert sorted(naming.SURFACE) == sorted(naming.KINDS)
    assert sorted(naming.COMPACT) == sorted(naming.KINDS)
    assert sorted(naming.ABBREV) == sorted(naming.KINDS)


def test_kinds_is_the_order_the_user_sees():
    """Anthropic, OpenAI, OpenCode, Microsoft, GitHub, OpenRouter. The panel
    stacks the tiles in this order and Settings lists its tabs in it."""
    assert naming.KINDS == (
        "claude",
        "codex",
        "opencode_go",
        "azure",
        "copilot",
        "openrouter",
    )


def test_the_separator_is_a_middle_dot_with_spaces():
    assert naming.SEPARATOR == " · "
    assert naming.full("azure") == "Microsoft" + naming.SEPARATOR + "Azure"


@pytest.mark.parametrize(
    "account_id,kind",
    [
        ("claude", "claude"),
        ("claude-3f9a12cd", "claude"),
        ("claude-", "claude"),
        ("codex", "codex"),
        ("codex-work", "codex"),
        ("opencode_go", "opencode_go"),
        ("azure", "azure"),
        ("copilot", "copilot"),
        ("openrouter", "openrouter"),
    ],
)
def test_kind_of_maps_an_account_id_to_its_family(account_id, kind):
    assert naming.kind_of(account_id) == kind
    assert naming.full(account_id) == naming.full(kind)
    assert naming.compact(account_id) == naming.compact(kind)
    assert naming.abbrev(account_id) == naming.abbrev(kind)


def test_kind_of_leaves_an_unrelated_id_alone():
    """"claudette" is not a Claude account, and neither is "" - the prefix rule
    is "the kind, or the kind and a hyphen", not "starts with these letters"."""
    assert naming.kind_of("claudette") == "claudette"
    assert naming.kind_of("codexy") == "codexy"
    assert naming.kind_of("") == ""
    assert naming.kind_of(None) == ""


def test_an_unknown_kind_still_renders_something_readable():
    """A provider added without touching the table must not raise inside a
    paint, and must not show the user a raw id with an underscore in it."""
    assert naming.full("brand_new") == "Brand New"
    assert naming.compact("brand_new") == "Brand New"
    # No guessed abbreviation: the menu bar keeps its own two-letter fallback.
    assert naming.abbrev("brand_new") == ""


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_account_label_keeps_the_company_and_brackets_the_users_name(kind):
    assert naming.account_label(kind, "Work") == f"{naming.full(kind)} (Work)"
    assert naming.account_label(kind, "  Work  ") == f"{naming.full(kind)} (Work)"
    assert naming.account_label(kind, "") == naming.full(kind)
    assert naming.account_label(kind, "   ") == naming.full(kind)
    assert naming.account_label(kind, None) == naming.full(kind)


def test_the_longest_named_account_reads_as_the_user_decided():
    assert naming.account_label("codex", "Work") == "OpenAI · ChatGPT + Codex (Work)"
    assert naming.account_label("claude", "Work") == "Anthropic · Claude (Work)"


def test_config_composes_account_names_from_the_same_table():
    """config is the only module allowed to prefer an account's own name; the
    name it falls back to is this table's."""
    config = Config()
    config.browser_accounts.append(
        BrowserAccount(id="codex-work", kind="codex", name="Work")
    )

    assert account_display_name(
        BrowserAccount(id="codex-work", kind="codex", name="Work")
    ) == naming.account_label("codex", "Work")
    assert display_name_for_account(config, "codex-work") == naming.account_label(
        "codex", "Work"
    )
    for kind in naming.KINDS:
        assert display_name_for_account(config, kind) == naming.full(kind)


# --- the consistency rule --------------------------------------------------


def _module_string_literals(path: Path) -> list[tuple[int, str]]:
    """Every plain string literal in a module, f-string pieces excluded.

    An f-string's constant pieces are fragments of a composed sentence
    ("Azure {field} must be a GUID"), not names - counting them would flag
    every message that happens to start with a vendor's name and teach the
    next reader to silence this test.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    inside_fstring = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for child in ast.walk(node):
                inside_fstring.add(id(child))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in inside_fstring
    ]


def _modules_under_test() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py")) if p.name != "naming.py"]


def _is_a_provider_display_name(value: str) -> bool:
    """Whether a literal *is* one of the six names, full or compact.

    The two real tests and the self-test below all call this rather than each
    spelling the comparison out: a self-test that re-implements the filter it
    guards would go on passing after the filter it guards was loosened.
    """
    names = {naming.full(k) for k in naming.KINDS} | {
        naming.compact(k) for k in naming.KINDS
    }
    return value in names


def _joins_a_vendor_to_a_surface(value: str) -> bool:
    """Whether a literal puts a vendor on either side of ``SEPARATOR``."""
    vendors = {c for c in naming.COMPANY.values() if c} | set(naming.SURFACE.values())
    return naming.SEPARATOR in value and bool(
        {part.strip() for part in value.split(naming.SEPARATOR)} & vendors
    )


def test_the_scan_actually_sees_the_source():
    """A guard on the guard: a bad path would make the two tests below pass by
    finding nothing at all."""
    modules = _modules_under_test()
    assert len(modules) > 20
    assert any(p.name == "widget.py" for p in modules)
    assert sum(len(_module_string_literals(p)) for p in modules) > 500


def test_no_module_writes_a_provider_name_of_its_own():
    """No display literal outside ``naming.py``.

    Every full and compact name, matched exactly: a module that needs one asks
    ``naming`` for it. This is what stops a seventh copy of "Microsoft · Azure"
    appearing in the module that needs it next.
    """
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}: {value!r}"
        for path in _modules_under_test()
        for lineno, value in _module_string_literals(path)
        if _is_a_provider_display_name(value)
    ]
    assert offenders == []


def test_no_module_joins_a_company_to_a_surface_itself():
    """``SEPARATOR`` is a general-purpose UI separator - the metric-row splitter
    in ``widget`` uses it, so do the ratio captions - so the rule is not "no
    middle dot". It is that no literal may put a *vendor* on either side of one:
    that string is a provider name, and provider names come from one table.
    """
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}: {value!r}"
        for path in _modules_under_test()
        for lineno, value in _module_string_literals(path)
        if _joins_a_vendor_to_a_surface(value)
    ]
    assert offenders == []


def test_the_consistency_rule_would_catch_a_regression(tmp_path):
    """The rule itself, against a module that breaks it both ways.

    Through the same two predicates the tests above use, so loosening one of
    them fails here too. Re-spelling the filters here instead would leave this
    passing over a rule that no longer refuses anything.
    """
    module = tmp_path / "offender.py"
    module.write_text(
        'HEADER = "Microsoft · Azure"\nCHIP = "Copilot"\n', encoding="utf-8"
    )
    literals = _module_string_literals(module)

    assert [v for _, v in literals if _is_a_provider_display_name(v)] == [
        "Microsoft · Azure",
        "Copilot",
    ]
    assert [v for _, v in literals if _joins_a_vendor_to_a_surface(v)] == [
        "Microsoft · Azure"
    ]
    # And that they refuse what they should: a sentence that merely starts
    # with a vendor's name is not a display literal, and a middle dot with no
    # vendor beside it is the general-purpose UI separator.
    assert not _is_a_provider_display_name("Microsoft · Azure accounts")
    assert not _joins_a_vendor_to_a_surface("error · rate limited")


def test_the_composed_dialog_titles_carry_the_full_name(qtbot):
    """The one surface the consistency rule is blind to, pinned by hand.

    That rule matches a literal *equal* to a display name, so the three
    titles this replaced - "Claude.ai session cookie" and its siblings -
    could come back tomorrow without it noticing: they contain a name, they
    are not one. Widening the rule to containment would flag hundreds of
    innocent sentences, so this is the trade: one assertion per title.
    """
    from aigauge.cookie_dialog import CookieDialog

    for provider in ("claude", "codex", "opencode_go"):
        dialog = CookieDialog(provider, display_name=None)
        qtbot.addWidget(dialog)
        assert dialog.windowTitle() == f"Paste {naming.full(provider)} session cookie"

    named = CookieDialog(
        "claude", display_name=naming.account_label("claude", "Work")
    )
    qtbot.addWidget(named)
    assert named.windowTitle() == "Paste Anthropic · Claude (Work)"
