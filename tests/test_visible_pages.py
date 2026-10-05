"""Pages the app reads are shown off screen and count themselves visible.

The app never puts the pages it reads on screen. A page in a view that is
never shown reports itself hidden, gets a 0x0 viewport, and - once it moves to
a new renderer process, as claude.ai's Cross-Origin-Opener-Policy makes it do
- no animation frames even when told it is visible. Chromium throttles a
hidden page hard: measured on Qt WebEngine 6.11, a zero-delay timer chain ran
11 times in 3 s instead of about 720, and requestAnimationFrame stopped after
one frame. From 5 October 2026 claude.ai's app never finished drawing under
that, and both Claude tiles and the ChatGPT tile timed out on every refresh,
on a connection that delivered the page in 0.1 s.

In a real browser the app's own readers and sign-in check, against pages that
boot through timers and animation frames, wait for content to scroll into
view, or sit behind a cross-site redirect or that header, fail hidden and read
in 2-4 s shown off screen. These tests pin the wiring with stand-ins: the
suite never starts a real page.
"""

from __future__ import annotations

import inspect

from PyQt6.QtCore import Qt

from aigauge import app as app_module
from aigauge.webview import scraper as scraper_module
from aigauge.webview import verify as verify_module
from aigauge.webview.page import OFFSCREEN_VIEWPORT, QuietWebEnginePage, show_offscreen


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


class _View:
    """Records what is done to a QWebEngineView, in order; detaching hides
    the page, as Qt does."""

    def __init__(self):
        self.calls = []
        self.page = None

    def setAttribute(self, attribute, on=True):  # noqa: N802 - Qt's name
        self.calls.append(("attribute", attribute, on))

    def resize(self, *size):
        self.calls.append(("resize", size))

    def show(self):
        self.calls.append(("show",))

    def setPage(self, page):  # noqa: N802 - Qt's name
        self.calls.append(("setPage", page))
        if page is None and self.page is not None:
            self.page.setVisible(False)
        self.page = page

    def stop(self):
        if self.page is not None:
            self.page.loadFinished.emit(False)  # Qt reports the stopped load

    def width(self):
        return OFFSCREEN_VIEWPORT[0]

    def height(self):
        return OFFSCREEN_VIEWPORT[1]

    def deleteLater(self):  # noqa: N802 - Qt's name
        pass


SIGNALS = ("loadStarted", "loadProgress", "urlChanged", "loadFinished")


def _emit(page, name):
    getattr(page, name).emit(*(() if name == "loadStarted" else (None,)))


def _shown_off_screen(view) -> bool:
    attribute = ("attribute", Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    return (
        attribute in view.calls
        and ("show",) in view.calls
        # Set after show(), the show would have been a real one.
        and view.calls.index(attribute) < view.calls.index(("show",))
        and ("resize", OFFSCREEN_VIEWPORT) in view.calls
    )


def test_a_view_is_shown_off_screen_never_on_screen():
    view = _View()

    show_offscreen(view)

    assert _shown_off_screen(view)


def test_keeping_a_page_visible_asks_for_it_at_once_and_on_every_load_signal():
    """Qt drops a request made before the page's first load - the page's
    contents do not exist yet - so it is made again on every load signal."""
    page = _Page()
    page.keep_visible()
    assert page.visible is True

    for name in SIGNALS:
        page.visible = False  # what Qt does to a request made too early
        _emit(page, name)
        assert page.visible is True, name


def test_a_released_page_goes_hidden_and_stays_hidden():
    page = _Page()
    page.keep_visible()

    page.release_visible()
    for name in SIGNALS:
        _emit(page, name)

    assert page.visible is False


def _profile():
    return type("Profile", (), {
        "persistentStoragePath": lambda self: "/tmp/profile",
        "httpUserAgent": lambda self: "UA",
    })()


def test_the_scraper_shows_its_view_off_screen_and_keeps_its_page_visible(qtbot, monkeypatch):
    pages, views = [], []

    def make_page(*args, **kwargs):
        page = _Page()
        page.scripts = lambda: None
        page.load = lambda _url: None
        pages.append(page)
        return page

    def make_view():
        view = _View()
        views.append(view)
        return view

    monkeypatch.setattr(scraper_module, "QuietWebEnginePage", make_page)
    monkeypatch.setattr(scraper_module, "QWebEngineView", make_view)
    monkeypatch.setattr(scraper_module, "get_profile", lambda _provider: _profile())

    scraper = scraper_module.HeadlessScraper("claude", "https://claude.ai/new", "1")
    try:
        page, view = pages[0], views[0]
        assert view.page is page and _shown_off_screen(view)
        assert page.visible is True
        # The request survives Qt dropping it before the first load.
        page.visible = False
        page.loadStarted.emit()
        assert page.visible is True
    finally:
        scraper._timeout.stop()
        scraper._soft_ready.stop()


def test_the_sign_in_check_gets_a_view_shown_off_screen(qtbot, monkeypatch):
    """Without a view its page got no frames after a move to a new renderer
    process - which claude.ai's Cross-Origin-Opener-Policy causes - and a
    good sign-in read as bad."""
    pages, views = [], []

    def make_page(*args, **kwargs):
        page = _Page()
        page.settings = lambda: type("S", (), {"setAttribute": lambda *_a: None})()
        page.load = lambda _url: None
        pages.append(page)
        return page

    def make_view():
        view = _View()
        views.append(view)
        return view

    monkeypatch.setattr(verify_module, "QuietWebEnginePage", make_page)
    monkeypatch.setattr(verify_module, "QWebEngineView", make_view)
    monkeypatch.setattr(verify_module, "get_profile", lambda _account: None)

    checker = verify_module.SessionVerifier("codex")
    try:
        page, view = pages[0], views[0]
        assert view.page is page and _shown_off_screen(view)
        page.visible = False
        page.loadStarted.emit()
        assert page.visible is True
    finally:
        checker._timeout.stop()


def _scraper_stand_in(page):
    view = _View()
    view.setPage(page)
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
    view = _View()
    view.setPage(page)
    stand_in = type("Verifier", (), {"deleteLater": lambda self: None})()
    stand_in._page = page
    stand_in._view = view
    stand_in._on_load_finished = lambda *_a: None

    verify_module.SessionVerifier._cleanup(stand_in)

    assert page.state == "Discarded"
    assert ("discard refused", "Discarded") not in page.events
    assert view.page is None
    assert page._keep_visible is False


def test_the_app_does_not_quit_when_an_off_screen_view_closes():
    """An off-screen view counts as a window to Qt. Were the app to quit
    when its last window closed, the end of a scrape would end the app."""
    source = inspect.getsource(app_module.main)

    assert "setQuitOnLastWindowClosed(False)" in source
    assert source.index("setQuitOnLastWindowClosed(False)") < source.index("App()")
