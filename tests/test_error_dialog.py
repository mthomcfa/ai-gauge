from aigauge.error_dialog import _format_diagnostics
from aigauge.models import SnapshotStatus, UsageSnapshot


def test_format_diagnostics_redacts_email():
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="boom",
        raw={"body_text": "signed in as person@example.com on the usage page"},
    )
    out = _format_diagnostics("claude", snapshot)
    assert "person@example.com" not in out
    assert "[redacted-email]" in out


def test_format_diagnostics_truncates_long_body_text():
    long_body = "person@example.com " + ("x" * 5000)
    snapshot = UsageSnapshot(
        provider="codex",
        status=SnapshotStatus.ERROR,
        error="boom",
        raw={"body_text": long_body},
    )
    out = _format_diagnostics("codex", snapshot)
    assert "[truncated]" in out
    assert "person@example.com" not in out
    # The huge page dump must not be copied wholesale into the clipboard.
    assert out.count("x") < 1000


def test_format_diagnostics_caps_page_supplied_strings_other_than_body_text():
    """The cap must not be an allowlist of one field name.

    Error snapshots now carry page- and Chromium-supplied strings whose length
    nothing on this side controls: document.title, Chromium's load_error_string
    and each row's raw text. Truncating only the key literally named body_text
    stopped covering the payload the moment those fields were added.
    """
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="page failed to load",
        raw={
            "load_failed": True,
            "title": "t" * 20000,
            "load_error_string": "e" * 20000,
            "session": {"raw": "r" * 20000},
        },
    )
    out = _format_diagnostics("claude", snapshot)

    assert out.count("t") < 3000, "document.title reached the clipboard uncapped"
    assert out.count("e") < 3000, "Chromium's error string reached the clipboard uncapped"
    assert out.count("r") < 3000, "a nested row's raw text reached the clipboard uncapped"


def test_format_diagnostics_still_caps_body_text_harder_than_other_strings():
    # body_text is the largest and most identifying field; its tighter limit
    # must not be lost to the general cap.
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="boom",
        raw={"body_text": "b" * 20000},
    )
    assert _format_diagnostics("claude", snapshot).count("b") < 700


def test_format_diagnostics_includes_the_app_version():
    # Fork and upstream ship overlapping release numbers, so a pasted
    # diagnostics blob is ambiguous without the full version string. The
    # README and the version-scheme docs both promise this field is here.
    import json

    from aigauge import __version__

    snapshot = UsageSnapshot(provider="claude", status=SnapshotStatus.ERROR, error="boom")
    payload = json.loads(_format_diagnostics("claude", snapshot))
    assert payload["app_version"] == __version__
    assert "+cfa." in payload["app_version"]


def test_diagnostics_are_bounded_against_a_page_controlled_payload():
    """Breadth, not just per-string length.

    Everything in snapshot.raw comes from a provider page. Measured before this
    cap existed: a 50,000-key payload produced a 1.3 MB clipboard carrying
    attacker-chosen text into a bug report.
    """
    hostile = {"/evil": {"planted": "ATTACKER-CONTROLLED-STRING"}}
    hostile.update({f"flood{i}": i for i in range(50000)})
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="boom",
        raw={"api": hostile, "rows": list(range(5000))},
    )

    out = _format_diagnostics("claude", snapshot)

    assert len(out) < 50_000, f"clipboard payload was {len(out)} bytes"
    assert "more keys" in out, "truncation must be visible, not silent"
    assert "more items" in out
