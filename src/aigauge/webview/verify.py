from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtWebEngineCore import QWebEngineSettings

from .page import QuietWebEnginePage
from .profile import get_profile

# Load the provider's actual usage page and check for text that only renders for
# a signed-in user. If the cookie is good the page renders inline; if not it
# either redirects to /login or shows an interstitial.
VERIFY_TARGETS = {
    "claude": (
        "https://claude.ai/settings/usage",
        r"""(() => {
          const text = ((document.body && (document.body.innerText || document.body.textContent)) || '')
            .replace(/\s+/g, ' ').trim();
          // Pin the host so an open redirect to another origin cannot satisfy
          // the check, mirroring the OpenCode target below.
          const host = location.hostname.toLowerCase();
          if (host !== 'claude.ai' && !host.endsWith('.claude.ai')) return false;
          const path = location.pathname.toLowerCase();
          if (path.startsWith('/login') || path.startsWith('/auth')) return false;
          // Fast positive: the usage panel rendered. Must accept both
          // headings, like providers/claude.py - Claude renamed "Plan usage
          // limits" to "Plan usage", and a marker this file recognises but
          // the extractor does not (or vice versa) is what made sign-in
          // report success while the tile errored forever.
          if (/Plan usage|Current session|All models|Weekly/i.test(text)) return true;
          // Otherwise mirror providers/claude.py's isLoggedOut, INVERTED. This
          // question is "is the session good?", not "did the usage dialog
          // render?" - checking only for usage text coupled sign-in to the
          // usage UI, so a usage-panel change made a perfectly valid session
          // unverifiable. Same fix already applied to OpenCode below.
          if (document.querySelector('a[href*="/login"]')) return false;
          // Require some signed-in app shell so a blank or errored page is not
          // mistaken for a session.
          const lowered = text.toLowerCase();
          const shell = ['new chat', 'chats', 'projects', 'settings', 'account', 'upgrade'];
          return shell.filter(marker => lowered.includes(marker)).length >= 2;
        })()""",
    ),
    "codex": (
        "https://chatgpt.com/codex/cloud/settings/analytics#personal-usage",
        r"""(() => {
          const visibleText = el => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
          const text = visibleText(document.body);
          // Mirror providers/codex.py::_build_snapshot exactly: a Weekly card
          // plus EITHER the 5-hour Session card (old layout) OR the
          // shared-agentic markers (new weekly-only layout). Accepting a bare
          // weekly card here while the extractor demanded the markers let
          // sign-in report success and then error forever on every refresh.
          const hasWeekly = /Weekly usage limit/i.test(text) &&
            /\d+(?:\.\d+)?\s*%/.test(text);
          const hasSession = /5 hour usage limit/i.test(text);
          // Must stay in step with providers/codex.py's markers: accepting a
          // layout here that the extractor then rejects is what made sign-in
          // report success and the tile error forever. OpenAI has shipped
          // several phrasings for the shared limit.
          const sharedAgentic =
            /shared agentic usage limit|shares? the same usage limit|workspace monthly credit limit|credits remaining|usage breakdown/i
              .test(text);
          if (hasWeekly && (hasSession || sharedAgentic)) {
            return true;
          }
          // Only interactive elements are real tabs. "Personal usage" is now a
          // plain heading div; including div/span/p clicked that wrapper forever
          // and exhausted the verification budget.
          const labels = Array.from(document.querySelectorAll('button,a,[role="tab"],[role="button"]'));
          const label = labels.find(el => visibleText(el).toLowerCase() === 'personal usage');
          // `labels` is already filtered to interactive elements, so the element
          // is its own click target.
          const target = label;
          if (target) target.click();
          return false;
        })()""",
    ),
    "opencode_go": (
        "https://opencode.ai/workspace/wrk_01KX3HT8MFWCMHR2289KGPZ1RD/go",
        r"""(() => {
          const visibleText = el => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
          const text = visibleText(document.body).toLowerCase();
          // Verify the authenticated workspace shell rather than rendered usage
          // meters: a valid account with no usage rows yet was being reported as
          // signed out. Host and a non-root path are still pinned so a redirect
          // to /auth or another host cannot satisfy the check.
          //
          // Host rule mirrors config.validate_opencode_usage_url (which allows
          // opencode.ai and its subdomains, and any non-root path) — pinning
          // the exact host and a /workspace/ path here would permanently fail
          // verification for a Usage URL this app's own validator accepts.
          const host = location.hostname.toLowerCase();
          const hostOk = host === 'opencode.ai' || host.endsWith('.opencode.ai');
          const pathOk = location.pathname.length > 1;
          // Require a majority of the shell markers rather than all of them:
          // 'api keys', 'members' and 'billing' are owner/admin nav entries, so
          // demanding every marker reported ordinary members as signed out.
          const shellMarkers = ['usage', 'api keys', 'members', 'billing', 'settings'];
          const found = shellMarkers.filter(marker => text.includes(marker)).length;
          return hostOk && pathOk && found >= 3;
        })()""",
    ),
}


class SessionVerifier(QObject):
    """Loads the provider's usage page off-screen and runs a JS check.

    Emits ``done(ok, error)``:
    - ``ok=True`` — page loaded as a signed-in user.
    - ``ok=False, error=""`` — page loaded but the signed-in marker was missing
      (typical when the cookie is expired or incomplete).
    - ``ok=False, error=<reason>`` — load failure or timeout; verification was
      inconclusive rather than negative.
    """

    done = pyqtSignal(bool, str)

    def __init__(
        self,
        provider: str,
        account_id: str | None = None,
        timeout_ms: int = 20000,
        parent: QObject | None = None,
        verify_url: str | None = None,
    ):
        super().__init__(parent)
        self._finished = False

        target = VERIFY_TARGETS.get(provider)
        if target is None:
            QTimer.singleShot(0, lambda: self._finish(False, f"no verify target for {provider}"))
            return
        url, check_js = target
        if verify_url:
            url = verify_url
        self._check_js = check_js
        self._check_attempts = 0

        profile = get_profile(account_id or provider)
        self._page = QuietWebEnginePage(profile, self)
        s = self._page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._finish(False, "timeout"))
        self._timeout.start(timeout_ms)

        self._page.loadFinished.connect(self._on_load_finished)
        self._page.load(QUrl(url))

    def _on_load_finished(self, ok: bool) -> None:
        if self._finished:
            return
        if not ok:
            self._finish(False, "page failed to load")
            return
        # Give React/SSR a beat to render before the check runs.
        QTimer.singleShot(2000, self._run_check)

    def _run_check(self) -> None:
        if self._finished:
            return
        self._page.runJavaScript(self._check_js, self._on_js_result)

    def _on_js_result(self, result: Any) -> None:
        if self._finished:
            return
        if result is True:
            self._finish(True, "")
            return
        self._check_attempts += 1
        if self._check_attempts >= 12:
            self._finish(False, "")
            return
        QTimer.singleShot(1000, self._run_check)

    def _finish(self, ok: bool, error: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._timeout.stop()
        self.done.emit(ok, error)
        QTimer.singleShot(0, self._cleanup)

    def _cleanup(self) -> None:
        try:
            self._page.loadFinished.disconnect(self._on_load_finished)
        except (TypeError, RuntimeError):
            pass
        try:
            self._page.setLifecycleState(self._page.LifecycleState.Discarded)
        except RuntimeError:
            pass
        self._page.deleteLater()
        self.deleteLater()


def verify_session(
    provider: str,
    on_done: Callable[[bool, str], None],
    *,
    account_id: str | None = None,
    parent: QObject | None = None,
    verify_url: str | None = None,
) -> SessionVerifier:
    """Convenience wrapper. Returns the verifier so the caller can keep a ref."""
    verifier = SessionVerifier(
        provider,
        account_id=account_id,
        parent=parent,
        verify_url=verify_url,
    )
    verifier.done.connect(on_done)
    return verifier
