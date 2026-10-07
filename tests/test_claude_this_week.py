"""claude.ai's October 2026 usage panel: "This week" and "Fable this week".

From 6 October 2026 the panel's rows read, from Michael's screen:

    Current session    Resets at 11:00 PM                                  20% used
    This week          Resets Saturday 10:00 AM                            73% used
    Fable this week    Separate weekly limit for Fable ·
                       Resets Saturday 10:00 AM                            14% used

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

# (own text, height, parent index[, text after its children]). A node's
# rendered text is its own text, its descendants' and any text after them,
# derived below, so a container cannot claim text its children do not have.
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
  _after: n[3] || '',
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
  return [el._own].concat(el._children.map(derive), [el._after])
    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
// Elements and text nodes under a root, in page order, as a TreeWalker
// showing both yields them.
function walk(el, out) {
  if (el._own) out.push({ nodeType: 3, nodeValue: el._own });
  el._children.forEach(child => { out.push(child); walk(child, out); });
  if (el._after) out.push({ nodeType: 3, nodeValue: el._after });
  return out;
}
NODES.forEach(el => { el.innerText = derive(el); el.textContent = el.innerText; });
// Descendants in page order: every list here gives a parent before its
// children and siblings in order.
NODES.forEach(el => {
  el.querySelectorAll = () => NODES.filter(n => n !== el && el.contains(n));
});
globalThis.document = {
  querySelectorAll: () => NODES,
  querySelector: () => null,
  createTreeWalker: root => {
    const nodes = walk(root, []);
    let i = 0;
    return { nextNode: () => nodes[i++] || null };
  },
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
    """Run ``script`` in node and parse what it prints.

    Over stdin and in UTF-8 both ways, not ``-e`` and ``text=True``: those
    encode the script and decode its output with the locale's encoding,
    and on Windows that is cp1252, which cannot decode the "●" a row
    here reads back - the output came back None."""
    out = subprocess.run(
        ["node", "-"], input=script, capture_output=True, encoding="utf-8", timeout=30
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


def test_a_row_the_catalog_does_not_know_cannot_take_the_weekly_figure():
    """Claude names per-model limits "<model> this week" now. One the catalog
    has not learned yet is no rival, and "Opus this week · Resets in 2 days
    · 9% used" is shorter than the real row: by length alone it was Weekly."""
    dom = PANEL + [
        ("", 70, 6),
        ("Opus this week", 20, len(PANEL)),
        ("Resets in 2 days", 20, len(PANEL)),
        ("9% used", 20, len(PANEL)),
    ]

    snapshot = _build_snapshot(_extract(dom), catalog=bundled_catalog("claude"))

    assert snapshot.status.name == "OK", snapshot.error
    assert {m.label: m.percent_used for m in snapshot.metrics}["Weekly"] == 73


def test_a_promotion_naming_this_week_leaves_the_gauge_layouts_weekly_row_alone():
    """"This week" is now tried before "Weekly". On the gauge layout a line
    like "Save 20% this week" is the only thing it finds: a percentage with no
    used/left wording, which must not stand in for the real "Weekly" row."""
    gauge = [
        ("", 900, None),
        ("Plan usage", 600, 0),
        ("Current session 8% used", 40, 1),
        ("Weekly 12% used", 40, 1),
        ("Opus only 91% used", 40, 1),
        ("Save 20% this week", 40, 0),
    ]

    snapshot = _build_snapshot(_extract(gauge), catalog=bundled_catalog("claude"))

    assert snapshot.status.name == "OK", snapshot.error
    assert {m.label: m.percent_used for m in snapshot.metrics}["Weekly"] == 12


def test_prose_naming_this_week_does_not_turn_a_refusal_into_a_number():
    """Claude collapses rows into one element at times. Main refused the
    collapsed old layout's Weekly; a "this week" in an insight line must not
    turn that refusal into a reading of the insight's 40%."""
    dom = [
        ("", 900, None),
        ("Plan usage limits", 600, 0),
        ("Current session Resets in 2 hr 59 min 64% used", 40, 1),
        ("All models Resets in 6 hr 29 min 30% used Opus only Resets in 6 hr 10% used", 60, 1),
        ("Insight: you've used 40% more this week than last", 40, 1),
    ]

    snapshot = _build_snapshot(_extract(dom), catalog=bundled_catalog("claude"))

    assert snapshot.status.name == "ERROR"
    assert "could not read Weekly" in snapshot.error


def _override_weekly(aliases: tuple[str, ...]) -> str:
    catalog = MeterCatalog(
        kind="claude",
        specs=tuple(
            spec if spec.key != "weekly_all"
            else MeterSpec(key="weekly_all", label="Weekly", aliases=aliases,
                           window=spec.window, primary=True)
            for spec in bundled_catalog("claude").specs
        ),
    )
    return extractor_source(EXTRACTOR_TEMPLATE, catalog, discover=False)


def test_an_override_listing_its_own_labels_still_gets_the_fix():
    """The README's override example, as users copied it before "This week"
    existed, replaces the entry's aliases. The shipped labels are still read
    after it, so the new panel reads 73 and the old one 30."""
    readme_example = _override_weekly(("All models", "Weekly", "Weekly limit"))
    old = [
        ("", 900, None),
        ("Plan usage limits", 600, 0),
        ("Current session Resets in 2 hr 59 min 64% used", 40, 1),
        ("All models Resets in 6 hr 29 min 30% used", 40, 1),
    ]

    assert _extract(PANEL, readme_example)["weekly_all"]["percent"] == 73
    assert _extract(old, _override_weekly(("Weekly limit",)))["weekly_all"]["percent"] == 30


def test_fable_is_still_read_if_the_row_is_shortened_to_its_name():
    dom = [(t.replace("Fable this week", "Fable"), h, p) for t, h, p in PANEL]

    assert _extract(dom)["rows"]["fable_weekly"]["percent"] == 14
    assert _extract(dom)["weekly_all"]["percent"] == 73


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


def test_a_row_named_by_another_alias_of_the_same_meter_is_that_meters():
    """A "Weekly limits" heading in the same element as the "All models" row
    names the row "Weekly" - the same meter - so reading "All models" finds
    it."""
    dom = [
        ("", 900, None),
        ("", 400, 0),
        ("Weekly limits All models Resets in 6 hr 29 min 30% used", 40, 1),
    ]

    assert _read_row("All models", dom)["percent"] == 30


def test_the_leading_label_must_be_a_whole_word():
    """"This weekend's pass" is not named "This week"; were it, it would win
    on length over the real row."""
    dom = [
        ("", 900, None),
        ("", 400, 0),
        ("This weekend's pass 5% used", 40, 1),
        ("This week Resets Saturday 10:00 AM 73% used", 40, 1),
    ]

    assert _read_row("This week", dom)["percent"] == 73


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

    assert _read_row("Fable this week", dom)["percent"] == 14
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


# --- pages the app cannot attribute are refused, never misread ------------
#
# The review's shapes: what claude.ai could plausibly render next. Each one
# showed another meter's number as Weekly, as an OK snapshot, before a row
# had to be named by the meter it was read for.

SESSION = ["Current session", "Resets at 11:00 PM", "20% used"]
THIS_WEEK = ["This week", "Resets Saturday 10:00 AM", "73% used"]
FABLE = ["Fable this week",
         "Separate weekly limit for Fable \u00b7 Resets Saturday 10:00 AM", "14% used"]
MYTHOS = ["Mythos this week",
          "Separate weekly limit for Mythos \u00b7 Resets Saturday 10:00 AM", "40% used"]


def _panel(rows, *, heading=""):
    nodes = [
        ("", 1300, None),
        ("Settings General Account Privacy Billing Usage", 1200, 0),
        ("", 1200, 0),
        ("Your usage Max (20x)", 30, 2),
        (heading, 360, 2),
    ]
    for row in rows:
        index = len(nodes)
        nodes.append(("", 70, 4))
        nodes.extend((part, 20, index) for part in row)
    nodes.append(("Limit resets Full reset Expires Oct 22", 60, 2))
    return nodes


def _first(row, *order):
    return [row[i] for i in order]


@pytest.mark.parametrize(
    "rows,heading",
    [
        # A model the catalog does not know yet, and the real row gone.
        ([SESSION, FABLE, MYTHOS], ""),
        # Percentages first, the real row gone.
        ([_first(SESSION, 2, 0, 1), _first(FABLE, 2, 0, 1)], ""),
        # The description before the label, the real row gone.
        ([SESSION, _first(FABLE, 1, 0, 2)], ""),
        # "This week" as a heading over per-model rows, Fable first.
        ([SESSION,
          ["Fable", "Separate weekly limit for Fable", "14% used"],
          ["All other models", "Resets Saturday 10:00 AM", "73% used"]], "This week"),
    ],
    ids=["unknown-model", "percent-first", "description-first", "heading-over-rows"],
)
def test_a_page_the_app_cannot_attribute_is_an_error_not_another_meters_number(rows, heading):
    snapshot = _build_snapshot(_extract(_panel(rows, heading=heading)),
                               catalog=bundled_catalog("claude"))

    # On main each of these was OK with Weekly 14 - Fable's number.
    assert snapshot.status.name == "ERROR", {m.label: m.percent_used for m in snapshot.metrics}
    assert "could not read" in snapshot.error and "Weekly" in snapshot.error


FABLE_WEEKLY_DESC = ["Fable this week",
                     "Weekly limit for Fable \u00b7 Resets Saturday 10:00 AM", "14% used"]


def test_a_label_at_the_start_of_a_description_does_not_rename_the_row():
    """"Fable this week · Weekly limit for Fable · 14% used" is Fable's: the
    first labelled element names the row, and a description that happens to
    start with another meter's label comes after it."""
    shown = _build_snapshot(_extract(_panel([SESSION, THIS_WEEK, FABLE_WEEKLY_DESC])),
                            catalog=bundled_catalog("claude"))
    renamed = _build_snapshot(
        _extract(_panel([SESSION, ["7-day limit", "Resets Saturday 10:00 AM", "73% used"],
                         FABLE_WEEKLY_DESC])),
        catalog=bundled_catalog("claude"))

    assert {m.label: m.percent_used for m in shown.metrics} == {
        "Session": 20, "Weekly": 73, "Fable": 14,
    }
    assert renamed.status.name == "ERROR"
    assert "Weekly" in renamed.error


def test_a_session_description_naming_the_weekly_limit_leaves_session_alone():
    gauge = [
        ("", 900, None),
        ("Plan usage", 600, 0),
        ("Current session \u00b7 Weekly limits reset Thursday \u00b7 8% used", 40, 1),
        ("Weekly 12% used", 40, 1),
    ]

    payload = _extract(gauge)

    assert payload["session"]["percent"] == 8
    assert payload["weekly_all"]["percent"] == 12


def test_a_digit_straight_after_the_label_still_names_the_row():
    """Inline label and percentage elements run together in innerText:
    "Current session20% used". A digit does not continue the word."""
    dom = [
        ("", 900, None),
        ("", 400, 0),
        ("Current session20% used", 40, 1),
    ]

    assert _read_row("Current session", dom)["percent"] == 20


def test_a_badge_before_the_label_does_not_hide_whose_row_it_is():
    """The row's text starts "New This week ...", but its label element
    starts "This week": the row is named by structure."""
    rows = [SESSION, ["New", *THIS_WEEK], FABLE]

    snapshot = _build_snapshot(_extract(_panel(rows)), catalog=bundled_catalog("claude"))

    assert {m.label: m.percent_used for m in snapshot.metrics} == {
        "Session": 20, "Weekly": 73, "Fable": 14,
    }


def _this_week_label_after(prefix: str):
    """The panel with the This week row's label a bare text node after an
    element holding ``prefix``, as ``<span><span>New</span>This week</span>``
    renders: no element of its own starts with the label."""
    nodes = _panel([SESSION])
    row = len(nodes)
    nodes.append(("", 70, 4))
    wrap = len(nodes)
    nodes.append(("", 20, row, "This week"))
    nodes.append((prefix, 20, wrap))
    nodes.append(("Resets Saturday 10:00 AM", 20, row))
    nodes.append(("73% used", 20, row))
    fable = len(nodes)
    nodes.append(("", 70, 4))
    nodes.extend((part, 20, fable) for part in FABLE)
    return nodes


@pytest.mark.parametrize(
    "prefix", ["New", "Usage limit:", "schedule", "Max"],
    ids=["badge", "screen-reader-text", "ligature-icon", "plan-chip"],
)
def test_a_label_sharing_its_element_with_a_prefix_is_still_found(prefix):
    snapshot = _build_snapshot(_extract(_this_week_label_after(prefix)),
                               catalog=bundled_catalog("claude"))

    assert {m.label: m.percent_used for m in snapshot.metrics} == {
        "Session": 20, "Weekly": 73, "Fable": 14,
    }


def test_a_list_number_before_the_label_does_not_hide_it():
    rows = [SESSION, ["1. This week", "Resets Saturday 10:00 AM", "73% used"], FABLE]

    payload = _extract(_panel(rows))

    assert payload["weekly_all"]["percent"] == 73


def test_rows_that_put_the_percentage_first_still_read():
    rows = [_first(SESSION, 2, 0, 1), _first(THIS_WEEK, 2, 0, 1), _first(FABLE, 2, 0, 1)]

    snapshot = _build_snapshot(_extract(_panel(rows)), catalog=bundled_catalog("claude"))

    assert {m.label: m.percent_used for m in snapshot.metrics} == {
        "Session": 20, "Weekly": 73, "Fable": 14,
    }


def test_an_unknown_models_row_beside_the_real_one_changes_nothing():
    snapshot = _build_snapshot(_extract(_panel([SESSION, THIS_WEEK, FABLE, MYTHOS])),
                               catalog=bundled_catalog("claude"))

    assert snapshot.status.name == "OK", snapshot.error
    assert {m.label: m.percent_used for m in snapshot.metrics} == {
        "Session": 20, "Weekly": 73, "Fable": 14,
    }


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
    """Where both a "This week" row and a "Weekly" row read cleanly, the
    first in catalog order is the gauge: today's label, not the older one."""
    aliases = next(
        spec.aliases for spec in bundled_catalog("claude").specs
        if spec.key == "weekly_all"
    )

    assert aliases.index("This week") < aliases.index("Weekly")
