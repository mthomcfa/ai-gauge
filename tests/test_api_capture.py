"""Behavioural tests for the API recorder, executed in node.

The recorder runs inside a provider page that fetches far more than usage -
on claude.ai that includes conversation content. These tests exist mostly to
pin the redaction: the sketch must carry the quota numbers and reset
timestamps while dropping every other string, because the result reaches the
log and the clipboard.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from aigauge.webview.api_capture import READBACK_JS, RECORDER_JS

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the recorder JS"
)

_HARNESS = """
globalThis.window = globalThis;
globalThis.location = {{ href: 'https://claude.ai/settings/usage', hostname: 'claude.ai' }};
function makeRes(url, body, ct) {{
  return {{
    url,
    headers: {{ get: h => (h === 'content-type' ? (ct || 'application/json') : null) }},
    clone: () => ({{ text: () => Promise.resolve(body) }}),
  }};
}}
globalThis.__responses = {responses};
globalThis.fetch = (u) => Promise.resolve(
  makeRes(u, JSON.stringify(globalThis.__responses[u]), globalThis.__ctype || null));
globalThis.XMLHttpRequest = function () {{}};
globalThis.XMLHttpRequest.prototype.open = function () {{}};
{ctype_override}
{recorder}
Promise.all(Object.keys(globalThis.__responses).map(u => window.fetch(u)))
  .then(() => new Promise(r => setTimeout(r, 20)))
  .then(() => process.stdout.write(JSON.stringify({readback})));
"""


def _capture(responses: dict, *, ctype: str | None = None) -> dict:
    script = _HARNESS.format(
        responses=json.dumps(responses),
        recorder=RECORDER_JS,
        readback=READBACK_JS,
        ctype_override=(
            f"globalThis.__ctype = {json.dumps(ctype)};" if ctype else ""
        ),
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


SECRET = "PRIVATE CONVERSATION TEXT THAT MUST NOT BE KEPT"


def test_quota_numbers_and_reset_timestamps_survive():
    """The two things the whole exercise is for.

    A number needs no polarity inference and a timestamp needs no countdown
    parsing - between them they retire both classes of bug that this fork has
    shipped and re-shipped.
    """
    got = _capture({
        "https://claude.ai/api/usage": {
            "five_hour": {"utilization": 0.64, "resets_at": "2026-08-10T14:00:00Z"},
            "seven_day": {"utilization": 0.12, "resets_at": "2026-08-14T00:00:00Z"},
        }
    })

    usage = got["/api/usage"]
    assert usage["five_hour"]["utilization"] == 0.64
    assert usage["seven_day"]["utilization"] == 0.12
    assert usage["five_hour"]["resets_at"] == "2026-08-10T14:00:00Z"


def test_page_content_never_reaches_the_capture():
    got = _capture({
        "https://claude.ai/api/conversations": {
            "conversations": [{"name": SECRET, "uuid": "abc"}],
            "account": {"email": "person@example.com", "full_name": "A Real Person"},
        }
    })

    blob = json.dumps(got)
    assert SECRET not in blob
    assert "person@example.com" not in blob
    assert "A Real Person" not in blob
    # Redacted to a length marker, not dropped - the field name is what makes
    # a mapping writable later.
    assert got["/api/conversations"]["account"]["email"] == f"<str:{len('person@example.com')}>"


def test_booleans_are_kept_and_arrays_are_summarised():
    got = _capture({
        "https://claude.ai/api/x": {
            "is_limited": True,
            "rows": [{"pct": 12}, {"pct": 44}, {"pct": 91}],
            "empty": [],
        }
    })

    x = got["/api/x"]
    assert x["is_limited"] is True
    assert x["rows"] == [{"pct": 12}, "<len:3>"], "array shape plus length only"
    assert x["empty"] == []


def test_a_cross_origin_response_is_ignored_entirely():
    got = _capture({
        "https://telemetry.example.com/api/beacon": {"utilization": 0.99},
        "https://claude.ai/api/usage": {"utilization": 0.5},
    })

    assert "/api/beacon" not in got
    assert "/api/usage" in got


def test_a_non_json_response_is_not_cloned_or_recorded():
    # Cloning buffers the body; doing that to a streamed response would hold
    # the whole thing in memory.
    got = _capture({"https://claude.ai/api/stream": {"utilization": 0.5}},
                   ctype="text/event-stream")

    assert got == {}


def test_deeply_nested_structures_are_bounded():
    deep = cur = {}
    for _ in range(12):
        cur["next"] = {}
        cur = cur["next"]
    cur["utilization"] = 0.5
    got = _capture({"https://claude.ai/api/deep": deep})

    assert "<object>" in json.dumps(got["/api/deep"]), "depth was not capped"


# --- wiring ----------------------------------------------------------------


def test_the_extractor_hands_the_capture_back_with_every_payload():
    """Behavioural, not a source-text count.

    Runs the real EXTRACTOR_JS against a stub page with no usage panel - the
    retry path, which is the shape a user in trouble actually produces - and
    checks the sketch travelled with it. A payload that drops the capture is a
    payload that cannot be used to write the mapping.
    """
    from aigauge.providers.claude import EXTRACTOR_JS

    # The extractor is a statement ending in ";" - bind it, do not nest it.

    script = f"""
    globalThis.window = globalThis;
    globalThis.__ag_api = {{ "/api/usage": {{ five_hour: {{ utilization: 0.64 }} }} }};
    globalThis.sessionStorage = {{ getItem: () => '9', setItem: () => {{}} }};
    globalThis.location = {{
      hostname: 'claude.ai', pathname: '/settings/usage', hash: '',
      href: 'https://claude.ai/settings/usage',
    }};
    globalThis.document = {{
      body: {{ innerText: 'Home Code Projects Hey there' }},
      title: 'New chat - Claude',
      querySelector: () => null,
      querySelectorAll: () => [],
    }};
    const payload = {EXTRACTOR_JS.strip().rstrip(";")};
    process.stdout.write(JSON.stringify(payload));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)

    assert payload.get("__retry_reason"), "expected the not-ready retry payload"
    assert payload["api"]["/api/usage"]["five_hour"]["utilization"] == 0.64


def test_capture_is_off_unless_a_provider_asks_for_it():
    """It must stay opt-in.

    The recorder wraps fetch on the host page; a provider that does not need
    it should not have its networking touched at all.
    """
    import aigauge.webview.scraper as scraper_mod

    calls: list = []
    real = scraper_mod.install_api_recorder
    scraper_mod.install_api_recorder = lambda page: calls.append(page) or True
    try:
        import inspect

        sig = inspect.signature(scraper_mod.HeadlessScraper.__init__)
        assert sig.parameters["capture_api"].default is False
    finally:
        scraper_mod.install_api_recorder = real
    assert calls == []


def test_claude_opts_in_and_no_other_provider_does():
    import inspect

    from aigauge.providers import claude, codex, opencode_go

    assert "capture_api=True" in inspect.getsource(claude)
    for module in (codex, opencode_go):
        assert "capture_api" not in inspect.getsource(module), module.__name__


# --- tamper resistance -----------------------------------------------------


def _tamper(attack: str) -> dict:
    """Run the recorder, then let page script try to subvert the capture."""
    script = f"""
    globalThis.window = globalThis;
    globalThis.location = {{ href: 'https://claude.ai/x', hostname: 'claude.ai' }};
    globalThis.fetch = () => Promise.resolve({{}});
    globalThis.XMLHttpRequest = function () {{}};
    globalThis.XMLHttpRequest.prototype.open = function () {{}};
    {RECORDER_JS}
    try {{ {attack} }} catch (e) {{}}
    process.stdout.write(JSON.stringify({READBACK_JS}));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    "attack",
    [
        # Straight reassignment - what a third-party bundle would do.
        'window.__ag_api = {"/evil": {"planted": "ATTACKER"}};',
        # Redefine the property outright.
        'Object.defineProperty(window, "__ag_api", {value: {"/evil": 1}});',
        # Flood it, to blow past the log rotation.
        'window.__ag_api = {}; for (let i=0;i<5000;i++) window.__ag_api["f"+i] = i;',
    ],
)
def test_page_script_cannot_replace_the_capture(attack):
    """The main world is shared with everything the provider page loads.

    Verified against a real browser before this was fixed: a script replacing
    window.__ag_api with 50,000 keys produced a 1 MB log line against a 512 KiB
    rotation - destroying the user's existing diagnostics - and put
    attacker-chosen text into the blob users are told to paste into bug
    reports. The store now lives in a closure behind a non-configurable getter.
    """
    got = _tamper(attack)

    assert "/evil" not in got
    assert not any(k.startswith("f") for k in got), "flood keys reached the capture"
    assert "ATTACKER" not in json.dumps(got)
