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
from aigauge.providers.catalog import bundled_catalog, extractor_source
from aigauge.providers.codex import (
    CODEX_ANALYTICS_URL,
    CODEX_USAGE_PAGE,
    CODEX_USAGE_URL,
    EXTRACTOR_TEMPLATE,
    _build_snapshot,
    _is_codex_usage_url,
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


def _extract(dom: Dom, pathname: str = "/settings/usage") -> dict:
    source = extractor_source(EXTRACTOR_TEMPLATE, bundled_catalog("codex"), discover=True)
    href = "https://chatgpt.com" + pathname + "?tab=overview"
    script = (
        (_DOM_STUB % {"dom": json.dumps(dom)})
        + f"globalThis.location = {{pathname: {json.dumps(pathname)}, "
        f"href: {json.dumps(href)}, hostname: 'chatgpt.com'}};\n"
        + "document.title = 'ChatGPT';\n"
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


@needs_node
def test_a_signed_out_page_is_still_reported_as_signed_out():
    """The poll must not hide a sign-out behind 'not ready'."""
    result = _extract(
        [("", 900, None), ("Log in", 10, 0), ("Sign up for free", 10, 0)]
    )

    assert "__retry_after_ms" not in result
    assert result["logged_out"] is True


@needs_node
def test_the_old_analytics_page_is_not_polled():
    """The poll is for Settings > Usage; the old page keeps its old reading."""
    result = _extract(_without_plan_limits(OVERVIEW), pathname="/codex/cloud/settings/analytics")

    assert "__retry_after_ms" not in result


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
    ],
)
def test_relative_resets_with_days_are_parsed(text, delta):
    parsed = _parse_reset_text(text)

    assert parsed is not None, f"{text!r} did not parse"
    assert abs((parsed - (datetime.now() + delta)).total_seconds()) < 60


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
        ("https://chatgpt.com/settings/usage?tab=overview&aigauge_ts=1", True),
        ("https://chatgpt.com/settings/usage?tab=analytics#personal-usage", True),
        (CODEX_ANALYTICS_URL + "?aigauge_ts=1#personal-usage", True),
        ("https://chatgpt.com/settings", False),
        ("https://chatgpt.com/settings/usage-history", False),
        ("https://evil.example/settings/usage", False),
    ],
)
def test_the_usage_page_is_recognised_at_either_address(url, expected):
    assert _is_codex_usage_url(url) is expected


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


def test_the_poll_fits_inside_the_timeout():
    """The extractor now waits for the plan limits: up to 20 reruns a second
    apart, after the early read or the post-load wait. On 2026-10-02 the page
    took 23 s to report loaded against the old 25 s timeout."""
    from aigauge.providers.codex import SCRAPE_TIMEOUT_MS
    from aigauge.webview.scraper import default_soft_ready_ms

    assert default_soft_ready_ms(3000, SCRAPE_TIMEOUT_MS) + 20 * 1000 <= SCRAPE_TIMEOUT_MS
