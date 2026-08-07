from aigauge.models import SnapshotStatus
from aigauge.providers.claude import CLAUDE_USAGE_URL, _build_snapshot


def test_claude_cloudflare_payload_is_auth_required():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly_all": None,
            "title": "Just a moment...",
            "body_text": "Verify you are human Cloudflare",
        }
    )

    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert "security verification" in (snapshot.error or "")


def test_claude_usage_rows_ignore_cloudflare_chat_titles():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": {"percent": 5, "kind": "used", "reset_text": "6 min"},
            "weekly_all": {"percent": 26, "kind": "used", "reset_text": "Thu 9:59 AM"},
            "title": "Claude",
            "url": CLAUDE_USAGE_URL,
            "body_text": (
                "New chat Search Chats Projects Recents "
                "Cloudflare push model for local GitHub repos "
                "Plan usage limits Current session 5% used All models 26% used"
            ),
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [metric.label for metric in snapshot.metrics] == ["Session", "Weekly"]


def test_claude_usage_rows_ignore_connectivity_chat_titles():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": {"percent": 5, "kind": "used", "reset_text": "6 min"},
            "weekly_all": {"percent": 26, "kind": "used", "reset_text": "Thu 9:59 AM"},
            "title": "Claude",
            "url": CLAUDE_USAGE_URL,
            "body_text": (
                "New chat Search Chats Projects Recents "
                "Can't reach Claude, check your connection and try again "
                "Plan usage limits Current session 5% used All models 26% used"
            ),
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [metric.label for metric in snapshot.metrics] == ["Session", "Weekly"]


def test_claude_idle_usage_ignores_cloudflare_chat_titles():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly_all": None,
            "title": "Claude",
            "url": CLAUDE_USAGE_URL,
            "body_text": (
                "New chat Search Chats Projects Recents "
                "Just a moment debugging Cloudflare "
                "Plan usage limits Current session Resets when you next use this limit "
                "All models Resets when you next use this limit"
            ),
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [(metric.label, metric.percent_used) for metric in snapshot.metrics] == [
        ("Session", 0.0),
        ("Weekly", 0.0),
    ]


def test_claude_logout_payload_is_auth_required():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly_all": None,
            "title": "Claude",
            "url": "https://claude.ai/logout",
            "body_text": "Loading...",
        }
    )

    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert "Not signed in" in (snapshot.error or "")


def test_claude_signed_in_empty_usage_payload_is_idle_zero():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly_all": None,
            "title": "Claude",
            "url": CLAUDE_USAGE_URL,
            "body_text": (
                "New chat Search Chats Projects Recents Plan usage limits "
                "Current session Resets when you next use this limit "
                "All models Resets when you next use this limit"
            ),
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [
        (metric.label, metric.percent_used, metric.reset_label)
        for metric in snapshot.metrics
    ] == [
        ("Session", 0.0, "idle"),
        ("Weekly", 0.0, "idle"),
    ]
    assert all(metric.window is None for metric in snapshot.metrics)


def test_claude_legacy_usage_url_can_still_be_idle_zero():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly_all": None,
            "title": "Claude",
            "url": "https://claude.ai/settings/usage",
            "body_text": (
                "Plan usage limits Current session Resets when you next use this limit "
                "All models Resets when you next use this limit"
            ),
        }
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [(metric.label, metric.percent_used) for metric in snapshot.metrics] == [
        ("Session", 0.0),
        ("Weekly", 0.0),
    ]


def test_claude_partial_render_payload_is_layout_error():
    # Sidebar-only body (main usage pane hasn't populated yet) must NOT be
    # classified as idle — it should surface as an error so the provider
    # retries instead of showing a confident 0/0.
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly_all": None,
            "title": "Claude",
            "url": CLAUDE_USAGE_URL,
            "body_text": "New chat Search Chats Projects Recents",
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "layout may have changed" in (snapshot.error or "")


def test_claude_unparsed_usage_payload_still_reports_layout_error():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly_all": None,
            "title": "Claude",
            "url": CLAUDE_USAGE_URL,
            "body_text": "Plan usage limits Current session 15% used",
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "layout may have changed" in (snapshot.error or "")


def test_claude_cant_reach_page_is_load_failure():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": None,
            "weekly_all": None,
            "title": "Claude",
            "url": CLAUDE_USAGE_URL,
            "body_text": "Can't reach Claude Check your connection. Try again",
        }
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "load failed" in (snapshot.error or "")


def test_claude_zero_weekly_usage_keeps_weekday_reset():
    snapshot = _build_snapshot(
        {
            "logged_out": False,
            "session": {"percent": 2, "kind": "used", "reset_text": "4 hr 58 min"},
            "weekly_all": {"percent": 0, "kind": "used", "reset_text": "Mon 6:00 PM"},
            "title": "Claude",
            "url": CLAUDE_USAGE_URL,
            "body_text": "Plan usage limits Current session 2% All models 0%",
        }
    )

    weekly = next(metric for metric in snapshot.metrics if metric.label == "Weekly")
    assert weekly.percent_used == 0
    assert weekly.resets_at is not None
    assert weekly.reset_label is None


# --- refusing rows that cannot be justified --------------------------------
#
# Two silent failure modes, both of which produced a plausible wrong number
# rather than an error. For a quota monitor that is the worst possible output:
# a user at 90% consumption shown as 10% has no reason to distrust it.


def _payload(**rows):
    base = {
        "logged_out": False,
        "session": {"percent": 5, "kind": "used", "reset_text": "6 min", "ambiguous": False},
        "weekly_all": {"percent": 26, "kind": "used", "reset_text": "Thu 9:59 AM", "ambiguous": False},
        "title": "Claude",
        "url": CLAUDE_USAGE_URL,
        "body_text": "Plan usage Current session 5% used Weekly 26% used",
    }
    base.update(rows)
    return base


def test_an_ambiguous_row_errors_instead_of_reporting_another_meters_number():
    snapshot = _build_snapshot(
        _payload(
            weekly_all={
                "percent": None,
                "kind": "unknown",
                "ambiguous": True,
                "raw": "Weekly 12% used Opus only 91% used Sonnet only 44% used",
                "reset_text": None,
            }
        )
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "Weekly" in (snapshot.error or "")
    assert "several meters" in (snapshot.error or "")
    # The row text must survive to the snapshot or the report is unactionable.
    assert "Sonnet only" in snapshot.raw["weekly_all"]["raw"]


def test_a_percentage_with_no_polarity_wording_errors_rather_than_guessing():
    """normalize_percent resolves an unknown kind to *used*.

    So a row meaning "42% left" was shown as 42% consumed, silently inverted.
    """
    snapshot = _build_snapshot(
        _payload(
            weekly_all={
                "percent": 42,
                "kind": "unknown",
                "ambiguous": False,
                "raw": "Weekly Resets in 3 days 42%",
                "reset_text": "3 days",
            }
        )
    )

    assert snapshot.status == SnapshotStatus.ERROR
    assert "used/remaining" in (snapshot.error or "")
    assert not snapshot.metrics, "an unjustified number reached the gauge"


def test_one_unreadable_row_is_not_hidden_behind_the_other_reading_fine():
    # Session reads cleanly here. Showing it alone would present a partial
    # picture as though it were the whole one.
    snapshot = _build_snapshot(
        _payload(weekly_all={"percent": 42, "kind": "unknown", "ambiguous": False})
    )

    assert snapshot.status == SnapshotStatus.ERROR


def test_a_row_carrying_no_percentage_is_absent_not_unreadable():
    # Regression guard: the idle and empty-panel paths depend on a row with no
    # percentage being treated as missing rather than as a layout failure.
    snapshot = _build_snapshot(
        _payload(
            weekly_all={"percent": None, "kind": "unknown", "ambiguous": False,
                        "reset_text": None}
        )
    )

    assert snapshot.status == SnapshotStatus.OK
    assert [m.label for m in snapshot.metrics] == ["Session"]


def test_both_polarities_still_build_normally():
    used = _build_snapshot(
        _payload(weekly_all={"percent": 26, "kind": "used", "ambiguous": False})
    )
    remaining = _build_snapshot(
        _payload(weekly_all={"percent": 74, "kind": "remaining", "ambiguous": False})
    )

    assert used.status == SnapshotStatus.OK
    assert remaining.status == SnapshotStatus.OK
    weekly = {m.label: m for m in remaining.metrics}["Weekly"]
    assert weekly.percent_used == 26, "remaining must still invert to used"


def test_payloads_predating_the_ambiguous_flag_still_build():
    # Cached snapshots and hand-built payloads have no "ambiguous" key.
    snapshot = _build_snapshot(
        _payload(weekly_all={"percent": 26, "kind": "used", "reset_text": "Thu 9:59 AM"})
    )

    assert snapshot.status == SnapshotStatus.OK
