"""ChatGPT's Settings > Usage page, where Codex usage lives since October 2026.

On 2026-10-05 the Codex analytics page forwarded to
``chatgpt.com/settings/usage?tab=analytics``, and the tile failed twice per
refresh: once on a page that showed no percentage the reader recognised, once
on "Skip to content Loading settings...". The Overview tab carries the limits:
a "Plan limits" section ("Shared across Codex, Work, ...") with a lone
"Weekly limit" card reading "58% left" and "Resets in 4d 7h", then Credits,
usage-limit resets and a "Daily usage" list of per-surface percentages.

The DOM below is that page as the user's screenshot shows it, run through the
production extractor in node on the same stub as ``test_meter_discovery_js``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from aigauge.models import SnapshotStatus
from aigauge.providers._common import SECURITY_VERIFICATION_STRONG_MARKERS
from aigauge.providers.catalog import bundled_catalog, extractor_source
from aigauge.providers.codex import (
    CODEX_ANALYTICS_URL,
    CODEX_USAGE_PAGE,
    CODEX_USAGE_URL,
    EXTRACTOR_TEMPLATE,
    USAGE_PANEL_WAIT_MS,
    _build_snapshot,
    _is_codex_analytics_url,
    _parse_reset_text,
    _weekly_only_layout_evidence,
)
from aigauge.webview.verify import VERIFY_TARGETS

from tests.test_meter_discovery_js import _DOM_STUB

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)

Dom = list[tuple[str, int, "int | None"]]

PLAN = [
    ("", 100, 2),
    ("Plan limits", 20, 7),
    ("Shared across Codex, Work, Workspace Agents, and ChatGPT for Excel. "
     "Chat conversations are not included.", 20, 7),
    ("", 60, 7),
    ("Weekly limit", 20, 10),
    ("Resets in 4d 7h", 20, 10),
    ("58% left", 20, 10),
]

OVERVIEW: Dom = [
    ("", 900, None),
    ("Skip to content", 10, 0),
    ("", 880, 0),
    ("Usage", 20, 2),
    ("", 20, 2),
    ("Overview", 20, 4),
    ("Analytics", 20, 4),
    *PLAN,
    ("", 150, 2),
    ("Credits", 20, 14),
    ("Shared workspace credits let you keep using Work and Codex beyond "
     "included usage.", 20, 14),
    ("", 40, 14),
    ("2,500 of 2,500 monthly credits left", 20, 17),
    ("Resets in 26d 9h", 20, 17),
    ("Increase monthly limit", 20, 17),
    ("", 40, 14),
    ("2,402 workspace credits", 20, 21),
    ("Add more", 20, 21),
    ("", 40, 14),
    ("Automatic reload", 20, 24),
    ("Up to 40% off", 20, 24),
    ("", 150, 2),
    ("Usage limit resets", 20, 27),
    ("Use a reset to restore your 5-hour limit, weekly limit, or both", 20, 27),
    ("Available 2", 20, 27),
    ("History", 20, 27),
    ("", 40, 27),
    ("Full reset", 20, 32),
    ("Expires October 22", 20, 32),
    ("Use reset", 20, 32),
    ("", 200, 2),
    ("Daily usage", 20, 36),
    ("Usage data is approximate and may be delayed by up to 6 hours", 20, 36),
    ("", 40, 36),
    ("Sep 6, 2026", 20, 39),
    ("Cloud", 20, 39),
    ("0%", 20, 39),
    ("", 40, 36),
    ("Sep 6, 2026", 20, 43),
    ("Web", 20, 43),
    ("1%", 20, 43),
    ("", 40, 36),
    ("Sep 6, 2026", 20, 47),
    ("CLI", 20, 47),
    ("0%", 20, 47),
]


def _without_plan_limits(dom: Dom) -> Dom:
    """The same page before the plan limits have rendered: those nodes kept
    as empty placeholders, so every index the rest of the tree uses holds."""
    plan = set(range(7, 7 + len(PLAN)))
    return [("", h, p) if i in plan else (t, h, p) for i, (t, h, p) in enumerate(dom)]


# A page this old has outlived the extractor's wait for the plan limits.
PAST_THE_WAIT_MS = USAGE_PANEL_WAIT_MS + 1000


def _extract(
    dom: Dom,
    pathname: str = "/settings/usage",
    *,
    page_age_ms: float = 2000,
    title: str = "ChatGPT",
    login_link: bool = False,
) -> dict:
    source = extractor_source(EXTRACTOR_TEMPLATE, bundled_catalog("codex"), discover=True)
    href = "https://chatgpt.com" + pathname + "?tab=overview"
    script = (
        (_DOM_STUB % {"dom": json.dumps(dom)})
        + f"globalThis.location = {{pathname: {json.dumps(pathname)}, "
        f"href: {json.dumps(href)}, hostname: 'chatgpt.com'}};\n"
        + f"document.title = {json.dumps(title)};\n"
        # performance.now() is the page's age: time since its navigation began.
        + "Object.defineProperty(globalThis, 'performance', "
        f"{{value: {{now: () => {float(page_age_ms)}}}, configurable: true}});\n"
        + (
            "document.querySelector = sel => String(sel).includes('/login') ? {} : null;\n"
            if login_link
            else ""
        )
        + "const RESULT = " + source.strip() + "\n"
        + "process.stdout.write(JSON.stringify(RESULT));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --- the extractor on the page --------------------------------------------


@needs_node
def test_the_overview_reads_as_a_weekly_limit_and_nothing_else():
    result = _extract(OVERVIEW)

    assert "__retry_after_ms" not in result, result.get("__retry_reason")
    weekly = result["rows"]["weekly"]
    assert (weekly["percent"], weekly["kind"], weekly["reset_text"]) == (58, "remaining", "4d 7h")
    assert result["session"] is None and "session" not in result["rows"]
    assert result["has_shared_agentic_text"] is True
    assert result["logged_out"] is False

    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert snapshot.status == SnapshotStatus.OK, snapshot.error
    assert [(m.label, m.percent_used) for m in snapshot.metrics] == [("Weekly", 42)]
    expected = datetime.now() + timedelta(days=4, hours=7)
    assert abs((snapshot.metrics[0].resets_at - expected).total_seconds()) < 120


@needs_node
@pytest.mark.parametrize(
    "dom",
    [
        [("", 900, None), ("Skip to content", 10, 0), ("Loading settings…", 10, 0)],
        _without_plan_limits(OVERVIEW),
    ],
    ids=["shell-still-loading", "daily-usage-before-the-plan-limits"],
)
def test_the_page_is_polled_until_the_weekly_limit_is_there(dom):
    """The read this extractor used to take, once, came back empty and was
    reported as a layout change; one of the user's two reads on 2026-10-05 saw
    only "Skip to content Loading settings..."."""
    result = _extract(dom)

    assert result.get("__retry_reason") == "usage panel not ready"
    assert result["__retry_after_ms"] > 0


SIGNED_OUT_LANDING: Dom = [
    ("", 900, None),
    ("Log in", 10, 0),
    ("Sign up for free", 10, 0),
    ("What can I help with? By messaging ChatGPT, you agree to our Terms", 10, 0),
]


@needs_node
def test_a_signed_out_page_left_at_settings_usage_is_reported_once_the_wait_ends():
    """A shell can say "Log in" before it hydrates, so on Settings > Usage
    looking signed out is not a reason to stop waiting. Once the page has had
    its time, it is reported as what it is - and the word "ChatGPT", on every
    chatgpt.com page, must not turn that into "loaded without usage cards"."""
    young = _extract(SIGNED_OUT_LANDING)
    assert young.get("__retry_reason") == "usage panel not ready"

    result = _extract(SIGNED_OUT_LANDING, page_age_ms=PAST_THE_WAIT_MS)
    assert "__retry_after_ms" not in result
    assert result["logged_out"] is True

    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED, snapshot.error
    assert "Not signed in" in (snapshot.error or "")


@needs_node
def test_a_sign_in_page_is_reported_at_once():
    """Signed out and sent to the sign-in page: another address, not polled."""
    result = _extract(SIGNED_OUT_LANDING, pathname="/auth/login", login_link=True)

    assert "__retry_after_ms" not in result
    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED, snapshot.error


@needs_node
def test_a_shell_saying_log_in_is_waited_out_not_reported_as_signed_out():
    """With the early read on, the first look can land on a shell. One with a
    "Log in" link must be polled like "Loading settings...", not ended."""
    shell = [("", 900, None), ("Skip to content", 10, 0), ("Log in", 10, 0)]

    result = _extract(shell, login_link=True)

    assert result.get("__retry_reason") == "usage panel not ready"


@needs_node
@pytest.mark.parametrize("marker", SECURITY_VERIFICATION_STRONG_MARKERS)
def test_a_security_check_is_reported_at_once_not_polled(marker):
    """The user has to click through it; polling only ran the clock down and
    then said "extractor retry limit exceeded". Every marker Python reports
    must also stop the poll, so the JS list is pinned to it here."""
    dom = [("", 900, None), (marker.capitalize(), 10, 0), ("Ray ID: 8c", 10, 0)]

    result = _extract(dom, title="Just a moment...")

    assert "__retry_after_ms" not in result
    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED, snapshot.error
    assert "security verification" in (snapshot.error or "")


@needs_node
def test_a_soft_cloudflare_check_is_reported_at_once_not_polled():
    dom = [("", 900, None), ("Checking your browser. Cloudflare", 10, 0)]

    result = _extract(dom, title="Just a moment...")

    assert "__retry_after_ms" not in result
    assert _build_snapshot(result, catalog=bundled_catalog("codex")).status == (
        SnapshotStatus.AUTH_REQUIRED
    )


@needs_node
@pytest.mark.parametrize(
    "title,login_link",
    [("ChatGPT", True), ("ChatGPT - Login settings", False)],
    ids=["stray-login-link", "login-in-title"],
)
def test_a_readable_weekly_limit_is_a_signed_in_page(title, login_link):
    """One stray login link or "login" in the title made the whole page read
    as signed out: on the old page the top-level card overrode that, and the
    new card only travels in ``rows``."""
    result = _extract(OVERVIEW, title=title, login_link=login_link)

    assert result["logged_out"] is False
    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert snapshot.status == SnapshotStatus.OK, snapshot.error


def test_a_card_read_in_rows_outweighs_a_logged_out_flag():
    """The Python half of the same rule, for a payload that arrives flagged."""
    payload = {
        "url": CODEX_USAGE_URL,
        "logged_out": True,
        "title": "ChatGPT",
        "body_text": "Plan limits Shared across Codex Weekly limit Resets in 4d 7h 58% left",
        "rows": {"weekly": {"percent": 58, "kind": "remaining", "reset_text": "4d 7h"}},
        "has_shared_agentic_text": True,
    }

    snapshot = _build_snapshot(payload, catalog=bundled_catalog("codex"))

    assert snapshot.status == SnapshotStatus.OK, snapshot.error


@needs_node
def test_a_weekly_limit_without_used_or_left_is_reported_once_the_wait_ends():
    """The poll waits for the wording, and then has to say what was missing:
    a bare retry limit told the user nothing, and was never retried."""
    dom = [(t.replace("58% left", "58%"), h, p) for t, h, p in OVERVIEW]

    young = _extract(dom)
    assert young.get("__retry_reason") == "usage panel not ready"
    assert young["rows"]["weekly"]["percent"] == 58, "the retry payload says what was read"

    result = _extract(dom, page_age_ms=PAST_THE_WAIT_MS)
    assert "__retry_after_ms" not in result
    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert snapshot.status == SnapshotStatus.ERROR
    assert "could not read Weekly" in (snapshot.error or "")


@needs_node
def test_a_page_that_never_shows_the_limits_is_reported_once_the_wait_ends():
    shell = [("", 900, None), ("Skip to content", 10, 0), ("Loading settings…", 10, 0)]

    result = _extract(shell, page_age_ms=PAST_THE_WAIT_MS)

    assert "__retry_after_ms" not in result
    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert snapshot.status == SnapshotStatus.ERROR
    assert "layout may have changed" in (snapshot.error or "")


@needs_node
@pytest.mark.parametrize(
    "pathname", ["/codex/cloud/settings/analytics", "/settings/usage-history"]
)
def test_only_settings_usage_is_polled(pathname):
    """The poll is for Settings > Usage; the old page keeps its old reading."""
    result = _extract(_without_plan_limits(OVERVIEW), pathname=pathname)

    assert "__retry_after_ms" not in result


@needs_node
@pytest.mark.parametrize("shown,used", [("0% left", 100), ("100% left", 0)])
def test_a_weekly_limit_at_either_end_is_read_at_once(shown, used):
    """0 is a percentage: a limit used up must not wait out the poll."""
    dom = [(t.replace("58% left", shown), h, p) for t, h, p in OVERVIEW]

    result = _extract(dom)

    assert "__retry_after_ms" not in result, result.get("__retry_reason")
    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert [(m.label, m.percent_used) for m in snapshot.metrics] == [("Weekly", used)]


# The same page with each card's text in one element. On the nested page above
# the usage container is never found - the card holding "58% left" also holds
# its "Weekly limit" label node, so no element is both innermost-marked and
# percentage-bearing, and discovery returns None (logged daily as
# discovery_no_container). Flattened, the container is found and every row in
# it reaches the adoption rules, which is the case worth pinning.
FLAT_OVERVIEW: Dom = [
    ("", 900, None),
    ("Usage Overview Analytics", 20, 0),
    ("", 600, 0),
    ("Plan limits Shared across Codex, Work, Workspace Agents, and ChatGPT for Excel.", 20, 2),
    ("Weekly limit Resets in 4d 7h 58% left", 40, 2),
    ("Credits 2,500 of 2,500 monthly credits left Resets in 26d 9h", 40, 2),
    ("Automatic reload Up to 40% off", 40, 2),
    ("Daily usage Usage data is approximate", 20, 2),
    ("Sep 6, 2026 Cloud 0%", 40, 2),
    ("Sep 6, 2026 Web 1%", 40, 2),
    ("Sep 6, 2026 CLI 0%", 40, 2),
]


@needs_node
@pytest.mark.parametrize("dom", [OVERVIEW, FLAT_OVERVIEW], ids=["nested", "flat"])
def test_nothing_else_on_the_overview_is_adopted_as_a_meter(dom, tmp_path):
    """Daily usage ("Sep 6, 2026 Cloud 0%") and "Up to 40% off" carry
    percentages. Neither has used/left wording, so the weekly scan - which
    runs on this page once it reads - must adopt neither."""
    from aigauge.providers.catalog import adopt_rows

    result = _extract(dom)
    if dom is FLAT_OVERVIEW:
        labels = {row["label"] for row in result["discovered"]}
        assert {"Sep 6, 2026 Cloud", "Automatic reload Up to"} <= labels, (
            "the flat page must put the furniture in front of the adoption rules"
        )

    assert adopt_rows("codex", result["discovered"], base_dir=tmp_path) == []


FIVE_HOUR_THEN_WEEKLY_FLAT: Dom = [
    ("", 900, None),
    ("", 400, 0),
    ("Plan limits", 20, 1),
    ("Shared across Codex, Work, Workspace Agents, and ChatGPT for Excel.", 20, 1),
    ("5-hour limit", 20, 1),
    ("Resets in 2h 10m", 20, 1),
    ("12% left", 20, 1),
    ("Weekly limit", 20, 1),
    ("Resets in 4d 7h", 20, 1),
    ("58% left", 20, 1),
]


@needs_node
def test_a_neighbouring_card_rendered_as_flat_siblings_does_not_lend_weekly_its_number():
    """With no element wrapping the weekly card alone, the smallest element
    holding "Weekly limit" and a percentage is the whole section, and its
    first percentage was the 5-hour card's: Weekly 88% used, reported as
    healthy. The read starts at the label."""
    result = _extract(FIVE_HOUR_THEN_WEEKLY_FLAT)

    weekly = result["rows"]["weekly"]
    assert (weekly["percent"], weekly["kind"], weekly["reset_text"]) == (58, "remaining", "4d 7h")
    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert [(m.label, m.percent_used) for m in snapshot.metrics] == [("Weekly", 42)]


# Two cards, each in its own box with its own label and percentage elements,
# the way the screenshot draws the one card there is today.
TWO_CARDS_NESTED: Dom = [
    ("", 900, None),
    ("", 400, 0),
    ("Plan limits", 20, 1),
    ("Shared across Codex, Work, Workspace Agents, and ChatGPT for Excel.", 20, 1),
    ("", 60, 1),
    ("5-hour limit", 20, 4),
    ("Resets in 2h 10m", 20, 4),
    ("12% left", 20, 4),
    ("", 60, 1),
    ("Weekly limit", 20, 8),
    ("Resets in 4d 7h", 20, 8),
    ("58% left", 20, 8),
]


@needs_node
def test_a_second_limit_neither_becomes_session_nor_disturbs_weekly():
    """What the CHANGELOG's Known section says. "5-hour limit" is not a
    Session alias, because the page's own resets prose says "5-hour limit"
    and would match it; and with each card's name and number in separate
    elements the scan finds no panel, so a second limit goes unread. Weekly
    still reads its own card."""
    result = _extract(TWO_CARDS_NESTED)

    assert result["session"] is None and "session" not in result["rows"]
    weekly = result["rows"]["weekly"]
    assert (weekly["percent"], weekly["kind"], weekly["reset_text"]) == (58, "remaining", "4d 7h")
    assert result["discovered"] is None
    snapshot = _build_snapshot(result, catalog=bundled_catalog("codex"))
    assert [(m.label, m.percent_used) for m in snapshot.metrics] == [("Weekly", 42)]


@needs_node
def test_daily_usage_is_never_the_weekly_limit():
    """Before the plan limits render, "weekly limit" is only in the resets
    prose. A Daily usage row that ever carries "used" must not be read from
    it as the weekly limit: Daily usage bounds the weekly card."""
    dom = [
        (t + " used" if t.endswith("%") else t, h, p)
        for t, h, p in _without_plan_limits(OVERVIEW)
    ]
    assert sum(t.endswith("% used") for t, _h, _p in dom) == 3

    result = _extract(dom)

    assert result.get("__retry_reason") == "usage panel not ready"


# --- python side -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,delta",
    [
        ("4d 7h", timedelta(days=4, hours=7)),
        ("26d 9h", timedelta(days=26, hours=9)),
        ("5d", timedelta(days=5)),
        ("in 3 days 4 hours", timedelta(days=3, hours=4)),
        ("1 day", timedelta(days=1)),
        ("2h 59m", timedelta(hours=2, minutes=59)),
        ("45m", timedelta(minutes=45)),
        ("4d7h", timedelta(days=4, hours=7)),
        ("4d, 7h", timedelta(days=4, hours=7)),
        ("4 days, 7 hours", timedelta(days=4, hours=7)),
    ],
)
def test_relative_resets_with_days_are_parsed(text, delta):
    parsed = _parse_reset_text(text)

    assert parsed is not None, f"{text!r} did not parse"
    assert abs((parsed - (datetime.now() + delta)).total_seconds()) < 60


@pytest.mark.parametrize("text", ["4daily", "8 de octubre", "3 dec", "1 dy"])
def test_a_d_that_runs_into_a_word_is_not_days(text):
    parsed = _parse_reset_text(text)

    assert parsed is None or parsed - datetime.now() < timedelta(days=1)


@pytest.mark.parametrize("text", ["3000000d", "80000000h"])
def test_an_impossible_countdown_is_no_reset_rather_than_a_crash(text):
    assert _parse_reset_text(text) is None


def test_a_weekday_reset_is_not_taken_for_days():
    parsed = _parse_reset_text("Mon 6:00 PM")

    assert parsed is not None and parsed.weekday() == 0
    assert (parsed.hour, parsed.minute) == (18, 0)


@pytest.mark.parametrize(
    "body",
    [
        "Plan limits Shared across Codex, Work, Workspace Agents, and ChatGPT for Excel.",
        "PLAN LIMITS Weekly limit 58% left",
        "Shared across Codex and Work",
    ],
)
def test_the_settings_usage_wording_is_strong_evidence_of_a_weekly_only_layout(body):
    """Strong, so a weekly limit at 0% used is believed rather than retried."""
    assert _weekly_only_layout_evidence({"body_text": body}) == "strong"


def test_an_untouched_weekly_limit_reads_as_zero_not_as_a_partial_render():
    payload = {
        "url": CODEX_USAGE_URL,
        "body_text": "Plan limits Shared across Codex Weekly limit Resets in 7d 100% left",
        "rows": {"weekly": {"percent": 100, "kind": "remaining", "reset_text": "7d",
                            "raw": "Weekly limit Resets in 7d 100% left"}},
        "has_shared_agentic_text": True,
    }
    snapshot = _build_snapshot(payload, catalog=bundled_catalog("codex"))

    assert snapshot.status == SnapshotStatus.OK, snapshot.error
    assert [(m.label, m.percent_used) for m in snapshot.metrics] == [("Weekly", 0)]


@pytest.mark.parametrize(
    "url,expected",
    [
        (CODEX_ANALYTICS_URL + "?aigauge_ts=1#personal-usage", True),
        # Settings > Usage is not the analytics page: the empty-page test that
        # rides on this counts "chatgpt" as signed-in evidence, and every
        # chatgpt.com page says it.
        ("https://chatgpt.com/settings/usage?tab=overview&aigauge_ts=1", False),
        ("https://chatgpt.com/settings/usage?tab=analytics#personal-usage", False),
        ("https://evil.example/codex/cloud/settings/analytics", False),
    ],
)
def test_only_the_old_analytics_page_gets_the_empty_page_test(url, expected):
    assert _is_codex_analytics_url(url) is expected


def test_the_weekly_meter_knows_its_settings_usage_label():
    weekly = next(s for s in bundled_catalog("codex").specs if s.key == "weekly")

    assert "Weekly limit" in weekly.aliases
    assert weekly.aliases[0] == "Weekly usage limit", "the old label is still tried first"


def test_the_scraper_and_the_verifier_target_the_same_page():
    verify_url, _js = VERIFY_TARGETS["codex"]

    assert CODEX_USAGE_URL == f"{CODEX_USAGE_PAGE}?tab=overview"
    assert verify_url == CODEX_USAGE_URL


def test_the_scrape_loads_the_overview_tab():
    import inspect

    from aigauge.providers.codex import CodexProvider

    source = inspect.getsource(CodexProvider)
    assert 'url=f"{CODEX_USAGE_PAGE}?tab=overview&aigauge_ts={cache_buster}"' in source
    assert "CODEX_ANALYTICS_URL" not in source


def _runner_kwargs(monkeypatch) -> dict:
    """What CodexProvider.refresh really hands ScrapeRunner."""
    from aigauge.providers import codex

    seen: dict = {}

    class _Runner:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self, on_done):
            pass

    monkeypatch.setattr(codex, "ScrapeRunner", _Runner)
    monkeypatch.setattr(codex, "account_is_busy", lambda _account: False)
    codex.CodexProvider(account_id="codex").refresh(lambda _snapshot: None)
    return seen


def test_the_poll_ends_on_the_extractor_wait_inside_the_timeout(monkeypatch):
    """The wait for the plan limits is the extractor's (USAGE_PANEL_WAIT_MS of
    page age), so it ends in a read Python classifies. The scraper's rerun cap
    must not end it first - at 20 reruns it gave up 24 s in with 16 s of the
    timeout unused, as "extractor retry limit exceeded" - and must itself end
    before the timeout, so a page whose clock restarts still leaves its text
    in the log. On 2026-10-02 the page took 23 s to report loaded."""
    from aigauge.webview.scraper import default_soft_ready_ms

    kwargs = _runner_kwargs(monkeypatch)
    timeout = kwargs["timeout_ms"]
    wait = kwargs["wait_ms"]
    reruns = kwargs["max_extractor_reruns"]

    # Reruns are a second apart (the extractor's __retry_after_ms).
    assert wait + reruns * 1000 > USAGE_PANEL_WAIT_MS, "the cap would end the wait"
    assert wait + reruns * 1000 <= timeout - 2000, "the cap would lose to the timeout"
    assert USAGE_PANEL_WAIT_MS <= timeout - 8000, "no room left for the last read"
    assert kwargs["soft_ready_ms"] == default_soft_ready_ms(wait, timeout)
    assert kwargs["soft_ready_ms"] < USAGE_PANEL_WAIT_MS


def test_the_extractor_carries_the_wait():
    from aigauge.providers.codex import EXTRACTOR_JS

    assert "__CODEX_PANEL_WAIT_MS__" not in EXTRACTOR_JS
    assert f"pageAgeMs < {USAGE_PANEL_WAIT_MS}" in EXTRACTOR_JS
