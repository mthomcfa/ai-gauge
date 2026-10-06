"""claude.ai's October 2026 usage panel: "This week" and "Fable this week".

From 6 October 2026 the panel's rows read, from Michael's screen:

    Current session    Resets at 11:00 PM                                  20% used
    This week          Resets Saturday 10:00 AM                            73% used
    Fable this week    Separate weekly limit for Fable · Resets Sat 10 AM  14% used

The reader looked for the seven-day meter as "All models", then "Weekly".
Neither labels a row any more, and the only "weekly" on the page is in the
Fable row's description - so the tile showed Fable's 14% as the weekly figure
while the account stood at 73%. A quota monitor reporting a fifth of the real
number is the worst way it can fail, and it did so as an OK snapshot.

These run the whole extractor in node against that panel, as a tree, and pass
its payload through the snapshot builder, so what is asserted is what the tile
would show.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from aigauge.providers.catalog import (
    BREAKDOWN_TAG,
    MeterCatalog,
    MeterSpec,
    bundled_catalog,
    extractor_source,
)
from aigauge.providers.claude import EXTRACTOR_JS, EXTRACTOR_TEMPLATE, _build_snapshot

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)

# (own text, height, parent index). A node's rendered text is its own text
# plus its descendants', derived below, so a container cannot claim text its
# children do not have.
PANEL: list[tuple[str, int, int | None]] = [
    ("", 1300, None),  # 0 body
    ("Settings General Account Privacy Billing Usage Capabilities Memory "
     "Design systems Claude Code Claude in Chrome Customize Skills Connectors "
     "Plugins Platform API keys", 1200, 0),  # 1 settings nav
    ("", 1200, 0),  # 2 the usage view
    ("Your usage Max (20x)", 30, 2),
    ("Heads up. At this pace you’ll run out tomorrow night, before "
     "Saturday’s reset.", 80, 2),
    ("Buy more usage", 40, 2),
    ("", 360, 2),  # 6 the rows
    ("", 70, 6),  # 7 Current session
    ("Current session", 20, 7),
    ("Resets at 11:00 PM", 20, 7),
    ("20% used", 20, 7),
    ("", 70, 6),  # 11 This week
    ("This week", 20, 11),
    ("Resets Saturday 10:00 AM", 20, 11),
    ("73% used", 20, 11),
    ("", 90, 6),  # 15 Fable this week
    ("Fable this week", 20, 15),
    ("Separate weekly limit for Fable · Resets Saturday 10:00 AM", 40, 15),
    ("14% used", 20, 15),
    ("", 260, 2),  # 19 Limit resets
    ("Limit resets", 20, 19),
    ("Full reset Expires Oct 22 Reset for free", 40, 19),
    ("5-hour reset None right now. When you get one, it shows up here.", 40, 19),
    ("Usage credits CA$0 Available for any task. Promotional credits are used "
     "before purchased credits.", 60, 2),
    ("Turn on usage credits to keep using Claude if you hit a plan limit. "
     "Learn more", 40, 2),
]

_STUB = r"""
const RAW = %(dom)s;
const NODES = RAW.map((n, i) => ({
  _own: n[0],
  _children: [],
  _parent: n[2],
  getBoundingClientRect: () => ({ height: n[1] }),
}));
NODES.forEach(el => {
  el.parentElement = el._parent === null ? null : NODES[el._parent];
  if (el.parentElement) el.parentElement._children.push(el);
  el.contains = other => {
    for (let cur = other; cur; cur = cur.parentElement) if (cur === el) return true;
    return false;
  };
});
function derive(el) {
  return [el._own].concat(el._children.map(derive))
    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
NODES.forEach(el => { el.innerText = derive(el); el.textContent = el.innerText; });
globalThis.document = {
  querySelectorAll: () => NODES,
  querySelector: () => null,
  title: 'Claude',
  body: NODES[0],
  documentElement: null,
};
globalThis.window = {};
globalThis.sessionStorage = { getItem: () => null, setItem: () => {} };
globalThis.location = {
  hostname: 'claude.ai', pathname: '/new', hash: '#settings/usage',
  href: 'https://claude.ai/new#settings/usage',
};
"""


def _node(script: str):
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _extract(dom=PANEL, js: str = EXTRACTOR_JS) -> dict:
    """The whole extractor, run against ``dom``: the payload the app gets."""
    return _node(
        (_STUB % {"dom": json.dumps(dom)})
        + "process.stdout.write(JSON.stringify(" + js.strip().rstrip(";") + "));"
    )


def _read_row(label: str, dom=PANEL, catalog: MeterCatalog | None = None):
    """The real readRow, as the shipped extractor defines it."""
    source = extractor_source(
        EXTRACTOR_TEMPLATE, catalog or bundled_catalog("claude"), discover=False
    )
    block = source[source.index("const ROW_LABELS"):source.index("const bodyText")]
    return _node(
        (_STUB % {"dom": json.dumps(dom)}) + block
        + f"process.stdout.write(JSON.stringify(readRow({json.dumps(label)})));"
    )


# --- what the tile shows ----------------------------------------------------


def test_the_weekly_figure_is_this_week_not_fable():
    payload = _extract()

    assert "__retry_after_ms" not in payload, payload.get("__retry_reason")
    assert payload["weekly_all"]["percent"] == 73
    assert payload["weekly_all"]["kind"] == "used"
    assert payload["session"]["percent"] == 20


def test_fable_is_read_as_its_own_meter():
    payload = _extract()

    fable = payload["rows"]["fable_weekly"]
    assert fable["percent"] == 14
    assert fable["kind"] == "used"
    assert fable["reset_text"] == "Saturday 10:00 AM"


def test_the_snapshot_shows_session_and_weekly_with_fable_in_the_breakdown():
    snapshot = _build_snapshot(_extract(), catalog=bundled_catalog("claude"))

    assert snapshot.status.name == "OK", snapshot.error
    shown = {m.label: (m.percent_used, m.tag) for m in snapshot.metrics}
    assert shown == {
        "Session": (20, None),
        "Weekly": (73, None),
        "Fable": (14, BREAKDOWN_TAG),
    }
    by_label = {m.label: m for m in snapshot.metrics}
    # Both reset wordings the panel now uses are understood.
    assert by_label["Session"].resets_at is not None  # "at 11:00 PM"
    assert by_label["Weekly"].resets_at is not None  # "Saturday 10:00 AM"
    assert by_label["Weekly"].resets_at.weekday() == 5
    assert by_label["Fable"].resets_at == by_label["Weekly"].resets_at


# --- the guard: a row belongs to the meter it starts with -------------------


def test_weekly_no_longer_finds_the_word_in_the_fable_rows_prose():
    """The fallback that produced 14%: "Weekly" matched "Separate weekly
    limit for Fable". The Fable row starts with Fable's label, so it is
    Fable's row. What is left holding the word is the element around all
    three rows, which cannot say whose number is whose - an error the user
    sees, never Fable's 14%."""
    row = _read_row("Weekly")

    assert row["ambiguous"] is True
    assert row["percent"] is None


def test_this_week_is_not_taken_from_fable_this_week():
    """"This week" is inside "Fable this week". The real row is smaller and
    wins anyway; without it, the Fable row must still not be taken."""
    without_this_week = [n for i, n in enumerate(PANEL) if i not in (11, 12, 13, 14)]
    # Re-point parents past the four removed nodes.
    remap = {old: new for new, old in enumerate(
        i for i in range(len(PANEL)) if i not in (11, 12, 13, 14))}
    dom = [(t, h, None if p is None else remap[p]) for t, h, p in without_this_week]

    assert _read_row("This week")["percent"] == 73
    row = _read_row("This week", dom)
    assert row["ambiguous"] is True
    assert row["percent"] is None


def test_a_row_led_by_another_alias_of_the_same_meter_still_reads():
    """A "Weekly" heading over the "This week" row names the same meter."""
    dom = [
        ("", 900, None),
        ("", 400, 0),
        ("Weekly This week Resets Saturday 10:00 AM 73% used", 40, 1),
    ]

    assert _read_row("This week", dom)["percent"] == 73


def test_the_leading_label_must_be_a_whole_word():
    dom = [
        ("", 900, None),
        ("", 400, 0),
        ("Fable this weekend weekly offer 30% used", 40, 1),
    ]

    # "Fable this weekend" is not "Fable this week": the row is not Fable's,
    # so the guard does not take it from the meter being read.
    assert _read_row("Weekly", dom)["percent"] == 30


def test_a_label_that_extends_another_meters_alias_keeps_its_own_row():
    """"Weekly" is another meter's alias and the start of "Weekly extra": a
    row starting "Weekly extra" is still Weekly extra's own."""
    catalog = MeterCatalog(
        kind="claude",
        specs=(
            *bundled_catalog("claude").specs,
            MeterSpec(key="weekly_extra", label="Weekly extra",
                      aliases=("Weekly extra",), window=None, primary=False),
        ),
    )
    dom = [
        ("", 900, None),
        ("", 400, 0),
        ("Weekly extra Resets Saturday 5% used", 40, 1),
    ]

    assert _read_row("Weekly extra", dom, catalog)["percent"] == 5


def test_an_icon_before_the_label_does_not_hide_whose_row_it_is():
    dom = [
        ("", 900, None),
        ("", 400, 0),
        ("● Fable this week Separate weekly limit for Fable 14% used", 40, 1),
    ]

    assert _read_row("Weekly", dom) is None


def test_an_element_holding_several_meters_is_still_refused_as_ambiguous():
    """The guard covers single rows only. A container holding several meters
    reaches readRowText, which says it cannot attribute the number - an error
    the user sees - instead of the row going quietly missing."""
    dom = [
        ("", 900, None),
        ("Current session 20% used This week 73% used Fable this week 14% used", 60, 0),
    ]

    row = _read_row("This week", dom)

    assert row["ambiguous"] is True
    assert row["percent"] is None


# --- the primary rows follow the catalog ------------------------------------


def _relabelled_weekly(alias: str) -> MeterCatalog:
    return MeterCatalog(
        kind="claude",
        specs=tuple(
            spec if spec.key != "weekly_all"
            else MeterSpec(
                key="weekly_all",
                label="Weekly",
                aliases=(*spec.aliases, alias),
                window=spec.window,
                primary=True,
            )
            for spec in bundled_catalog("claude").specs
        ),
    )


def test_a_relabelled_weekly_meter_is_read_once_its_alias_is_in_the_catalog():
    """Before, the seven-day row was read through a hard-coded pair, so an
    alias added to the catalog filled `rows` but never `weekly_all`, the
    field the readiness check and the snapshot read first."""
    dom = [(t.replace("This week", "Seven days"), h, p) for t, h, p in PANEL]
    js = extractor_source(EXTRACTOR_TEMPLATE, _relabelled_weekly("Seven days"),
                          discover=False)

    before = _extract(dom)
    after = _extract(dom, js)

    # Without the alias the only "this week" left is inside "Fable this
    # week", in the element around all three rows: unreadable, not 14.
    assert before["weekly_all"]["percent"] is None
    assert before["weekly_all"]["ambiguous"] is True
    # With it, the clean read wins over that ambiguous one, although the
    # catalog lists the new alias last.
    assert after["weekly_all"]["percent"] == 73
    assert after["weekly_all"]["ambiguous"] is False


def test_the_shipped_order_tries_all_models_first():
    """Both older layouts still read as before: "All models" where it labels
    a row, and "Weekly" where that is the row's own label."""
    old = [
        ("", 900, None),
        ("Plan usage limits", 600, 0),
        ("Current session Resets in 2 hr 59 min 64% used", 40, 1),
        ("All models Resets in 6 hr 29 min 30% used", 40, 1),
    ]
    gauge = [
        ("", 900, None),
        ("Plan usage", 600, 0),
        ("Current session 8% used", 40, 1),
        ("Weekly 12% used", 40, 1),
        ("Opus only 91% used", 40, 1),
    ]

    assert _extract(old)["weekly_all"]["percent"] == 30
    assert _extract(gauge)["weekly_all"]["percent"] == 12


def test_the_catalog_names_this_week_before_weekly():
    """Order matters: "Weekly" is a word the page also uses in prose."""
    aliases = next(
        spec.aliases for spec in bundled_catalog("claude").specs
        if spec.key == "weekly_all"
    )

    assert aliases.index("This week") < aliases.index("Weekly")
