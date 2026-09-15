import os
import sys
from pathlib import Path

import pytest

# Make `src/` importable
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


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
    from aigauge.providers import _scrape_runner

    _scrape_runner._ACTIVE_ACCOUNTS.clear()  # noqa: SLF001
    yield
    _scrape_runner._ACTIVE_ACCOUNTS.clear()  # noqa: SLF001
