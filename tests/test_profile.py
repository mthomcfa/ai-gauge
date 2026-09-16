import sys

import pytest

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


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege on Windows")
def test_purge_profile_never_deletes_through_a_link():
    """A link inside profiles/ is refused at the primitive, not just by the sweep.

    `pending_profile_purges` and `pending_data_clears` come from config.json,
    so an id can reach `purge_profile` without passing the dialog's filter; a
    link named there pointed at a live account's profile and deleted it.
    """
    live = webview_profile_dir("claude-live")
    live.mkdir(parents=True)
    (live / "Cookies").write_text("session")
    link = app_data_dir() / "profiles" / "claude-alias"
    try:
        link.symlink_to(live, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this filesystem refuses symlinks")

    purge_profile("claude-alias")

    assert (live / "Cookies").read_text() == "session"
    assert link.is_symlink()
