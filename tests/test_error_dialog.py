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
