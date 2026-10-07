"""Behavioral test for how the Claude extractor reads page text.

Executes the real expression from EXTRACTOR_JS in node against a stubbed DOM
where innerText and textContent deliberately differ, the way a real page with
an inline <style> block does. A substring assertion ("does the source say
innerText?") would be satisfied by the word appearing in a comment - which is
exactly the failure mode this repo has already shipped three times.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from aigauge.providers.catalog import bundled_catalog
from aigauge.providers.claude import EXTRACTOR_JS

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)

# The real page: Claude inlines <style> in the body, so textContent is the CSS
# source concatenated with the visible text, while innerText is just the text.
_CSS = (
    "#static-composer[hidden]{display:none}#static-composer{position:absolute;"
    "z-index:10;left:0;right:0;width:100%}"
)
_VISIBLE = "Plan usage limits Current session All models"


def _body_text_expression() -> str:
    """Pull the bodyText assignment out of the production source."""
    match = re.search(
        r"const bodyText = (\(\(document\.body.*?)\;", EXTRACTOR_JS, re.S
    )
    assert match, "bodyText assignment not found; did EXTRACTOR_JS change shape?"
    return match.group(1)


def _eval_body_text(*, inner: str, text: str) -> str:
    script = f"""
    globalThis.document = {{ body: {{ innerText: {json.dumps(inner)},
                                      textContent: {json.dumps(text)} }} }};
    process.stdout.write(String({_body_text_expression()}));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_body_text_excludes_inline_stylesheet_source():
    body_text = _eval_body_text(inner=_VISIBLE, text=_CSS + " " + _VISIBLE)

    assert "Plan usage limits" in body_text
    assert "static-composer" not in body_text, "CSS leaked into the page text"
    assert "z-index" not in body_text


def test_body_text_has_no_percent_when_the_page_shows_none():
    """The load-bearing consequence.

    Two idle checks require the ABSENCE of a percent sign. CSS is full of
    "width:100%", so reading textContent made both unreachable on any page
    with an inline <style>.
    """
    body_text = _eval_body_text(inner=_VISIBLE, text=_CSS + " " + _VISIBLE)

    assert "%" not in body_text


def test_body_text_falls_back_to_text_content_when_inner_text_is_absent():
    # Not every environment populates innerText (detached documents, some
    # headless paths); the fallback must still yield the text.
    body_text = _eval_body_text(inner="", text=_VISIBLE)

    assert "Plan usage limits" in body_text


def _panel_signal_expression() -> str:
    match = re.search(r"const usagePanelSignals = (/.+?/i)\.test", EXTRACTOR_JS)
    assert match, "usagePanelSignals not found; did EXTRACTOR_JS change shape?"
    return match.group(1)


@pytest.mark.parametrize(
    "heading",
    [
        # Older layout.
        "Plan usage limits Current session All models",
        # Newer gauge/bar layout: heading is "Plan usage" and the seven-day
        # meter is labelled "Weekly", per Claude's own meter table.
        "Plan usage Current session Weekly Opus only Sonnet only",
        # The heading ALONE must be enough. Including "Current session" above
        # let a regex still pinned to "Plan usage limits" pass on that token,
        # so the heading change was not actually being tested.
        "Plan usage Weekly Opus only Sonnet only Claude Design",
    ],
)
def test_both_claude_usage_layouts_are_recognised_as_the_usage_panel(heading):
    script = f"""
    const re = {_panel_signal_expression()};
    process.stdout.write(String(re.test({json.dumps(heading)})));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == "true", f"layout not recognised: {heading!r}"


def _guard_source() -> str:
    """ALIAS_OWNER, ledBy and the two ledBy* checks, anchored on code."""
    start = EXTRACTOR_JS.index("const ALIAS_OWNER")
    end = EXTRACTOR_JS.index("function findRowByLabel")
    return EXTRACTOR_JS[start:end]


def _weekly_row_source() -> str:
    """readPrimary and the two primary reads, anchored on code."""
    start = EXTRACTOR_JS.index("function readPrimary")
    end = EXTRACTOR_JS.index("const rows = readCatalogRows")
    block = EXTRACTOR_JS[start:end]
    assert "const weeklyAll = readPrimary(" in block, "weeklyAll not read by readPrimary"
    return block


def _weekly_row(available, *, catalog=None, ambiguous=(), unknown=(), unled=()) -> str | None:
    """The real read, against a stubbed readRow that finds ``available``."""
    if catalog is None:
        catalog = bundled_catalog("claude").to_js()
    script = f"""
    const CATALOG = {json.dumps(catalog)};
    const ROW_LABELS = CATALOG.flatMap(m => m.aliases);
    const available = new Set({json.dumps(list(available))});
    const unclear = new Set({json.dumps(list(ambiguous))});
    const unworded = new Set({json.dumps(list(unknown))});
    const mentioned = new Set({json.dumps(list(unled))});
    const readRow = label => available.has(label)
      ? {{ label: label, ambiguous: unclear.has(label),
           kind: unworded.has(label) ? 'unknown' : 'used',
           raw: (mentioned.has(label) ? "you've used 40% more " : '') + label + ' 12% used' }}
      : null;
    // The guard helpers readPrimary calls, from the same source.
    {_guard_source()}
    {_weekly_row_source()}
    process.stdout.write(JSON.stringify(weeklyAll && weeklyAll.label));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    "available,expected",
    [
        # Legacy layout: only "All models" exists.
        (["All models"], "All models"),
        # Gauge/bar layout: only "Weekly" exists - the fallback must fire.
        (["Weekly"], "Weekly"),
        # October 2026: "This week", with "weekly" in the Fable row's prose.
        (["This week"], "This week"),
        (["This week", "Weekly"], "This week"),
        # Both older labels present: the legacy one wins, as it always has.
        (["All models", "Weekly"], "All models"),
    ],
)
def test_weekly_row_resolves_across_every_layout(available, expected):
    """Executes the real read against a stubbed readRow.

    The previous version of this test asserted the source string
    "readRow('All models') || readRow('Weekly')" appeared in EXTRACTOR_JS.
    That passes if the line survives only in a comment and fails on
    reformatting - it tests the text, not the behaviour. I wrote it in the
    same session in which I flagged that exact pattern five times.
    """
    assert _weekly_row(available) == expected


def test_a_clean_read_wins_over_an_ambiguous_one_whatever_the_order():
    """"this week" sits inside "Fable this week", so an element holding both
    rows reads ambiguous for it; a later alias that reads cleanly wins."""
    assert _weekly_row(["This week", "Weekly"], ambiguous=["This week"]) == "Weekly"
    # Nothing clean: the ambiguous read is kept, so the user is told.
    assert _weekly_row(["This week"], ambiguous=["This week"]) == "This week"


def test_a_read_with_no_used_or_left_wording_gives_way_to_one_with_it():
    """"Save 20% this week" carries "this week" and a percentage but no
    wording, so it cannot be a gauge; a later alias's row can."""
    assert _weekly_row(["This week", "Weekly"], unknown=["This week"]) == "Weekly"
    assert _weekly_row(["This week"], unknown=["This week"]) == "This week"


def test_a_row_that_only_mentions_the_label_cannot_overrule_an_earlier_refusal():
    """"you've used 40% more this week" reads cleanly for "This week", but it
    is prose, not the row: the earlier ambiguous read stands, and the user
    is told Weekly could not be read instead of being shown 40."""
    assert _weekly_row(
        ["All models", "This week"], ambiguous=["All models"], unled=["This week"]
    ) == "All models"
    # Nor can a read with no wording: the first read is what is reported.
    assert _weekly_row(
        ["All models", "This week"], ambiguous=["All models"], unknown=["This week"]
    ) == "All models"
    # A row the meter names still overrules it.
    assert _weekly_row(["All models", "This week"], ambiguous=["All models"]) == "This week"


def test_the_shipped_labels_are_still_tried_after_an_overrides_own():
    """An override's aliases replace the catalog entry's, but the shipped
    labels were always read for Session and Weekly and still are."""
    catalog = [
        dict(m, aliases=["Weekly limit"]) if m["key"] == "weekly_all" else m
        for m in bundled_catalog("claude").to_js()
    ]

    assert _weekly_row(["All models"], catalog=catalog) == "All models"
    assert _weekly_row(["Weekly limit", "All models"], catalog=catalog) == "Weekly limit"


def test_a_switched_off_weekly_meter_still_falls_back_to_the_shipped_labels():
    catalog = [m for m in bundled_catalog("claude").to_js() if m["key"] != "weekly_all"]

    assert _weekly_row(["This week"], catalog=catalog) == "This week"
