from PyQt6.QtCore import QUrl

from aigauge.webview.login_window import (
    VERIFY_TARGETS,
    _blocked_main_frame_scheme,
    _host_allowed,
    _is_google_host,
    _safe_url_for_log,
)


def test_opaque_and_foreign_schemes_are_blocked_in_sign_in_top_frame():
    # data: and blob: are opaque-origin phishing canvases in a chrome-less
    # window; other non-web schemes are blocked too.
    assert _blocked_main_frame_scheme("data") is True
    assert _blocked_main_frame_scheme("blob") is True
    assert _blocked_main_frame_scheme("javascript") is True
    assert _blocked_main_frame_scheme("file") is True
    assert _blocked_main_frame_scheme("ftp") is True


def test_web_and_about_schemes_are_allowed_in_sign_in_top_frame():
    assert _blocked_main_frame_scheme("http") is False
    assert _blocked_main_frame_scheme("https") is False
    assert _blocked_main_frame_scheme("about") is False


def test_google_hosts_are_detected_and_allowlisted():
    assert _is_google_host("accounts.google.com")
    assert _is_google_host("google.com")
    assert _host_allowed("accounts.google.com")
    assert _host_allowed("accounts.youtube.com")


def test_logged_blocked_url_drops_query_and_fragment():
    url = QUrl(
        "https://accounts.google.com/o/oauth2/v2/auth?"
        "login_hint=person@example.com#frag"
    )

    assert _safe_url_for_log(url) == "https://accounts.google.com/o/oauth2/v2/auth"

def test_opencode_go_verify_target_checks_authenticated_shell():
    url, check_js = VERIFY_TARGETS["opencode_go"]

    assert url.startswith("https://opencode.ai/workspace/")
    # Verifies the signed-in workspace shell, not rendered usage meters: an
    # account with no usage rows yet was wrongly reported as signed out.
    assert "Rolling Usage" not in check_js
    for marker in ("api keys", "members", "billing", "settings"):
        assert marker in check_js
    # Host + workspace path are still pinned so a redirect can't satisfy it.
    assert "opencode.ai" in check_js
    assert "workspace" in check_js
