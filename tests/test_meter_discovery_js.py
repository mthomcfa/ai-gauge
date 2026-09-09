"""The catalog scan and the weekly discovery scan, executed in node.

Same posture as ``test_claude_row_reading.py``: the real functions are pulled
out of the production extractor source and run against a stub DOM. Asserting
that the source *mentions* a catalog would pass on a comment, which is the
failure mode this repo has already shipped three times.

The stub DOM is a real tree — each node carries its OWN text and names its
parent, and an ancestor's ``innerText`` is derived from its descendants, the
way a browser renders it. Both discovery scans are defined by containment: a
percentage in the sidebar or the task rail is not a meter, and where it sits
is the only way to tell. Hand-written ancestor text could silently disagree
with its children, which is precisely the relationship under test.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from aigauge.providers.catalog import (
    SOURCE_BUNDLED,
    SOURCE_DISCOVERY,
    MeterCatalog,
    MeterSpec,
    bundled_catalog,
    extractor_source,
)
from aigauge.providers.claude import EXTRACTOR_TEMPLATE as CLAUDE_TEMPLATE
from aigauge.providers.codex import EXTRACTOR_TEMPLATE as CODEX_TEMPLATE

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)

_DOM_STUB = r"""
const RAW = %(dom)s;
const NODES = RAW.map((n, i) => ({
  _own: n[0],
  _children: [],
  tagName: 'DIV',
  className: '',
  _i: i,
  _parent: n[2],
  getAttribute: () => null,
  getBoundingClientRect: () => ({ height: n[1] }),
}));
NODES.forEach(el => {
  el.parentElement = el._parent === null ? null : NODES[el._parent];
  if (el.parentElement) el.parentElement._children.push(el);
  el.contains = other => {
    let cur = other;
    while (cur) {
      if (cur === el) return true;
      cur = cur.parentElement;
    }
    return false;
  };
});
// Derived, not declared: a node's rendered text is its own text plus its
// descendants', so a container cannot be given text its children do not have.
function derive(el) {
  return [el._own].concat(el._children.map(derive))
    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
NODES.forEach(el => { el.innerText = derive(el); el.textContent = el.innerText; });
globalThis.document = {
  querySelectorAll: () => NODES,
  querySelector: () => null,
  title: 'stub',
  body: NODES[0],
  documentElement: null,
};
"""


def _block(template: str, catalog: MeterCatalog, start: str) -> str:
    """Everything from ``start`` to the top-level bodyText assignment.

    ``rindex``: Codex's windowTextAfterLabel declares a local ``bodyText`` of
    its own, so the *first* occurrence lands inside a function body and slices
    the block in half.
    """
    source = extractor_source(template, catalog, discover=True)
    block = source[source.index(start) : source.rindex("const bodyText")]
    assert block.count("{") == block.count("}"), "the extracted block is not balanced"
    return block


def _run(js: str, dom: list[tuple[str, int, int | None]], expression: str):
    script = (_DOM_STUB % {"dom": json.dumps(dom)}) + js + (
        f"\nprocess.stdout.write(JSON.stringify({expression}));"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --- Claude ----------------------------------------------------------------
#
# The body (index 0) holding the usage panel, the panel holding one element per
# rendered row, and a row outside the panel carrying a percentage of its own.

CLAUDE_DOM: list[tuple[str, int, int | None]] = [
    ("", 900, None),
    ("Plan usage", 600, 0),
    ("Current session Resets in 2 hr 59 min 64% used", 40, 1),
    ("Weekly Resets in 3 days 30% used", 40, 1),
    ("Opus only Resets in 3 days 91% used", 40, 1),
    ("Sonnet only 44% used", 40, 1),
    ("Cowork sessions 7% used", 40, 1),
    ("Daily included routine runs Resets in 5 hr 12% used", 40, 1),
    ("Upgrade to Max 20% off", 40, 1),
    # Outside the usage panel entirely.
    ("Storage 88% used", 40, 0),
]


def _claude_block(catalog: MeterCatalog | None = None) -> str:
    return _block(CLAUDE_TEMPLATE, catalog or bundled_catalog("claude"), "const ROW_LABELS")


def test_every_catalog_meter_the_page_renders_gets_its_own_row():
    rows = _run(_claude_block(), CLAUDE_DOM, "readCatalogRows({})")

    assert {key: row["percent"] for key, row in rows.items()} == {
        "session": 64,
        "weekly_all": 30,
        "opus_only": 91,
        "sonnet_only": 44,
        "daily_routine_runs": 12,
    }


def test_the_seeded_primary_rows_are_never_re_read():
    """The two primary rows stay the primary path, exactly as before."""
    rows = _run(
        _claude_block(),
        CLAUDE_DOM,
        "readCatalogRows({session: {percent: 1, seeded: true}})",
    )

    assert rows["session"] == {"percent": 1, "seeded": True}


def test_a_relabelled_primary_meter_is_recovered_by_adding_an_alias():
    """What the override file buys: a page relabel is a data change."""
    dom = [
        ("", 900, None),
        ("Plan usage", 600, 0),
        ("Session limit 64% used", 40, 1),
        ("Weekly Resets in 3 days 30% used", 40, 1),
    ]
    relabelled = MeterCatalog(
        kind="claude",
        specs=tuple(
            spec if spec.key != "session"
            else MeterSpec(
                key="session",
                label="Session",
                aliases=("Current session", "Session limit"),
                window=spec.window,
                primary=True,
            )
            for spec in bundled_catalog("claude").specs
        ),
    )

    before = _run(_claude_block(), dom, "readCatalogRows({})")
    after = _run(_claude_block(relabelled), dom, "readCatalogRows({})")

    assert "session" not in before
    assert after["session"]["percent"] == 64
    assert after["session"]["kind"] == "used"


def _with_extra(source: str) -> MeterCatalog:
    return MeterCatalog(
        kind="claude",
        specs=bundled_catalog("claude").specs
        + (
            MeterSpec(
                key="cowork_sessions",
                label="Cowork sessions",
                aliases=("Cowork sessions",),
                source=source,
            ),
        ),
    )


# A layout that collapses two meters into one element. Claude has shipped
# rows like this; it is also the only shape in which the rival-label rule can
# fire, because a leaf element isolating one meter has one percentage.
_COLLAPSED_DOM: list[tuple[str, int, int | None]] = [
    ("", 900, None),
    ("Plan usage", 600, 0),
    ("Cowork sessions 7% used Current session Resets in 2 hr 59 min 64% used", 40, 1),
    ("Weekly Resets in 3 days 30% used", 40, 1),
]


# The same collapsed element with the two meters the other way round. This is
# the shape that decides the rule: readRowText takes the LAST percentage, so
# here the adopted meter's number is the one Session walks off with.
_COLLAPSED_DOM_SWAPPED: list[tuple[str, int, int | None]] = [
    ("", 900, None),
    ("Plan usage", 600, 0),
    ("Current session Resets in 2 hr 59 min 64% used Cowork sessions 7% used", 40, 1),
    ("Weekly Resets in 3 days 30% used", 40, 1),
]


@pytest.mark.parametrize("dom", [_COLLAPSED_DOM, _COLLAPSED_DOM_SWAPPED])
def test_an_adopted_label_makes_a_shared_container_ambiguous(dom):
    """ROW_LABELS is the rival set, and a rival costs the row its number.

    An adopted meter sharing a collapsed container with Session must cost
    Session its percentage, not hand it the adopted meter's: readRowText takes
    the LAST percentage in the container, so with "Cowork sessions" out of the
    rival set the primary Session row reported 7% used as an OK snapshot.
    Refusing is recoverable; a plausible wrong number is not.
    """
    rows = _run(_claude_block(_with_extra(SOURCE_DISCOVERY)), dom,
                "readCatalogRows({})")

    assert rows["session"]["ambiguous"] is True
    assert rows["session"]["percent"] is None


def test_an_adopted_label_still_leaves_every_row_readable_on_the_clean_page():
    """The cost of the rule, measured: on a page that isolates its rows, none.

    Every meter, primary and adopted alike, still reads its own number - the
    rival rule only fires where two meters share one container and there is
    genuinely no way to tell which percentage is whose.
    """
    rows = _run(_claude_block(_with_extra(SOURCE_DISCOVERY)), CLAUDE_DOM,
                "readCatalogRows({})")

    assert {key: row["percent"] for key, row in rows.items()} == {
        "session": 64,
        "weekly_all": 30,
        "opus_only": 91,
        "sonnet_only": 44,
        "daily_routine_runs": 12,
        "cowork_sessions": 7,
    }
    assert not any(row["ambiguous"] for row in rows.values())


def test_a_bundled_rival_still_refuses_to_attribute_the_percentage():
    """The control: the attribution rule itself is untouched.

    The same DOM and the same label, shipped rather than discovered, still
    refuses to guess which meter the number belongs to.
    """
    rows = _run(_claude_block(_with_extra(SOURCE_BUNDLED)), _COLLAPSED_DOM,
                "readCatalogRows({})")

    assert rows["session"]["ambiguous"] is True
    assert rows["session"]["percent"] is None


def test_discovery_returns_the_rows_the_catalog_does_not_know():
    discovered = _run(_claude_block(), CLAUDE_DOM, "discoverRows()")
    labels = [row["label"] for row in discovered]

    assert "Cowork sessions" in labels, "a new meter must reach Python"
    assert all(row["in_container"] for row in discovered)


def test_discovery_never_leaves_the_usage_container():
    discovered = _run(_claude_block(), CLAUDE_DOM, "discoverRows()")

    assert "Storage" not in [row["label"] for row in discovered]


def test_discovery_skips_an_element_wrapping_several_meters():
    """A wrapper's percentages belong to its children, and they are candidates
    in their own right."""
    discovered = _run(_claude_block(), CLAUDE_DOM, "discoverRows()")

    assert not any(row["label"].startswith("Plan usage") for row in discovered)


# The shape that made `in_container` meaningless: the marker phrase renders in
# the settings nav, outside the panel, so the smallest element holding it and
# two percentages was <body> - and every percentage on the page is inside
# <body>.
CLAUDE_NAV_DOM: list[tuple[str, int, int | None]] = [
    ("", 900, None),                                              # 0 body
    ("", 200, 0),                                                 # 1 settings nav
    ("Plan usage", 20, 1),                                        # 2 nav item
    ("Account", 20, 1),                                           # 3 nav item
    ("", 600, 0),                                                 # 4 the usage panel
    ("Usage", 20, 4),                                             # 5 panel heading
    ("Current session Resets in 2 hr 59 min 64% used", 40, 4),    # 6
    ("Weekly Resets in 3 days 30% used", 40, 4),                  # 7
    ("Cowork sessions 7% used", 40, 4),                           # 8
    ("", 100, 0),                                                 # 9 rest of the page
    ("Storage 88% used", 20, 9),                                  # 10
    ("Referral bonus 15% off", 20, 9),                            # 11
    ("Save 20% on Max", 20, 9),                                   # 12
]


def test_the_container_is_the_panel_when_the_marker_also_sits_in_the_nav():
    assert _run(_claude_block(), CLAUDE_NAV_DOM, "usageContainer()._i") == 4


def test_furniture_outside_the_panel_is_not_discovered():
    discovered = _run(_claude_block(), CLAUDE_NAV_DOM, "discoverRows()")
    labels = [row["label"] for row in discovered]

    assert "Cowork sessions" in labels
    assert not {"Storage", "Referral bonus", "Save"} & set(labels)


def test_a_relabelled_primary_meter_also_relocates_the_panel():
    """The catalog is how a relabel is fixed, so the marker follows it.

    Nothing in this DOM matches the fixed marker phrases; the panel is found
    through the primary aliases the catalog injects.
    """
    dom = [
        ("", 900, None),
        ("Usage", 600, 0),
        ("Session limit 64% used", 40, 1),
        ("7 day limit 30% used", 40, 1),
        ("Storage 88% used", 40, 0),
    ]
    relabelled = MeterCatalog(
        kind="claude",
        specs=(
            MeterSpec(key="session", label="Session", aliases=("Session limit",),
                      primary=True),
            MeterSpec(key="weekly_all", label="Weekly", aliases=("7 day limit",),
                      primary=True),
        ),
    )

    assert _run(_claude_block(relabelled), dom, "usageContainer()._i") == 1


def test_a_container_that_swallowed_the_page_is_refused():
    """No container beats a container of everything: discovery then finds
    nothing rather than adopting the page around the panel."""
    dom = [
        ("", 900, None),
        # A wrapper that is mostly page copy: two rows and the whole article
        # around them.
        ("Plan usage " + "some page copy " * 40, 600, 0),
        ("Current session 64% used", 40, 1),
        ("Weekly 30% used", 40, 1),
    ]

    assert _run(_claude_block(), dom, "usageContainer()") is None
    assert _run(_claude_block(), dom, "discoverRows()") is None


# A nav that carries the marker phrase AND percentages of its own. The panel
# is the bigger element, so "smallest marked element holding two percentages"
# picked the nav — and the real panel was never scanned at all.
CLAUDE_NAV_WITH_FURNITURE: list[tuple[str, int, int | None]] = [
    ("", 900, None),                                              # 0 body
    ("", 200, 0),                                                 # 1 settings nav
    ("Plan usage", 20, 1),                                        # 2 nav item
    ("Storage 88% used", 20, 1),                                  # 3
    ("Cache 12% used", 20, 1),                                    # 4
    ("", 600, 0),                                                 # 5 the usage panel
    ("Current session Resets in 2 hr 59 min 64% used", 40, 5),    # 6
    ("Weekly Resets in 3 days 30% used", 40, 5),                  # 7
    ("Cowork sessions 7% used", 40, 5),                           # 8
]


def test_a_smaller_marked_region_outside_the_panel_does_not_win():
    """A bare marker mention is a nav item, not a panel.

    The nav holds the phrase and two percentages in 42 characters; the panel
    needs a hundred. Anchoring on the marker alone therefore preferred the
    nav, and Storage/Cache became the meters while Session and Weekly were
    never looked at.
    """
    assert _run(_claude_block(), CLAUDE_NAV_WITH_FURNITURE, "usageContainer()._i") == 5

    labels = [
        row["label"]
        for row in _run(_claude_block(), CLAUDE_NAV_WITH_FURNITURE, "discoverRows()")
    ]
    assert "Cowork sessions" in labels
    assert not {"Storage", "Cache"} & set(labels)


# The SPA wrapper: <body> is refused outright, but div#root is not <body>, and
# a panel rendering fewer than two percentages sends the climb straight past
# it into the wrapper that holds the whole application.
CLAUDE_SPA_ROOT_DOM: list[tuple[str, int, int | None]] = [
    ("", 900, None),                                              # 0 body
    ("", 880, 0),                                                 # 1 div#root
    ("", 100, 1),                                                 # 2 settings nav
    ("Plan usage", 20, 2),                                        # 3 nav item
    ("", 200, 1),                                                 # 4 the usage panel
    ("Current session Resets in 2 hr 59 min 64% used", 40, 4),    # 5 its only meter
    ("", 100, 1),                                                 # 6 rest of the page
    ("Storage 88% used", 20, 6),                                  # 7
    ("Save 20% on Max", 20, 6),                                   # 8
]


def test_a_panel_with_one_meter_does_not_promote_the_spa_root():
    """No container beats a container of the whole application.

    The climb passes the panel (one percentage) and lands on the wrapper under
    <body>, which holds the nav and the footer as well — so every percentage
    on the page reads as `in_container`. The bare "Plan usage" nav item inside
    it, on no path up from the anchor, is the evidence that the climb left the
    panel.
    """
    assert _run(_claude_block(), CLAUDE_SPA_ROOT_DOM, "usageContainer()") is None
    assert _run(_claude_block(), CLAUDE_SPA_ROOT_DOM, "discoverRows()") is None


def _deeply_nested_page_swallower() -> list[tuple[str, int, int | None]]:
    """Two rows, each buried six wrappers deep inside a page-sized element."""
    dom: list[tuple[str, int, int | None]] = [
        ("", 900, None),
        ("Plan usage " + "some page copy " * 40, 600, 0),
    ]
    for text in ("Current session 64% used", "Weekly 30% used"):
        parent = 1
        for _ in range(6):
            dom.append(("", 100, parent))
            parent = len(dom) - 1
        dom.append((text, 40, parent))
    return dom


def test_wrappers_around_a_row_do_not_talk_a_swallowed_page_past_the_ratio():
    """The ratio counts rows, and a wrapper is not a row.

    Every wrapper between a row and the container reports that row's
    percentage too, so summing all single-percentage descendants multiplied
    the rows total by the nesting depth. Six layers was enough to make an
    element that is mostly page copy look like a panel that is mostly rows.
    """
    dom = _deeply_nested_page_swallower()

    assert _run(_claude_block(), dom, "usageContainer()") is None
    assert _run(_claude_block(), dom, "discoverRows()") is None


def test_a_nested_wrapper_does_not_become_a_second_meter():
    """A wrapper that adds a heading in front of one row is that row again.

    Both elements are candidates, both carry the same percentage and reset
    text, and one label ends in the other — so the page would grow a "Included
    Cowork sessions" meter beside "Cowork sessions", reporting one number
    twice.
    """
    dom = [
        ("", 900, None),                                          # 0 body
        ("Plan usage", 600, 0),                                   # 1 panel
        ("Included", 60, 1),                                      # 2 wrapper
        ("Cowork sessions 7% used Resets in 3 days", 30, 2),      # 3 the row
        ("Current session 64% used Resets in 2 hr", 40, 1),       # 4
    ]

    labels = [row["label"] for row in _run(_claude_block(), dom, "discoverRows()")]

    assert "Cowork sessions" in labels
    assert "Included Cowork sessions" not in labels


def test_a_discovered_row_carries_the_same_polarity_verdict_as_a_known_one():
    discovered = _run(_claude_block(), CLAUDE_DOM, "discoverRows()")
    by_label = {row["label"]: row for row in discovered}

    # "20% off" has no used/remaining wording beside it; the row still travels
    # so Python can reject it by label, but its polarity is not guessed.
    assert by_label["Upgrade to Max"]["kind"] == "unknown"
    assert by_label["Cowork sessions"]["kind"] == "used"


@pytest.mark.parametrize("discover,expected", [(False, None), (True, ["scanned"])])
def test_discovery_only_runs_when_the_scan_is_due(discover, expected):
    """Executes the real expression against a stubbed discoverRows.

    Asserting the source string would pass on a comment and fail on
    reformatting - it tests the text, not the behaviour.
    """
    source = extractor_source(
        CLAUDE_TEMPLATE, bundled_catalog("claude"), discover=discover
    )
    start = source.index("const DISCOVER")
    declaration = source[start : source.index(";", start) + 1]
    expression = "DISCOVER ? discoverRows() : null"
    assert expression in source, "the discovery gate changed shape"

    script = (
        f"{declaration}\nconst discoverRows = () => ['scanned'];\n"
        f"process.stdout.write(JSON.stringify({expression}));"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == expected


def test_an_alias_that_looks_like_an_injection_marker_leaves_valid_js(tmp_path):
    """A label is data. Spliced as source it took the extractor out entirely.

    ``node --check`` rather than a string assertion: the failure was a
    SyntaxError, and only a parser can say the source is still a program.
    """
    catalog = MeterCatalog(
        kind="claude",
        specs=(
            MeterSpec(
                key="session",
                label="__AG_CATALOG__",
                aliases=("__AG_CATALOG__", "__AG_ROW_LABELS__"),
                primary=True,
            ),
        ),
    )
    path = tmp_path / "extractor.js"
    path.write_text(
        extractor_source(CLAUDE_TEMPLATE, catalog, discover=True), encoding="utf-8"
    )

    out = subprocess.run(
        ["node", "--check", str(path)], capture_output=True, text=True, timeout=30
    )

    assert out.returncode == 0, out.stderr


# --- Codex -----------------------------------------------------------------

CODEX_DOM: list[tuple[str, int, int | None]] = [
    ("", 900, None),
    ("Personal usage", 600, 0),
    ("5 hour usage limit 42% used Resets 1:55 PM", 40, 1),
    ("Weekly usage limit 61% used Resets Mon 6:00 PM", 40, 1),
    ("Cloud tasks limit 12% used Resets Mon 6:00 PM", 40, 1),
    # The task rail, which sits outside the analytics panel.
    ("Fix the flaky test 90% done", 40, 0),
]


def _codex_block() -> str:
    return _block(CODEX_TEMPLATE, bundled_catalog("codex"), "const CATALOG")


def test_every_catalog_card_the_page_renders_gets_its_own_row():
    rows = _run(_codex_block(), CODEX_DOM, "readCatalogCards({})")

    assert {key: row["percent"] for key, row in rows.items()} == {
        "session": 42,
        "weekly": 61,
    }


def test_codex_discovery_returns_the_cards_the_catalog_does_not_know():
    discovered = _run(_codex_block(), CODEX_DOM, "discoverCards()")
    labels = [row["label"] for row in discovered]

    assert "Cloud tasks limit" in labels
    # A card whose name starts with a digit must not lose its label.
    assert "5 hour usage limit" in labels


def test_codex_discovery_never_leaves_the_usage_container():
    discovered = _run(_codex_block(), CODEX_DOM, "discoverCards()")

    assert "Fix the flaky test" not in [row["label"] for row in discovered]


# The workspace-credit layout: no card says "usage limit", and the only text
# that does is prose sitting above the panel. The old marker resolved to
# <body>, which put the task rail inside the usage container.
CODEX_CREDIT_DOM: list[tuple[str, int, int | None]] = [
    ("", 900, None),                                                    # 0 body
    ("Codex and Work share the same usage limit.", 30, 0),              # 1 prose
    ("", 600, 0),                                                       # 2 the panel
    ("Workspace monthly credit limit 37% used Resets Apr 1", 40, 2),    # 3
    ("Cloud tasks 12% used Resets Mon 6:00 PM", 40, 2),                 # 4
    ("Fix the flaky test 90% done", 40, 0),                             # 5 task rail
]


# The same two escapes on the Codex page: a rail that carries the marker
# wording and percentages of its own, and a panel with a single card sitting
# inside the SPA wrapper.
CODEX_RAIL_WITH_FURNITURE: list[tuple[str, int, int | None]] = [
    ("", 900, None),                                                 # 0 body
    ("", 200, 0),                                                    # 1 side rail
    ("Usage limits", 20, 1),                                         # 2 rail item
    ("Storage 88% used", 20, 1),                                     # 3
    ("Cache 12% used", 20, 1),                                       # 4
    ("", 600, 0),                                                    # 5 the panel
    ("5 hour usage limit 42% used Resets 1:55 PM", 40, 5),           # 6
    ("Weekly usage limit 61% used Resets Mon 6:00 PM", 40, 5),       # 7
    ("Cloud tasks limit 12% used Resets Mon 6:00 PM", 40, 5),        # 8
]


def test_codex_a_smaller_marked_region_outside_the_panel_does_not_win():
    assert _run(_codex_block(), CODEX_RAIL_WITH_FURNITURE, "usageContainer()._i") == 5

    labels = [
        row["label"]
        for row in _run(_codex_block(), CODEX_RAIL_WITH_FURNITURE, "discoverCards()")
    ]
    assert "Cloud tasks limit" in labels
    assert not {"Storage", "Cache"} & set(labels)


CODEX_SPA_ROOT_DOM: list[tuple[str, int, int | None]] = [
    ("", 900, None),                                                 # 0 body
    ("", 880, 0),                                                    # 1 the SPA root
    ("", 100, 1),                                                    # 2 side rail
    ("Usage limits", 20, 2),                                         # 3 rail item
    ("", 200, 1),                                                    # 4 the panel
    ("Weekly usage limit 61% used Resets Mon 6:00 PM", 40, 4),       # 5 its only card
    ("", 100, 1),                                                    # 6 task rail
    ("Fix the flaky test 90% done", 20, 6),                          # 7
    ("Deploy the fix 20% done", 20, 6),                              # 8
]


def test_codex_a_panel_with_one_card_does_not_promote_the_spa_root():
    assert _run(_codex_block(), CODEX_SPA_ROOT_DOM, "usageContainer()") is None
    assert _run(_codex_block(), CODEX_SPA_ROOT_DOM, "discoverCards()") is None


def test_codex_wrappers_do_not_talk_a_swallowed_page_past_the_ratio():
    dom: list[tuple[str, int, int | None]] = [
        ("", 900, None),
        ("Weekly usage limit " + "some page copy " * 40, 600, 0),
    ]
    for text in ("5 hour usage limit 42% used", "Weekly usage limit 61% used"):
        parent = 1
        for _ in range(6):
            dom.append(("", 100, parent))
            parent = len(dom) - 1
        dom.append((text, 40, parent))

    assert _run(_codex_block(), dom, "usageContainer()") is None
    assert _run(_codex_block(), dom, "discoverCards()") is None


def test_the_workspace_credit_layout_still_finds_its_panel():
    assert _run(_codex_block(), CODEX_CREDIT_DOM, "usageContainer()._i") == 2


def test_the_task_rail_is_not_inside_the_workspace_credit_panel():
    discovered = _run(_codex_block(), CODEX_CREDIT_DOM, "discoverCards()")
    labels = [row["label"] for row in discovered]

    assert "Workspace monthly credit limit" in labels
    assert "Fix the flaky test" not in labels
