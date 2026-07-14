from aigauge.config import app_data_dir, webview_profile_dir
from aigauge.webview.profile import purge_profile


def test_purge_profile_deletes_valid_dir():
    profile_dir = webview_profile_dir("claude-deadbeef")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Cookies").write_bytes(b"data")

    purge_profile("claude-deadbeef")

    assert not profile_dir.exists()


def test_purge_profile_refuses_traversal_id():
    # A sentinel one level above profiles/ must survive an unsafe id: the
    # traversal id is rejected by webview_profile_dir() and nothing is deleted.
    sentinel = app_data_dir() / "keep.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("keep")

    purge_profile("../../keep.txt")

    assert sentinel.exists()


def test_purge_profile_missing_dir_is_noop():
    # Removing an account that never opened a browser must not raise.
    purge_profile("codex-00000000")
