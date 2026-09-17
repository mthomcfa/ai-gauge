from datetime import datetime

import pytest

from aigauge.models import (
    MAX_NOTE_CHARS,
    SnapshotStatus,
    UsageMetric,
    UsageSnapshot,
    bounded_note,
)
from aigauge.providers import opencode_go
from aigauge.providers.catalog import bundled_catalog, metric_for_spec


def test_snapshot_defaults():
    snap = UsageSnapshot(provider="x", status=SnapshotStatus.OK)
    assert snap.metrics == []
    assert snap.error is None
    assert snap.raw == {}
    assert isinstance(snap.fetched_at, datetime)


def test_metric_optional_fields():
    m = UsageMetric(label="Session")
    assert m.percent_used is None
    assert m.resets_at is None
    assert m.reset_label is None
    assert m.note is None


def _catalog_note(text: str) -> str | None:
    """A meter the catalog reads off a Claude usage page."""
    spec = bundled_catalog("claude").spec_for_key("session")
    metric = metric_for_spec(
        spec, {"percent": 40.0, "kind": "used", "reset_text": text}
    )
    return metric.note


def _opencode_element_note(text: str) -> str | None:
    """opencode.ai's own reader: one element's text, no `clean_label`."""
    payload = {
        "logged_out": False,
        "url": "https://opencode.ai/usage",
        "title": "Usage",
        "usage": [
            {
                "label": "Weekly Usage",
                "percent": 42.0,
                "reset_text": f"Resets in {text}",
                "raw": "x",
            }
        ],
        "has_usage_text": True,
        "has_percent_text": True,
        "body_text": "",
    }
    return opencode_go._build_snapshot(payload).metrics[0].note  # noqa: SLF001


def _opencode_body_note(text: str) -> str | None:
    """Its body-text fallback: a regex group over the whole page."""
    payload = {
        "logged_out": False,
        "url": "https://opencode.ai/usage",
        "title": "Usage",
        "usage": [],
        "has_usage_text": True,
        "has_percent_text": True,
        "body_text": f"Weekly Usage 42% Resets in {text}",
    }
    return opencode_go._build_snapshot(payload).metrics[0].note  # noqa: SLF001


@pytest.mark.parametrize(
    "build",
    [_catalog_note, _opencode_element_note, _opencode_body_note],
    ids=["the meter catalog", "an opencode.ai element", "opencode.ai's body text"],
)
def test_a_page_derived_note_is_bounded_whichever_provider_built_it(build):
    """The bound belongs to the field, not to the one reader that had it.

    `MAX_NOTE_CHARS` was enforced at `catalog.metric_for_spec` alone, and
    opencode.ai builds its own `UsageMetric` from an element's text and from a
    regex group over the whole page body - 200 010 characters of either
    reached the model and were carried for the life of the tile, re-clipped by
    each of the six tooltips that show a note on every refresh.
    """
    note = build("A" * 200_000)

    assert note is not None, "the builder produced no note to bound"
    assert len(note) <= MAX_NOTE_CHARS


def test_the_note_bound_leaves_a_note_that_is_already_short_alone():
    """The clip is a bound, not a formatter: a real "Resets in ..." comes back
    as itself, and no note stays no note."""
    assert len(bounded_note("x" * 100_000)) == MAX_NOTE_CHARS
    assert bounded_note("Resets in 2 hours") == "Resets in 2 hours"
    assert bounded_note("") == ""
    assert bounded_note(None) is None


def test_a_note_this_app_composed_is_not_clipped_to_a_page_bound():
    """Why the bound is at the readers and not in `UsageMetric`.

    Azure composes several sentences - period, the 8-24 h Cost Management lag,
    gross of credits, the next fetch time - into one note, and the widget
    clips what it shows. A blanket clip on the field would have truncated this
    app's own prose in order to bound a provider's page text.
    """
    composed = "Sentence. " * 60
    assert len(composed) > MAX_NOTE_CHARS
    assert UsageMetric(label="Spend this month", note=composed).note == composed
