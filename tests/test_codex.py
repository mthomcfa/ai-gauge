import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from aigauge.gauge import provider_max_percent
from aigauge.models import SnapshotStatus
from aigauge.providers.catalog import adopt_rows, load_catalog
from aigauge.providers.codex import (
    CODEX_USAGE_URL,
    EXTRACTOR_JS,
    _build_snapshot,
    _parse_body_card,
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


def _shared_limit_regex_literal() -> str:
    """Pull the real regex out of the production JS source."""
    match = re.search(
        r"has_shared_agentic_text:\s*(/.+?/i)\s*\n?\s*\.test", EXTRACTOR_JS, re.S
    )
    assert match, "has_shared_agentic_text not found; did EXTRACTOR_JS change shape?"
    return match.group(1)


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)
@pytest.mark.parametrize(
    "phrase",
    [
        "shared agentic usage limit",
        "Codex and Work share the same usage limit",
        "Workspace monthly credit limit",
    ],
)
def test_extractor_js_matches_every_known_shared_limit_phrasing(phrase):
    """Executes the regex from EXTRACTOR_JS, not a copy of it.

    The previous version of this test declared its own copy of the pattern and
    asserted that copy matched. It passed regardless of what EXTRACTOR_JS
    contained - deleting the production regex outright left it green - while
    its name claimed the opposite. Narrowing the real regex must fail here,
    because a phrasing the extractor misses is precisely how a live account
    ended up erroring forever.
    """
    script = (
        f"const re = {_shared_limit_regex_literal()};\n"
        f"process.stdout.write(String(re.test({json.dumps(phrase)})));"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == "true", f"extractor does not recognise: {phrase!r}"


# --- one field per usage card ----------------------------------------------
#
# Codex's analytics panel can render more cards than the two the app knew
# about. Every catalog meter becomes its own field; only the 5-hour and weekly
# cards stay untagged, because tagged metrics are excluded from the tray colour
# (gauge.provider_max_percent).


def _card(percent, kind="used", reset_text=None):
    return {"percent": percent, "kind": kind, "reset_text": reset_text}


def _cards_payload(**rows):
    payload = {
        "logged_out": False,
        "session": _card(42, reset_text="1:55 PM"),
        "weekly": _card(61, reset_text="Mon 6:00 PM"),
        "rows": {
            "session": _card(42, reset_text="1:55 PM"),
            "weekly": _card(61, reset_text="Mon 6:00 PM"),
        },
        "title": "Codex",
        "url": CODEX_USAGE_URL,
        "has_usage_text": True,
        "has_percent_text": True,
        "body_text": "Personal usage 5 hour usage limit 42% used Weekly usage limit 61% used",
    }
    payload["rows"].update(rows)
    return payload


def test_an_extra_card_becomes_an_informational_field(tmp_path):
    adopt_rows(
        "codex",
        [{"label": "Cloud tasks limit", "percent": 12.0, "kind": "used",
          "reset_text": "Mon 6:00 PM", "in_container": True}],
        base_dir=tmp_path,
    )
    catalog = load_catalog("codex", base_dir=tmp_path)
    payload = _cards_payload(
        cloud_tasks_limit=_card(12, reset_text="Mon 6:00 PM"),
    )

    snapshot = _build_snapshot(payload, catalog=catalog)

    assert snapshot.status == SnapshotStatus.OK
    assert [m.label for m in snapshot.metrics] == [
        "Session",
        "Weekly",
        "Cloud tasks limit",
    ]
    assert [m.label for m in snapshot.metrics if m.tag is None] == [
        "Session",
        "Weekly",
    ]


def test_an_extra_card_is_not_mistaken_for_a_partial_render(tmp_path):
    """The partial-render guard counts primary cards, not every metric.

    It compares the metric labels against {session, weekly}. Counting the
    breakdown rows too would make every page that renders a third card look
    like a half-rendered one and retry forever.
    """
    adopt_rows(
        "codex",
        [{"label": "Cloud tasks limit", "percent": 12.0, "kind": "used",
          "reset_text": "Mon 6:00 PM", "in_container": True}],
        base_dir=tmp_path,
    )
    payload = _cards_payload(cloud_tasks_limit=_card(12, reset_text="Mon 6:00 PM"))

    snapshot = _build_snapshot(
        payload, catalog=load_catalog("codex", base_dir=tmp_path)
    )

    assert snapshot.status == SnapshotStatus.OK
    assert provider_max_percent(snapshot) == 61


def test_a_bare_percentage_is_refused_rather_than_reported_as_used():
    """The defect recorded in docs/next-session.md, now fixed.

    readCard tested the whole card for "used"/"remaining", so a card rendering
    only "42%" resolved to *used*. If it meant 42% left, the gauge pointed the
    wrong way - and a wrong number that looks right is the one failure mode
    this app cannot announce.
    """
    payload = _cards_payload()
    payload["rows"]["weekly"] = _card(42, kind="unknown")
    payload["weekly"] = payload["rows"]["weekly"]

    snapshot = _build_snapshot(payload)

    assert snapshot.status == SnapshotStatus.ERROR
    assert "used/remaining" in (snapshot.error or "")
    assert not snapshot.metrics, "an unjustified number reached the gauge"


def test_a_payload_without_the_rows_block_still_reads_both_cards():
    # Cached snapshots and hand-built payloads predate `rows`.
    payload = _cards_payload()
    payload.pop("rows")

    snapshot = _build_snapshot(payload)

    assert [m.label for m in snapshot.metrics] == ["Session", "Weekly"]


# --- polarity, read beside the percentage ----------------------------------


def _read_card_source() -> str:
    """Extract polarityFor + readCardText from production, not a copy."""
    start = EXTRACTOR_JS.index("function polarityFor")
    end = EXTRACTOR_JS.index("function readCard(")
    block = EXTRACTOR_JS[start:end]
    assert "function readCardText" in block, "readCardText not in the block"
    return block


def _read_card_text(text: str) -> dict:
    script = f"""
    {_read_card_source()}
    process.stdout.write(JSON.stringify(readCardText({json.dumps(text)})));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5 hour usage limit 42% used Resets 1:55 PM", "used"),
        ("5 hour usage limit 42% remaining Resets 1:55 PM", "remaining"),
        ("5 hour usage limit 42% left Resets 1:55 PM", "remaining"),
        ("5 hour usage limit used 42% Resets 1:55 PM", "used"),
        # No wording beside the number: refused, not guessed.
        ("5 hour usage limit 42% Resets 1:55 PM", "unknown"),
        ("5 hour usage limit 42%", "unknown"),
    ],
)
@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)
def test_codex_polarity_is_read_beside_the_percentage(text, expected):
    assert _read_card_text(text)["kind"] == expected


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)
def test_a_countdown_saying_left_does_not_flip_the_polarity():
    """"2 hr left" is a clock, not a quota.

    Scanning the whole card for "left" read it as a direction and inverted the
    gauge; the forward scan stops before any countdown.
    """
    card = _read_card_text("Weekly usage limit 12% used Resets in 2 hr left")

    assert card["kind"] == "used"


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)
def test_wording_from_the_next_card_cannot_set_this_cards_polarity():
    """The text-window fallback runs into the next card.

    "5 hour usage limit 42% Weekly usage limit 61% remaining" used to resolve
    the bare 42% to *remaining* off the neighbour's wording.
    """
    card = _read_card_text(
        "5 hour usage limit 42% Weekly usage limit 61% remaining"
    )

    assert card["percent"] == 42
    assert card["kind"] == "unknown"


def test_the_python_text_fallback_uses_the_same_polarity_rule():
    """_parse_body_card carried the identical defect and gets the identical fix."""
    bare = _parse_body_card(
        "5 hour usage limit 42% Weekly usage limit 61% remaining",
        "5 hour usage limit",
        ("Weekly usage limit",),
    )
    left = _parse_body_card("5 hour usage limit 42% left", "5 hour usage limit")
    countdown = _parse_body_card(
        "Weekly usage limit 12% used Resets in 2 hr left", "Weekly usage limit"
    )

    assert bare["kind"] == "unknown"
    assert left["kind"] == "remaining"
    assert countdown["kind"] == "used"
