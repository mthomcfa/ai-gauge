"""Behavioural tests for Claude's row reader, executed in node.

``findRowByLabel``/``readRow`` decide which number the gauge shows. Until now
nothing executed them: the other JS tests cover bodyText, the usage-panel
regex, and the weekly-row fallback with a *stubbed* readRow. Both of the
silent failure modes on this branch live in the code none of that reaches.

The two failures being guarded:

1. **Attribution.** readRow takes the last ``%`` in whichever element it
   picks. The newer layout renders sibling meters (Opus only, Sonnet only)
   next to the weekly one, so a container holding several of them reports the
   last meter's number as the weekly figure.
2. **Polarity.** ``normalize_percent(42, "unknown")`` returns 42 and treats it
   as *used*. A row rendering a bare ``42%`` with no "used"/"remaining" wording
   is therefore reported as 42% consumed even when it means 42% left.

Both produce a plausible wrong number rather than an error, which for a quota
monitor is the worst possible output.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from aigauge.providers.claude import EXTRACTOR_JS

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)


def _row_reader_source() -> str:
    """Extract ROW_LABELS + norm + findRowByLabel + readRow from production.

    Anchored on code, not comments: from the ROW_LABELS declaration up to the
    bodyText assignment, which is the first statement after readRow.
    """
    start = EXTRACTOR_JS.index("const ROW_LABELS")
    end = EXTRACTOR_JS.index("const bodyText")
    block = EXTRACTOR_JS[start:end]
    assert "function readRow" in block, "readRow not in the extracted block"
    assert "function findRowByLabel" in block, "findRowByLabel not in the block"
    return block


def _read_row(label: str, elements: list[tuple[str, int]]) -> dict | None:
    """Run the real readRow against a stub DOM.

    ``elements`` is a list of ``(innerText, height)``. Height matters: the
    scorer penalises tall containers, which is one of the ways it avoids
    picking a whole section.
    """
    dom = [{"innerText": text, "height": height} for text, height in elements]
    script = f"""
    const NODES = {json.dumps(dom)}.map(n => ({{
      innerText: n.innerText,
      textContent: n.innerText,
      getBoundingClientRect: () => ({{ height: n.height }}),
    }}));
    globalThis.document = {{ querySelectorAll: () => NODES }};
    {_row_reader_source()}
    process.stdout.write(JSON.stringify(readRow({json.dumps(label)})));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --- baseline: the layouts that already work must keep working -------------


def test_reads_the_old_layout_session_row():
    row = _read_row(
        "Current session",
        [
            ("Plan usage limits Current session Resets in 2 hr 59 min 64% used "
             "All models Resets in 6 hr 29 min 30% used", 300),
            ("Current session Resets in 2 hr 59 min 64% used", 40),
            ("All models Resets in 6 hr 29 min 30% used", 40),
        ],
    )

    assert row["percent"] == 64
    assert row["kind"] == "used"
    assert not row["ambiguous"]


def test_reads_the_old_layout_weekly_row_without_borrowing_the_session_number():
    row = _read_row(
        "All models",
        [
            ("Plan usage limits Current session Resets in 2 hr 59 min 64% used "
             "All models Resets in 6 hr 29 min 30% used", 300),
            ("Current session Resets in 2 hr 59 min 64% used", 40),
            ("All models Resets in 6 hr 29 min 30% used", 40),
        ],
    )

    assert row["percent"] == 30
    assert not row["ambiguous"]


def test_reads_the_new_layout_weekly_row():
    row = _read_row(
        "Weekly",
        [
            ("Plan usage Current session 8% used Weekly 12% used "
             "Opus only 91% used Sonnet only 44% used", 400),
            ("Weekly 12% used", 40),
        ],
    )

    assert row["percent"] == 12
    assert row["kind"] == "used"
    assert not row["ambiguous"]


def test_a_missing_row_is_still_null():
    assert _read_row("Weekly", [("Plan usage Current session 8% used", 40)]) is None


# --- guard 1: attribution --------------------------------------------------


def test_a_container_holding_several_meters_is_refused_not_guessed():
    """The failure this guard exists for.

    When the DOM offers no element that isolates the weekly meter, the reader
    used to fall back to the largest container and return the LAST percentage
    in it - here 44, belonging to "Sonnet only". A user at 12% of their weekly
    limit would have been shown 44%, with nothing to indicate it was wrong.
    """
    row = _read_row(
        "Weekly",
        [
            ("Weekly 12% used Opus only 91% used Sonnet only 44% used", 120),
        ],
    )

    assert row is not None
    assert row["ambiguous"] is True, "a multi-meter container must not be trusted"
    assert row["percent"] is None, "handed back a number it could not attribute"
    # The evidence still travels, so the layout is fixable from one report.
    assert "Sonnet only" in row["raw"]


def test_ambiguity_needs_both_a_rival_label_and_a_rival_number():
    # A row that merely mentions another meter in prose, with a single
    # percentage, is still unambiguous - refusing it would be a false alarm.
    row = _read_row(
        "Weekly",
        [("Weekly limit across Opus only and Sonnet only models 12% used", 40)],
    )

    assert row["percent"] == 12
    assert not row["ambiguous"]


def test_the_scorer_prefers_an_isolated_row_over_an_ambiguous_container():
    # When a clean row exists the guard must not fire at all.
    row = _read_row(
        "Weekly",
        [
            ("Weekly 12% used Opus only 91% used Sonnet only 44% used", 120),
            ("Weekly 12% used", 32),
        ],
    )

    assert row["percent"] == 12
    assert not row["ambiguous"]


# --- guard 2: polarity -----------------------------------------------------


def test_a_bare_percentage_is_reported_as_unknown_polarity():
    """No "used"/"remaining" wording means the direction is a coin flip.

    normalize_percent treats unknown as used, so a gauge reading "42%" that
    actually meant 42% left would show a user at 58% consumption as 42%.
    """
    row = _read_row("Weekly", [("Weekly Resets in 3 days 42%", 40)])

    assert row["percent"] == 42
    assert row["kind"] == "unknown"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Weekly 42% used", "used"),
        ("Weekly 42% remaining", "remaining"),
        # Wording Claude's newer surfaces use for the same two directions.
        ("Weekly 42% left", "remaining"),
        ("Weekly 42% consumed", "used"),
    ],
)
def test_polarity_vocabulary_covers_the_known_phrasings(text, expected):
    assert _read_row("Weekly", [(text, 40)])["kind"] == expected


def test_a_time_countdown_saying_left_does_not_flip_the_polarity():
    """Why polarity is read beside the percentage, not across the row.

    "left" is a quota word in "42% left" and a *clock* word in "2 hr left".
    A row-wide scan finds the clock one, calls the row "remaining", and
    inverts it: 64% used would be displayed as 36%. Widening the vocabulary
    without anchoring it would have introduced this while fixing the other
    direction.
    """
    row = _read_row("Current session", [("Current session 64% used · 2 hr left", 40)])

    assert row["kind"] == "used"
    assert row["percent"] == 64


def test_a_bare_percentage_is_not_rescued_by_a_countdown_elsewhere_in_the_row():
    # Same trap, the other way round: nothing beside the number means unknown,
    # even though "left" appears earlier in the row.
    row = _read_row("Weekly", [("Weekly Resets in 2 hr left 42%", 40)])

    assert row["kind"] == "unknown"


@pytest.mark.parametrize(
    "label,text",
    [
        ("Current session", "Current session 64% of your session limit used"),
        ("Weekly", "Weekly 64% of the weekly allowance consumed"),
    ],
)
def test_polarity_wording_is_reached_through_prose(label, text):
    """Found by running the real extractor in a real browser.

    An earlier version of this rule required the wording to sit immediately
    against the number, separated by punctuation at most. "64% of your session
    limit used" is an ordinary phrasing and the ORIGINAL code read it fine, so
    that rule was a regression: it turned a working gauge into an error.
    """
    assert _read_row(label, [(text, 40)])["kind"] == "used"


@pytest.mark.parametrize(
    "text",
    [
        # The scan must halt at the digit, never reaching the clock's "left".
        "Weekly 42% Resets in 3 days left",
        # ...and at a bare time unit with no digit after the percentage.
        "Weekly 42% Resets next week, hours left",
        # ...and at the word "reset" itself. Found by adversarial review of
        # this rule: these carry neither a digit nor a time unit, so a scan
        # stopping only at those walked into the countdown's "left" and
        # reported a bare percentage as 58% used.
        "Weekly 42% Resets tomorrow, some left",
        "Weekly 42% until reset, plenty left",
        "Weekly 42% renews soon, a little left",
    ],
)
def test_the_forward_scan_stops_before_a_countdown(text):
    """The reason the scan is bounded rather than unbounded.

    Reading forward to the end of the row would find "left" in "3 days left"
    and report 58% used for a row that never stated a direction. Stopping at
    the first digit or time unit keeps prose reachable and clocks out of
    reach - the two requirements pull in opposite directions and this is the
    line between them.
    """
    assert _read_row("Weekly", [(text, 40)])["kind"] == "unknown"
