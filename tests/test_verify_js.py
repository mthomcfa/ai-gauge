"""Behavioral tests for the sign-in verification JavaScript.

The verify snippets previously had only substring assertions, which are
tautological: a marker word surviving in a *comment* satisfied them even when
the guard it described had been deleted. These tests execute the real snippet
against a stubbed DOM so the assertions track behavior instead of text.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from aigauge.webview.verify import VERIFY_TARGETS

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the verify JS"
)

_HARNESS = """
globalThis.document = {
  body: { innerText: BODY },
  title: TITLE,
  querySelectorAll: () => [],
};
globalThis.location = { hostname: HOST, pathname: PATH, href: "https://" + HOST + PATH };
const result = (CHECK);
process.stdout.write(JSON.stringify(result === true));
"""


def _run_check(provider: str, *, body: str, host: str, path: str, title: str = "") -> bool:
    _url, check_js = VERIFY_TARGETS[provider]
    script = (
        _HARNESS.replace("BODY", json.dumps(body))
        .replace("TITLE", json.dumps(title))
        .replace("HOST", json.dumps(host))
        .replace("PATH", json.dumps(path))
        .replace("CHECK", check_js)
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ----- Codex -----


def test_codex_verify_accepts_classic_session_plus_weekly_layout():
    assert _run_check(
        "codex",
        body="Personal usage 5 hour usage limit 12% used Weekly usage limit 40% used",
        host="chatgpt.com",
        path="/codex/cloud/settings/analytics",
    )


def test_codex_verify_accepts_weekly_only_with_shared_agentic_markers():
    assert _run_check(
        "codex",
        body="Personal usage Shared agentic usage limit Weekly usage limit 40% used",
        host="chatgpt.com",
        path="/codex/cloud/settings/analytics",
    )


def test_codex_verify_accepts_current_shared_limit_wording():
    # Real page text from a user's log (2026-08-06). OpenAI moved from
    # "shared agentic usage limit" to "Codex and Work share the same usage
    # limit"; verify.py and providers/codex.py must recognise both, or sign-in
    # and extraction disagree and the tile errors forever.
    assert _run_check(
        "codex",
        body=(
            "Codex and Work Analytics Personal usage "
            "Codex and Work share the same usage limit. "
            "Weekly usage limit 100% remaining "
            "Workspace monthly credit limit 100% remaining"
        ),
        host="chatgpt.com",
        path="/codex/cloud/settings/analytics",
    )


def test_codex_verify_rejects_bare_weekly_without_layout_markers():
    # Must mirror providers/codex.py::_build_snapshot. Accepting this here while
    # the extractor rejected it made sign-in report success and then error on
    # every refresh, with no way for the user to recover.
    assert not _run_check(
        "codex",
        body="Personal usage Weekly usage limit 40% used",
        host="chatgpt.com",
        path="/codex/cloud/settings/analytics",
    )


def test_codex_verify_rejects_page_without_any_usage_text():
    assert not _run_check(
        "codex", body="Log in to continue", host="chatgpt.com", path="/auth/login"
    )


# ----- OpenCode -----

_SHELL = "Usage API keys Members Billing Settings"


def test_opencode_verify_accepts_authenticated_workspace_shell():
    assert _run_check(
        "opencode_go", body=_SHELL, host="opencode.ai", path="/workspace/wrk_1/go"
    )


def test_opencode_verify_accepts_member_without_admin_nav_entries():
    # A non-admin member sees no API keys / Billing entries. Requiring every
    # marker reported those valid sessions as signed out.
    assert _run_check(
        "opencode_go",
        body="Usage Members Settings",
        host="opencode.ai",
        path="/workspace/wrk_1/go",
    )


def test_opencode_verify_accepts_subdomain_and_non_workspace_path():
    # config.validate_opencode_usage_url accepts these, so verification must
    # not permanently fail for a Usage URL this app itself blesses.
    assert _run_check(
        "opencode_go", body=_SHELL, host="app.opencode.ai", path="/workspace/wrk_1/go"
    )
    assert _run_check(
        "opencode_go", body=_SHELL, host="opencode.ai", path="/settings/usage"
    )


def test_opencode_verify_rejects_foreign_host_even_with_shell_text():
    # The host pin is the actual guard; deleting it must fail this test.
    assert not _run_check(
        "opencode_go", body=_SHELL, host="evil.com", path="/workspace/wrk_1/go"
    )
    assert not _run_check(
        "opencode_go", body=_SHELL, host="opencode.ai.evil.com", path="/workspace/wrk_1/go"
    )


def test_opencode_verify_rejects_root_path_and_sparse_text():
    assert not _run_check("opencode_go", body=_SHELL, host="opencode.ai", path="/")
    assert not _run_check(
        "opencode_go", body="Usage policy", host="opencode.ai", path="/workspace/wrk_1/go"
    )
