"""Pages the app reads count themselves visible, with no window on screen.

The app never shows the pages it reads, and a page whose view is never shown
reports itself hidden. Chromium throttles hidden pages hard - measured on Qt
WebEngine 6.11, a zero-delay timer chain ran 11 times in 3 s instead of about
720, and requestAnimationFrame stopped after one frame - and from 5 October
2026 claude.ai's app never finished drawing under that: both Claude tiles and
the ChatGPT tile timed out on every refresh, on a connection that delivered
the page in 0.1 s. In a real browser, against a page that boots through 300
zero-delay timers and two animation frames, the app's own Claude and ChatGPT
readers and the sign-in check fail hidden and read in about 3 s visible.

These pin the wiring with stand-ins: the suite never starts a real page.
"""

from __future__ import annotations

from aigauge.webview import scraper as scraper_module
from aigauge.webview import verify as verify_module
from aigauge.webview.page import QuietWebEnginePage


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot=None):
        if slot in self.slots:
            self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Page:
    """Qt's visibility and lifecycle rules, and the real keep/release code."""

    class LifecycleState:
        Active = "Active"
        Discarded = "Discarded"

    keep_visible = QuietWebEnginePage.keep_visible
    release_visible = QuietWebEnginePage.release_visible
    _reassert_visible = QuietWebEnginePage._reassert_visible

    def __init__(self, *_args, **_kwargs):
        self._keep_visible = False
        self.visible = False
        self.state = "Active"
        self.events = []
        for name in ("loadStarted", "loadProgress", "urlChanged", "loadFinished",
                     "renderProcessTerminated", "loadingChanged"):
            setattr(self, name, _Signal())

    def isVisible(self):  # noqa: N802 - Qt's name
        return self.visible

    def setVisible(self, visible):  # noqa: N802 - Qt's name
        self.visible = visible
        self.events.append(("visible", visible))

    def setLifecycleState(self, state):  # noqa: N802 - Qt's name
        # Qt refuses: "failed to transition from Active to Discarded state:
        # page is visible".
        if state == self.LifecycleState.Discarded and self.visible:
            self.events.append(("discard refused", state))
            return
        self.state = state
        self.events.append(("lifecycle", state))

    def deleteLater(self):  # noqa: N802 - Qt's name
        pass


SIGNALS = ("loadStarted", "loadProgress", "urlChanged", "loadFinished")


def test_a_page_kept_visible_counts_itself_visible_at_once():
    page = _Page()

    page.keep_visible()

    assert page.visible is True


def test_visibility_comes_back_on_every_load_signal():
    """Set before the page's first load, Qt loses the flag - the page's
    contents are created hidden, like the view - so setting it once did
    nothing. It is re-asserted on every load signal."""
    page = _Page()
    page.keep_visible()

    for name in SIGNALS:
        page.visible = False  # what Qt does to a flag set before the first load
        getattr(page, name).emit(*(() if name == "loadStarted" else (None,)))
        assert page.visible is True, name


def test_a_released_page_goes_hidden_and_stays_hidden():
    page = _Page()
    page.keep_visible()

    page.release_visible()
    for name in SIGNALS:
        getattr(page, name).emit(*(() if name == "loadStarted" else (None,)))

    assert page.visible is False


def test_the_scraper_keeps_its_page_visible(qtbot, monkeypatch):
    pages = []

    def make_page(*args, **kwargs):
        page = _Page()
        page.scripts = lambda: None
        page.load = lambda _url: None
        pages.append(page)
        return page

    class _View:
        def __init__(self):
            self.page = None

        def setPage(self, page):  # noqa: N802 - Qt's name
            self.page = page

        def resize(self, *_args):
            pass

        def width(self):
            return 1280

        def height(self):
            return 900

    profile = type("Profile", (), {
        "persistentStoragePath": lambda self: "/tmp/profile",
        "httpUserAgent": lambda self: "UA",
    })()
    monkeypatch.setattr(scraper_module, "QuietWebEnginePage", make_page)
    monkeypatch.setattr(scraper_module, "QWebEngineView", _View)
    monkeypatch.setattr(scraper_module, "get_profile", lambda _provider: profile)

    scraper = scraper_module.HeadlessScraper("claude", "https://claude.ai/new", "1")
    try:
        assert pages and pages[0].visible is True
        assert pages[0]._keep_visible is True
    finally:
        scraper._timeout.stop()
        scraper._soft_ready.stop()


def test_the_sign_in_check_keeps_its_page_visible(qtbot, monkeypatch):
    pages = []

    def make_page(*args, **kwargs):
        page = _Page()
        page.settings = lambda: type("S", (), {"setAttribute": lambda *_a: None})()
        page.load = lambda _url: None
        pages.append(page)
        return page

    monkeypatch.setattr(verify_module, "QuietWebEnginePage", make_page)
    monkeypatch.setattr(verify_module, "get_profile", lambda _account: None)

    checker = verify_module.SessionVerifier("codex")
    try:
        assert pages and pages[0].visible is True
    finally:
        checker._timeout.stop()


def _scraper_stand_in(page):
    class _View:
        # As Qt does: stopping a load reports it finished, and detaching the
        # view hides the page.
        def stop(self):
            page.loadFinished.emit(False)

        def setPage(self, new_page):  # noqa: N802 - Qt's name
            if new_page is None:
                page.setVisible(False)

        def deleteLater(self):  # noqa: N802 - Qt's name
            pass

    view = _View()
    stand_in = type("Scraper", (), {"deleteLater": lambda self: None})()
    stand_in._page = page
    stand_in._view = view
    for name in ("_on_load_finished", "_on_load_progress", "_on_url_changed",
                 "_on_render_process_terminated", "_on_loading_changed"):
        setattr(stand_in, name, lambda *_a: None)
    return stand_in


def test_the_scrapers_cleanup_hides_its_page_so_it_can_be_discarded():
    """Qt will not discard a visible page, and the scrape's cleanup discards
    its page to free the renderer at once."""
    page = _Page()
    page.keep_visible()

    scraper_module.HeadlessScraper._cleanup(_scraper_stand_in(page))

    assert page.state == "Discarded"
    assert ("discard refused", "Discarded") not in page.events
    # Released, so nothing that arrives late - the stop's own "load
    # finished" included - makes it visible again.
    assert page._keep_visible is False
    page.loadFinished.emit(True)
    assert page.visible is False


def test_the_sign_in_checks_cleanup_hides_its_page_so_it_can_be_discarded():
    page = _Page()
    page.keep_visible()
    stand_in = type("Verifier", (), {"deleteLater": lambda self: None})()
    stand_in._page = page
    stand_in._on_load_finished = lambda *_a: None

    verify_module.SessionVerifier._cleanup(stand_in)

    assert page.state == "Discarded"
    assert ("discard refused", "Discarded") not in page.events
