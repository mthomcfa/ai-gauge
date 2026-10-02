"""Behavioural tests for how the Claude extractor reaches the usage surface.

The failure these guard against, observed live on 2026-08-10: the app
navigated to ``claude.ai/new#settings/usage``, Claude no longer opened the
settings dialog from that hash, and the extractor sat on the signed-in home
screen retrying until it exhausted its budget - on every refresh, forever.

The route check was the reason it could not recover. ``onUsageRoute()`` tests
the URL *shape*, and the app navigates to a usage URL itself, so the check was
true from the first poll and the recovery path never ran. Route decisions are
now made on rendered evidence.

The surface then moved back. From 2026-10-02 ``/settings/usage`` forwards to
``/new#settings/usage`` mid-load, and that hash opens Claude's in-app usage
view again, so the scraper loads it and it is the one recovery candidate.
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


@pytest.mark.parametrize(
    "pathname,hash_",
    [("/new", ""), ("/settings/usage", ""), ("/", "")],
    ids=["home", "old-usage-path", "root"],
)
def test_a_page_without_the_usage_view_is_sent_to_it(pathname, hash_):
    """The signed-in home screen, or the route the view used to live on,
    with nothing usage-shaped rendered. From /new this is a hash change -
    the same step Claude's own forward takes to open the view."""
    r = _ensure_route(pathname, hash_, HOME)

    assert r["navigated"] == "/new#settings/usage", "did not attempt to recover"
    assert r["reason"], "recovery must be reported as a retry reason"


def test_being_on_the_right_route_but_unhydrated_is_never_re_routed():
    """A slow page must not be navigated away from.

    This previously fell back to the legacy hash route, which turned out to be
    actively harmful. Observed live on 2026-08-10: the settings page loads i18n,
    org, feature, memory, MCP and marketplace endpoints before usage, so
    body_text is "Loading..." for seconds. No usage panel has rendered yet, the
    fallback fired, and it navigated away from the correct route to one we have
    direct evidence does not open the dialog - discarding the load that was
    about to succeed, and with it the recorded API capture. One account in that
    run came back with api={} for exactly this reason while the other, which
    had not yet re-routed, carried eight endpoints.
    """
    for body in (HOME, "Loading..."):
        r = _ensure_route("/new", "#settings/usage", body)
        assert r["navigated"] is None, f"navigated away while body was {body!r}"


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


@pytest.mark.parametrize("tries", [1, 2])
def test_navigation_is_bounded_so_a_bouncing_route_cannot_spin(tries):
    r = _ensure_route("/new", "", HOME, tries=tries)

    assert r["navigated"] is None, "kept navigating after the candidate was spent"


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

    assert CLAUDE_USAGE_URL == "https://claude.ai/new#settings/usage"
    assert verify_url == CLAUDE_USAGE_URL


def test_the_first_route_candidate_is_the_url_the_scraper_loads():
    # Otherwise the very first poll would navigate away from the page the
    # scraper just fetched, wasting a load on every single refresh.
    from urllib.parse import urlparse

    source = _route_source()
    first = source.split("ROUTE_CANDIDATES = [")[1].split("]")[0].split(",")[0]
    loaded = urlparse(CLAUDE_USAGE_URL)

    assert first.strip().strip("'\"") == f"{loaded.path}#{loaded.fragment}"
