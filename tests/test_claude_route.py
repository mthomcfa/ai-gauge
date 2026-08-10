"""Behavioural tests for how the Claude extractor reaches the usage surface.

The failure these guard against, observed live on 2026-08-10: the app
navigated to ``claude.ai/new#settings/usage``, Claude no longer opened the
settings dialog from that hash, and the extractor sat on the signed-in home
screen retrying until it exhausted its budget - on every refresh, forever.

The route check was the reason it could not recover. ``onUsageRoute()`` tests
the URL *shape*, and the app navigates to a usage URL itself, so the check was
true from the first poll and the recovery path never ran. Route decisions are
now made on rendered evidence.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from aigauge.providers.claude import CLAUDE_USAGE_URL, EXTRACTOR_JS
from aigauge.webview.verify import VERIFY_TARGETS

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)

# What Claude's signed-in home screen actually renders, from the user's log.
HOME = (
    "Home Code New Chats and tasks Projects Artifacts Scheduled Customize "
    "Recents Design MT Michael Max Hey there, Michael How can I help you today?"
)
USAGE = "Plan usage Current session 8% used Weekly 12% used"


def _route_source() -> str:
    """Anchored on code, not comments, so reformatting cannot silently skew it."""
    start = EXTRACTOR_JS.index("const usagePanelSignals")
    end = EXTRACTOR_JS.index("const routeReason")
    block = EXTRACTOR_JS[start:end]
    assert "function ensureUsageRoute" in block, "ensureUsageRoute not in the block"
    return block


def _ensure_route(pathname, hash_, body, *, host="claude.ai", tries=0) -> dict:
    script = f"""
    let navigated = null, stored = {json.dumps(str(tries))};
    globalThis.sessionStorage = {{
      getItem: () => stored, setItem: (k, v) => {{ stored = v; }},
    }};
    globalThis.location = {{
      hostname: {json.dumps(host)}, pathname: {json.dumps(pathname)},
      hash: {json.dumps(hash_)},
      set href(v) {{ navigated = v; }}, get href() {{ return "x"; }},
    }};
    const bodyText = {json.dumps(body)};
    {_route_source()}
    process.stdout.write(JSON.stringify(
      {{reason: ensureUsageRoute(), navigated, tries: stored}}));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_hash_route_no_longer_opening_the_dialog_is_recovered():
    """The exact live failure.

    URL carries #settings/usage, page shows the home screen. The old check saw
    the hash, declared itself already on the usage route, and returned without
    acting - so the extractor waited for rows that were never coming.
    """
    r = _ensure_route("/new", "#settings/usage", HOME)

    assert r["navigated"] == "/settings/usage", "did not attempt to recover"
    assert r["reason"], "recovery must be reported as a retry reason"


def test_a_bounced_direct_route_falls_back_to_the_legacy_hash_route():
    # Already on /settings/usage but the home screen rendered: navigating there
    # again is a no-op, so the next candidate must be tried instead.
    r = _ensure_route("/settings/usage", "", HOME)

    assert r["navigated"] == "/new#settings/usage"


@pytest.mark.parametrize(
    "pathname,hash_",
    [("/settings/usage", ""), ("/new", "#settings/usage"), ("/new", "")],
)
def test_a_rendered_usage_panel_is_never_navigated_away_from(pathname, hash_):
    # Rendered evidence wins wherever it appears - including somewhere neither
    # candidate URL predicted, which is how this surface keeps moving.
    r = _ensure_route(pathname, hash_, USAGE)

    assert r["navigated"] is None
    assert r["reason"] is None


def test_navigation_is_bounded_so_a_bouncing_route_cannot_spin():
    r = _ensure_route("/new", "", HOME, tries=2)

    assert r["navigated"] is None, "kept navigating after both candidates were spent"


def test_a_foreign_host_is_never_navigated():
    # An open redirect must not be able to drive navigation.
    r = _ensure_route("/new", "", HOME, host="evil.com")

    assert r["navigated"] is None


def test_the_scraper_and_the_verifier_target_the_same_url():
    """Nothing pinned this before, and the two drifting apart is a known trap.

    A verifier that loads a different surface than the extractor is how sign-in
    reported success while the tile errored forever.
    """
    verify_url, _js = VERIFY_TARGETS["claude"]

    assert CLAUDE_USAGE_URL == "https://claude.ai/settings/usage"
    assert verify_url == CLAUDE_USAGE_URL


def test_the_first_route_candidate_is_the_url_the_scraper_loads():
    # Otherwise the very first poll would navigate away from the page the
    # scraper just fetched, wasting a load on every single refresh.
    source = _route_source()
    first = source.split("ROUTE_CANDIDATES = [")[1].split("]")[0].split(",")[0]

    assert first.strip().strip("'\"") == "/settings/usage"
