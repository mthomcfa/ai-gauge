from datetime import datetime, timedelta

from aigauge.models import SnapshotStatus
from aigauge.providers.codex import (
    CODEX_USAGE_URL,
    _build_snapshot,
    _parse_reset_text,
    _weekly_only_layout_evidence,
)


def test_parse_reset_text_handles_weekday_time():
    parsed = _parse_reset_text("Mon 6:00 PM")

    assert parsed is not None
    assert parsed.weekday() == 0
    assert parsed.hour == 18
    assert parsed.minute == 0
    assert parsed > datetime.now()


def test_parse_reset_text_handles_at_prefix_and_date_at_time():
    time_only = _parse_reset_text("at 4:47 PM")
    dated = _parse_reset_text("May 19, 2026 at 9:36 AM")

    assert time_only is not None
    assert time_only.hour == 16
    assert time_only.minute == 47
    assert dated is not None
    assert dated.month == 5
    assert dated.day == 19
    assert dated.year == 2026
    assert dated.hour == 9
    assert dated.minute == 36


def test_codex_logged_out_payload_is_auth_required():
    snapshot = _build_snapshot(
        {
            "logged_out": True,
            "session": None,
            "weekly": None,
            "title": "Login",
            "body_text": "Sign in to continue",
        }
    )

    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert "Not signed in" in (snapshot.error or "")


def test_codex_empty_shell_with_login_task_titles_is_transient_error():
    snapshot = _build_snapshot(
        {
            "logged_out": True,
            "session": None,
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "Codex cloud tasks Sign in flow debugging",
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "without usage cards" in (snapshot.error or "")


def test_codex_usage_rows_ignore_stale_logged_out_flag():
    snapshot = _build_snapshot(
        {
            "logged_out": True,
            "session": {"percent": 12, "kind": "used", "reset_text": "4 hr 10 min"},
            "weekly": {"percent": 31, "kind": "used", "reset_text": "Mon 6:00 PM"},
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "Codex Tasks Sign in debugging 5 hour usage limit 12% used",
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [metric.label for metric in snapshot.metrics] == ["Session", "Weekly"]


def test_codex_cloudflare_payload_is_auth_required():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Just a moment...",
            "body_text": "Verify you are human Cloudflare",
        }
    )

    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert "security verification" in (snapshot.error or "")


def test_codex_cloudflare_soft_payload_is_auth_required():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Just a moment...",
            "body_text": "Checking if the site connection is secure. Cloudflare",
        }
    )

    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert "security verification" in (snapshot.error or "")


def test_codex_usage_rows_ignore_cloudflare_mentions():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": {"percent": 12, "kind": "used", "reset_text": "4 hr 10 min"},
            "weekly": {"percent": 31, "kind": "used", "reset_text": "Mon 6:00 PM"},
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": (
                "Codex Tasks Cloudflare tunnel debugging 5 hour usage limit 12% used "
                "Weekly usage limit 31% used"
            ),
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [metric.label for metric in snapshot.metrics] == ["Session", "Weekly"]


def test_codex_body_text_fallback_ignores_cloudflare_task_titles():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": (
                "Codex Tasks Just a moment Cloudflare tunnel debugging "
                "Personal usage 5 hour usage limit 88% remaining Resets at 4:47 PM "
                "Weekly usage limit 75% remaining Resets Mon 6:00 PM"
            ),
            "has_usage_text": True,
            "has_percent_text": True,
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [metric.percent_used for metric in snapshot.metrics] == [12.0, 25.0]


def test_codex_signed_in_empty_usage_payload_is_transient_error():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "Codex cloud tasks",
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "without usage cards" in (snapshot.error or "")


def test_codex_partial_usage_rows_are_transient_error():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": {
                "percent": 2,
                "kind": "remaining",
                "reset_text": "Jul 10, 2026 12:14 AM",
            },
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "5 hour usage limit 2% remaining Weekly usage limit",
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "part of the usage cards" in (snapshot.error or "")


def test_codex_active_session_with_idle_weekly_is_transient_error():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": {
                "percent": 99,
                "kind": "remaining",
                "reset_text": "4 hr 59 min",
            },
            "weekly": {
                "percent": 100,
                "kind": "remaining",
                "reset_text": None,
            },
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "5 hour usage limit 99% remaining Weekly usage limit 100% remaining",
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "active session with an idle weekly card" in (snapshot.error or "")

def test_codex_usage_signal_prevents_false_idle_fallback():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "Codex cloud tasks",
            "has_usage_text": True,
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "layout may have changed" in (snapshot.error or "")


def test_codex_generic_usage_text_prevents_false_idle_fallback():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "Codex cloud Usage Settings",
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "layout may have changed" in (snapshot.error or "")


def test_codex_unparsed_usage_payload_still_reports_layout_error():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "5 hour usage limit Weekly usage limit",
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "layout may have changed" in (snapshot.error or "")


def test_codex_metrics_carry_windows():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": {"percent": 10, "kind": "used", "reset_text": "4 hr 30 min"},
            "weekly": {"percent": 20, "kind": "used", "reset_text": "Mon 6:00 PM"},
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "5 hour usage limit 10% Weekly usage limit 20%",
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [metric.window for metric in snapshot.metrics] == [
        timedelta(hours=5),
        timedelta(days=7),
    ]


def test_codex_body_text_fallback_reads_new_visible_cards():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": (
                "Personal usage 5 hour usage limit 99% remaining "
                "Resets at 4:47 PM Weekly usage limit 94% remaining "
                "Resets May 19, 2026 at 9:36 AM"
            ),
            "has_usage_text": True,
            "has_percent_text": True,
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [metric.percent_used for metric in snapshot.metrics] == [1.0, 6.0]
    assert all(metric.resets_at is not None for metric in snapshot.metrics)


def test_codex_accepts_weekly_only_shared_agentic_layout():
    # Codex can temporarily expose only the shared weekly agentic limit with no
    # 5-hour Session card. That must produce a usable snapshot instead of an
    # 'error - stale' partial-render retry loop.
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": (
                "Personal usage Shared agentic usage limit "
                "Weekly usage limit 40% used Resets May 19, 2026 at 9:36 AM"
            ),
            "has_usage_text": True,
            "has_percent_text": True,
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [metric.label for metric in snapshot.metrics] == ["Weekly"]
    assert snapshot.metrics[0].percent_used == 40.0


def test_codex_weekly_only_without_shared_layout_markers_still_retries():
    # A genuinely partial render of the OLD Session+Weekly layout must keep
    # retrying rather than silently reporting a weekly-only snapshot.
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": None,
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "Personal usage Weekly usage limit 40% used",
            "has_usage_text": True,
            "has_percent_text": True,
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR


def test_codex_weekly_only_uses_full_page_marker_not_truncated_body():
    # body_text is truncated to 2000 chars by the extractor and the analytics
    # panel can sit past that cut. The layout decision must come from the
    # full-page boolean, or a valid page is rejected forever as 'stale'.
    filler = "Task list item. " * 200  # pushes markers past the 2000-char cut
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": {"percent": 40.0, "kind": "used", "reset_text": "Mon 9:00 AM"},
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": (filler + " Weekly usage limit 40% used")[:2000],
            "has_shared_agentic_text": True,
            "has_usage_text": True,
            "has_percent_text": True,
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [m.label for m in snapshot.metrics] == ["Weekly"]


def test_codex_weekly_only_idle_zero_percent_is_treated_as_mid_hydration():
    # A lone Weekly card at 0% with an idle countdown looks identical to a
    # half-rendered page. Accepting it would tell the user their whole weekly
    # quota is untouched, so it must keep retrying instead.
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly": {"percent": 0.0, "kind": "used", "reset_text": None},
            "title": "Codex",
            "url": CODEX_USAGE_URL,
            "body_text": "Usage breakdown Weekly usage limit 0% used",
            "has_usage_summary_text": True,
            "has_usage_text": True,
            "has_percent_text": True,
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR


# Payload captured verbatim from a real user's log (0.6.5+cfa.1, 2026-08-06).
# The account's weekly quota was genuinely untouched - "100% remaining" - and
# the 0%/idle guard rejected it as a mid-hydration render, so the tile errored
# on every refresh forever.
_IDLE_WEEKLY_PAYLOAD = {
    "body_text": (
        "Codex and Work Analytics 7D 1M Custom Group by: Day API reference "
        "Personal usage Code review Workspace usage Leaderboard Balance "
        "Codex and Work share the same usage limit. Weekly usage limit "
        "100% remaining Workspace monthly credit limit 100% remaining "
        "Resets Aug 31, 2026 8:00 PM 0 of 2,500 credits used"
    ),
    "has_percent_text": True,
    # False because the shipped JS only matched "shared agentic usage limit".
    "has_shared_agentic_text": False,
    "has_usage_summary_text": True,
    "has_usage_text": True,
    "logged_out": False,
    "session": None,
    "title": "Codex",
    "url": "https://chatgpt.com/codex/cloud/settings/analytics?aigauge_ts=1786031234#usage",
    "weekly": {
        "kind": "remaining",
        "percent": 100,
        "raw": "Weekly usage limit 100% remaining",
        "reset_text": None,
    },
}


def test_idle_weekly_only_account_is_not_reported_as_a_partial_render():
    snap = _build_snapshot(dict(_IDLE_WEEKLY_PAYLOAD))
    assert snap.status is SnapshotStatus.OK, snap.error
    weekly = {m.label.lower(): m for m in snap.metrics}["weekly"]
    assert weekly.percent_used == 0


def test_current_page_wording_is_recognised_as_strong_layout_evidence():
    # The page says "share the same usage limit", not "shared agentic usage
    # limit". Missing the current wording is what forced the weak-evidence path.
    assert _weekly_only_layout_evidence(dict(_IDLE_WEEKLY_PAYLOAD)) == "strong"


def test_weak_evidence_still_retries_an_idle_lone_weekly_card():
    # Only generic settings vocabulary: a half-rendered old two-card layout
    # looks like this, so keep retrying rather than reporting 0% used.
    payload = dict(_IDLE_WEEKLY_PAYLOAD)
    payload["body_text"] = "Codex Analytics Personal usage credits remaining"
    payload["has_usage_summary_text"] = True
    assert _weekly_only_layout_evidence(payload) == "weak"
    snap = _build_snapshot(payload)
    assert snap.status is SnapshotStatus.ERROR


def test_strong_evidence_with_real_usage_still_reports_it():
    payload = dict(_IDLE_WEEKLY_PAYLOAD)
    payload["weekly"] = {
        "kind": "remaining",
        "percent": 40,
        "raw": "Weekly usage limit 40% remaining",
        "reset_text": None,
    }
    snap = _build_snapshot(payload)
    assert snap.status is SnapshotStatus.OK
    weekly = {m.label.lower(): m for m in snap.metrics}["weekly"]
    assert weekly.percent_used == 60


def test_extractor_js_matches_every_known_shared_limit_phrasing():
    import re as _re
    for phrase in (
        "shared agentic usage limit",
        "Codex and Work share the same usage limit",
        "Workspace monthly credit limit",
    ):
        assert _re.search(
            r"shared agentic usage limit|shares? the same usage limit"
            r"|workspace monthly credit limit",
            phrase,
            _re.I,
        ), phrase
