from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWebEngineCore import QWebEnginePage

log = logging.getLogger("aigauge.webview.page")

# The viewport a page read off screen is given: a desktop browser window, so
# a site lays itself out as it would for the user.
OFFSCREEN_VIEWPORT = (1280, 900)


def show_offscreen(view) -> None:
    """Show a QWebEngineView as far as Qt and its page are concerned, never on screen.

    A view that is only resized and never shown gives its page a 0x0
    viewport and no frames once the page moves to a new renderer process -
    which a cross-site redirect does, and so does a site that sends
    Cross-Origin-Opener-Policy, as claude.ai does. With no viewport nothing
    ever scrolls into view, so content waiting on an IntersectionObserver
    never appears. Qt's WA_DontShowOnScreen shows the widget without
    putting a window on screen: no window, no taskbar entry, no focus. The
    attribute has to be set before show(), or the show is a real one.

    Shown, the page draws: one that animates continuously cost about a third
    of a core in the app's process and an eighth in the renderer, measured,
    for as long as it is open - which is only until its read ends. And the
    view counts as a window to Qt, so the app must not quit when its last
    window closes (it does not: app.main).
    """
    view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    view.resize(*OFFSCREEN_VIEWPORT)
    view.show()


# JS console fragments that are pure third-party telemetry/analytics chatter
# and never useful when diagnosing AI Gauge issues. Matched as substrings.
_NOISY_CONSOLE_FRAGMENTS = (
    "Error with Permissions-Policy header: Unrecognized feature:",
    "[GSI_LOGGER]:",
    "[Intercom] The App ID in your code snippet has not been set.",
    "preloaded using link preload in Early Hints but not used",
    # claude.ai loads an isolated analytics iframe that posts a long stream of
    # info-level messages on every page load (~25 lines per scrape).
    "[IsolatedSegment]",
    # Datadog RUM init banner from claude.ai's bundle.
    "[O11Y] [DatadogRUM]",
    "DatadogRUM",
    # The embedded pages request browser features the offscreen webview does not
    # grant, log a styled anti-debug banner, and probe WebGPU / storage that the
    # throwaway profile lacks. None of it affects reading usage numbers.
    "Potential permissions policy violation:",
    "font-size:0;color:transparent",
    "Failed to create WebGPU Context Provider",
    "QuotaExceededError",
)


def _shorten(value: str, limit: int = 300) -> str:
    return value if len(value) <= limit else value[:limit] + "..."


def _safe_source_id(source_id: str) -> str:
    url = QUrl(source_id)
    if url.isValid() and url.scheme() in ("http", "https") and url.host():
        return f"{url.scheme()}://{url.host()}{url.path()}"
    return source_id


def _python_level_for(js_level: object) -> int:
    """Map a QWebEnginePage console level enum to a Python logging level.

    JS Info-level messages from the embedded pages are overwhelmingly routine
    analytics chatter, so they're routed to DEBUG (suppressed by the default
    INFO file logger). Warnings and errors still surface in the log.
    """
    name = getattr(js_level, "name", str(js_level))
    if name == "ErrorMessageLevel":
        return logging.WARNING
    if name == "WarningMessageLevel":
        return logging.INFO
    return logging.DEBUG


class QuietWebEnginePage(QWebEnginePage):
    """QWebEnginePage that suppresses noisy third-party console chatter."""

    def __init__(self, profile, parent=None, *, provider: str = "unknown"):
        super().__init__(profile, parent)
        self._diagnostic_provider = provider
        self._keep_visible = False

    def keep_visible(self) -> None:
        """Have the page count itself visible, with no window on screen.

        The app reads its pages without showing them, and a page whose view
        is never shown reports ``document.visibilityState == "hidden"``.
        Chromium throttles hidden pages hard: measured on Qt WebEngine 6.11, a
        zero-delay timer chain ran 11 times in 3 s instead of about 720, and
        requestAnimationFrame stopped after one frame. claude.ai's app, from
        5 October 2026, never finished drawing under that: 0 characters of
        text after a minute, on a connection that delivered the page in
        0.1 s. Visible, the same page draws.

        Pair it with show_offscreen on the page's view: this alone leaves a
        viewless or never-shown page with a 0x0 viewport and, after a move
        to a new renderer process, without frames.

        Asked for before the page's first load, it is dropped - the page's
        contents do not exist yet - so it is asked for again on every load
        signal rather than once, which also covers any later reset.
        """
        if not hasattr(self, "setVisible"):
            return
        self._keep_visible = True
        for signal in (self.loadStarted, self.loadProgress, self.urlChanged, self.loadFinished):
            signal.connect(self._reassert_visible)
        self._reassert_visible()

    def release_visible(self) -> None:
        """Hidden again, so the page can be discarded.

        Qt refuses to discard a visible page ("failed to transition from
        Active to Discarded state: page is visible"), and the scrape's cleanup
        discards its page to free the renderer at once.
        """
        self._keep_visible = False
        if hasattr(self, "setVisible"):
            self.setVisible(False)

    def _reassert_visible(self, *_args) -> None:
        if self._keep_visible and not self.isVisible():
            self.setVisible(True)

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):  # noqa: N802
        if any(fragment in message for fragment in _NOISY_CONSOLE_FRAGMENTS):
            return
        python_level = _python_level_for(level)
        log.log(
            python_level,
            "webengine console provider=%s level=%s source=%s line=%s message=%r",
            self._diagnostic_provider,
            getattr(level, "name", str(level)),
            _shorten(_safe_source_id(source_id or "")),
            line_number,
            _shorten(message),
        )
        super().javaScriptConsoleMessage(level, message, line_number, source_id)
