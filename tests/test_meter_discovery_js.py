"""The catalog scan and the weekly discovery scan, executed in node.

Same posture as ``test_claude_row_reading.py``: the real functions are pulled
out of the production extractor source and run against a stub DOM. Asserting
that the source *mentions* a catalog would pass on a comment, which is the
failure mode this repo has already shipped three times.

The stub DOM is a real tree — each node names its parent — because both
discovery scans are defined by containment: a percentage in the sidebar or the
task rail is not a meter, and where it sits is the only way to tell.
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

_DOM_STUB = """
const RAW = %(dom)s;
const NODES = RAW.map((n, i) => ({
  innerText: n[0],
  textContent: n[0],
  tagName: 'DIV',
  className: '',
  _i: i,
  _parent: n[2],
  getAttribute: () => null,
  getBoundingClientRect: () => ({ height: n[1] }),
}));
NODES.forEach(el => {
  el.parentElement = el._parent === null ? null : NODES[el._parent];
  el.contains = other => {
    let cur = other;
    while (cur) {
      if (cur === el) return true;
      cur = cur.parentElement;
    }
    return false;
  };
});
globalThis.document = {
  querySelectorAll: () => NODES,
  querySelector: () => null,
  title: 'stub',
  body: NODES[0],
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
# One container (index 0) holding one element per rendered row, plus a sidebar
# row outside it carrying a percentage of its own.

CLAUDE_DOM: list[tuple[str, int, int | None]] = [
    ("Plan usage Current session Resets in 2 hr 59 min 64% used "
     "Weekly Resets in 3 days 30% used Opus only Resets in 3 days 91% used "
     "Sonnet only 44% used Cowork sessions 7% used "
     "Daily included routine runs Resets in 5 hr 12% used "
     "Upgrade to Max 20% off", 600, None),
    ("Current session Resets in 2 hr 59 min 64% used", 40, 0),
    ("Weekly Resets in 3 days 30% used", 40, 0),
    ("Opus only Resets in 3 days 91% used", 40, 0),
    ("Sonnet only 44% used", 40, 0),
    ("Cowork sessions 7% used", 40, 0),
    ("Daily included routine runs Resets in 5 hr 12% used", 40, 0),
    ("Upgrade to Max 20% off", 40, 0),
    # Outside the usage panel entirely.
    ("Storage 88% used", 40, None),
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
        ("Plan usage Session limit 64% used Weekly Resets in 3 days 30% used", 600, None),
        ("Session limit 64% used", 40, 0),
        ("Weekly Resets in 3 days 30% used", 40, 0),
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
    ("Plan usage Cowork sessions 7% used Current session Resets in 2 hr 59 min "
     "64% used Weekly Resets in 3 days 30% used", 600, None),
    ("Cowork sessions 7% used Current session Resets in 2 hr 59 min 64% used", 40, 0),
    ("Weekly Resets in 3 days 30% used", 40, 0),
]


def test_an_adopted_label_cannot_make_a_primary_row_unreadable():
    """ROW_LABELS is the rival set, and a rival costs the row its number.

    An adopted label that turns up inside the Session row made the primary
    read `ambiguous` — an ERROR snapshot on every refresh, for as long as the
    entry sat in the override file. Discovered meters are read through
    CATALOG; only the meters this build ships belong in the rival set.
    """
    rows = _run(_claude_block(_with_extra(SOURCE_DISCOVERY)), _COLLAPSED_DOM,
                "readCatalogRows({})")

    assert rows["session"]["ambiguous"] is False
    assert rows["session"]["percent"] == 64


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


# --- Codex -----------------------------------------------------------------

CODEX_DOM: list[tuple[str, int, int | None]] = [
    ("Personal usage 5 hour usage limit 42% used Resets 1:55 PM "
     "Weekly usage limit 61% used Resets Mon 6:00 PM "
     "Cloud tasks limit 12% used Resets Mon 6:00 PM", 600, None),
    ("5 hour usage limit 42% used Resets 1:55 PM", 40, 0),
    ("Weekly usage limit 61% used Resets Mon 6:00 PM", 40, 0),
    ("Cloud tasks limit 12% used Resets Mon 6:00 PM", 40, 0),
    # The task rail, which sits outside the analytics panel.
    ("Fix the flaky test 90% done", 40, None),
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
