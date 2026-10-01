import os
import sys
from pathlib import Path

import pytest

# Make `src/` importable
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# Headless unless told otherwise, before anything below can create a
# QApplication. The suite is written against Qt's offscreen platform, which is
# what CI runs: one 800 x 800 screen, no window manager, a fixed font, and a
# QWindow.startSystemResize() that declines so the drag arithmetic is the path
# under test. On a real desktop several of those stop holding - Windows takes
# the resize itself, a 2560 px monitor is wider than a test that relies on the
# small screen expects, real fonts fit labels the tests need to overflow - and a
# plain `pytest` failed seven tests that pass in CI, none of them a defect in
# the app. setdefault, so a run that names a platform still gets it.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Imported at collection time, on purpose: the scrape runner pulls in
# QtWebEngineWidgets, which Qt requires to be imported before the first
# QCoreApplication exists. A full run satisfies that by accident (test_app.py
# is collected first and imports it), but a single file run through a qtbot
# test would otherwise import it from the autouse fixture below, after the
# QApplication, and every test in the file errors at setup.
from aigauge.providers import _scrape_runner as _scrape_runner_module  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    """Redirect %APPDATA% so config writes never touch the user's real folder."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def no_leaked_live_scrapes():
    """Start and end every test with no account marked as mid-scrape.

    The live-scrape guard is module state keyed on the account, on purpose:
    a settings save rebuilds every provider and must not be able to drop it.
    The cost is that a test which starts a scrape through a fake runner and
    never completes it leaves the account busy for every later test, and the
    next `refresh()` on that account is refused as already_running. Which
    test leaks depends on what the platform skips - seven meter-catalog tests
    failed on the macOS runner alone - so the reset lives here, not in the
    file that happened to notice.
    """
    _scrape_runner_module._ACTIVE_ACCOUNTS.clear()  # noqa: SLF001
    yield
    _scrape_runner_module._ACTIVE_ACCOUNTS.clear()  # noqa: SLF001
