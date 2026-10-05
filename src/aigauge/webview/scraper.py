from __future__ import annotations

import logging
import time
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtWebEngineWidgets import QWebEngineView

from .page import QuietWebEnginePage
from .api_capture import install_api_recorder
from .profile import get_profile

log = logging.getLogger("aigauge.scraper")


def _enum_name(value: Any) -> str:
    return str(getattr(value, "name", value))


def _call_or_empty(obj: Any, name: str) -> Any:
    func = getattr(obj, name, None)
    if not callable(func):
        return ""
    try:
        return func()
    except RuntimeError:
        return ""


def _url_to_string(value: Any) -> str:
    to_string = getattr(value, "toString", None)
    if callable(to_string):
        return str(to_string())
    return str(value or "")


def _safe_url(value: Any) -> str:
    raw = _url_to_string(value)
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0][:300]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))[:300]


# What a page is allowed to cost one log line. `document.title` is chosen by
# the page and nothing bounded it: with a 1 MB title the `scrape ok` record
# measured 11 079 134 characters and `scrape fail` 1 000 367, against a
# 512 KiB x 3 rotation - one scrape erasing the whole diagnostic history that
# the error dialog asks the user to attach.
_LOG_TITLE_LIMIT = 200
# The extractor's key names are page data too. Both numbers are app.py's
# `_LOG_KEY_LEN_LIMIT` and `_LOG_DICT_KEY_LIMIT`, by value rather than by
# import: the same shape of list on the same log ring, and this module is
# deliberately free of app imports.
_LOG_KEY_LEN_LIMIT = 60
_LOG_KEY_COUNT_LIMIT = 50
# Chromium's own `errorString`. Measured against real QtWebEngine it is a Qt
# string-table message, not the server's reason phrase - 49 characters for an
# HTTP failure, 20 for `net::ERR_UNSAFE_PORT` - so this bound costs nothing
# today. It closes the assumption rather than a hole: forcing the field to
# 500 000 characters produced a 500 353-character `scrape fail` record,
# 0.32x the whole 512 KiB x 3 rotation, and it is the largest argument
# on that record with no cap of its own.
_LOG_ERROR_LIMIT = 300


def _clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _key_text(raw_key: Any) -> str:
    """A key as a string, from a result whose keys the page chose.

    `str()` runs the key's own `__str__`, and `_clip` runs its `__bool__`
    first. app.py routes the same walk through a guard of this shape; this
    module had `_clip(key, ...)` directly, so one key that refuses to be
    printed replaced the whole `scrape ok` record with "the diagnostics
    could not be read".
    """
    try:
        return str(raw_key)
    except Exception:  # noqa: BLE001 - a log line must never raise
        return "<key>"


def _result_keys_for_log(result: Any) -> str:
    """The extractor's top-level key names, bounded like app.py's.

    Guarded end to end, like app.py's `_raw_keys_for_log`: `_key_text`
    covers a key that refuses to be printed, but the walk itself runs the
    payload's `__iter__` and a `dict` subclass can refuse that too. The
    caller catches what escapes and replaces the whole `scrape ok` record
    with "the diagnostics could not be read", so one refusing payload cost
    the diagnostics of a scrape that had worked.
    """
    try:
        if not isinstance(result, dict):
            # The class name is the payload's too, and nothing bounds a class
            # name: clipped to the same limit as a key.
            return type(result).__name__[:_LOG_KEY_LEN_LIMIT]
        keys = sorted(_key_text(key)[:_LOG_KEY_LEN_LIMIT] for key in result)
        shown = keys[:_LOG_KEY_COUNT_LIMIT]
        if len(keys) > _LOG_KEY_COUNT_LIMIT:
            shown.append(f"... {len(keys) - _LOG_KEY_COUNT_LIMIT} more")
        return str(shown)
    except Exception:  # noqa: BLE001 - a log line must never raise
        return "[]"


# A timeout is bounded by a QTimer, so a scrape cannot legitimately run for
# several times its own budget. When it reports that it did, the clock moved
# under it: the machine suspended mid-scrape. Observed on a laptop resumed
# after two days as elapsed_s=228477 against an 80 s budget. The multiple is
# deliberately wide so a merely slow machine is never misread as a resume.
RESUME_ARTIFACT_FACTOR = 3.0


def default_soft_ready_ms(wait_ms: int, timeout_ms: int) -> int:
    """When an attempt reads a page whose load has not finished.

    A third of the timeout, which leaves two thirds for the extractor to poll
    for its rows; never sooner than the ordinary post-load wait, and never past
    the timeout itself.
    """
    return max(0, min(max(wait_ms, timeout_ms // 3), timeout_ms))


# Is a real page's DOM there yet? Two strings and nothing else, so nothing a
# page controls reaches Python beyond whether its URL is http(s).
_READY_PROBE_JS = (
    "(() => { try { return [String(document.readyState), "
    "String(location.protocol) + '//']; } catch (e) { return null; } })()"
)


class HeadlessScraper(QObject):
    """Load a page in an offscreen QWebEngineView, then evaluate JS to extract data.

    Lives on the GUI thread (QtWebEngine is GUI-thread-only). The owner keeps a
    reference until `done` fires; the callback delivers either the JS result or
    an error string.
    """

    done = pyqtSignal(object, str)  # (result_or_None, error_or_empty_string)

    _RETRYABLE_ERRORS = (
        "timeout",
        "page failed to load",
        "extractor returned null",
        "extractor retry limit exceeded",
    )

    def __init__(
        self,
        provider: str,
        url: str,
        extractor_js: str,
        wait_ms: int = 4000,
        timeout_ms: int = 25000,
        capture_api: bool = False,
        max_extractor_reruns: int = 5,
        max_attempts: int = 1,
        soft_ready_ms: int | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._provider = provider
        self._url = url
        self._extractor_js = extractor_js
        self._wait_ms = wait_ms
        self._timeout_ms = timeout_ms
        self._capture_api = capture_api
        self._max_extractor_reruns = max(1, max_extractor_reruns)
        self._max_attempts = max(1, max_attempts)
        self._attempt = 0
        self._finished = False
        self._started_at = time.monotonic()
        self._last_load_status = ""
        self._last_load_url = ""
        self._last_load_error_code = ""
        self._last_load_error_domain = ""
        self._last_load_error_string = ""
        self._last_load_is_error_page = ""
        self._last_url = url
        self._url_change_count = 0
        self._max_progress = 0
        self._render_terminated = False
        self._extractor_reruns = 0
        # Has this attempt's extractor been started, by either route below?
        self._extracting = False
        # Did the last load that stopped or failed end net::ERR_ABORTED?
        # Recorded when Chromium reports it, because the deferred check in
        # _on_load_failed runs after the replacement load's "started" has
        # already overwritten the fields above.
        self._last_failure_aborted = False
        # Set by the retry when it stops a load that is still in progress: the
        # abort that stop() causes is expected, and is not this attempt's.
        self._expect_abort = False
        # How long an attempt waits for Chromium's "load finished" before it
        # reads the page anyway. A page counts as loaded only once every
        # subresource has settled, and on claude.ai on 2026-10-02 that event
        # never fired: the usage was on screen, the scrape sat at 70% until its
        # timeout, and the retry did the same.
        #
        # Opt-in (None is off), because it only suits an extractor that polls
        # until its rows are there: Claude's does. Codex's reads once, and
        # counts any "log in" text on a page without "usage limit" as signed
        # out, so reading its page early could turn a slow load into a false
        # sign-out.
        self._soft_ready_ms = (
            None
            if soft_ready_ms is None
            else max(0, min(int(soft_ready_ms), timeout_ms))
        )

        profile = get_profile(provider)
        self._page = QuietWebEnginePage(profile, self, provider=provider)
        # Must be installed before the first navigation: the recorder wraps
        # fetch/XHR at DocumentCreation, so a page already loading would have
        # issued its requests through the originals.
        self._api_capture_installed = (
            install_api_recorder(self._page) if capture_api else False
        )
        self._view = QWebEngineView()
        self._view.setPage(self._page)
        # Offscreen — never .show(). The page still has to count itself
        # visible, or Chromium throttles it until heavy apps never draw.
        self._view.resize(1280, 900)
        self._page.keep_visible()

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._finish(None, "timeout"))

        self._soft_ready = QTimer(self)
        self._soft_ready.setSingleShot(True)
        self._soft_ready.timeout.connect(self._on_soft_ready)

        self._page.loadFinished.connect(self._on_load_finished)
        self._page.loadProgress.connect(self._on_load_progress)
        self._page.urlChanged.connect(self._on_url_changed)
        self._page.renderProcessTerminated.connect(self._on_render_process_terminated)
        loading_changed = getattr(self._page, "loadingChanged", None)
        if loading_changed is not None:
            loading_changed.connect(self._on_loading_changed)

        log.info(
            "scrape start provider=%s url=%s profile=%s viewport=%sx%s "
            "wait_ms=%s timeout_ms=%s max_attempts=%s user_agent=%r",
            provider,
            _safe_url(url),
            profile.persistentStoragePath(),
            self._view.width(),
            self._view.height(),
            wait_ms,
            timeout_ms,
            self._max_attempts,
            profile.httpUserAgent(),
        )
        self._begin_attempt()

    def _begin_attempt(self) -> None:
        self._attempt += 1
        self._last_load_status = ""
        self._last_load_url = ""
        self._last_load_error_code = ""
        self._last_load_error_domain = ""
        self._last_load_error_string = ""
        self._last_load_is_error_page = ""
        self._last_url = self._url
        self._url_change_count = 0
        self._max_progress = 0
        self._render_terminated = False
        self._extractor_reruns = 0
        self._extracting = False
        self._timeout.stop()
        self._timeout.start(self._timeout_ms)
        self._soft_ready.stop()
        if self._soft_ready_ms is not None:
            self._soft_ready.start(self._soft_ready_ms)
        target = QUrl(self._url)
        if self._attempt > 1 and target.hasFragment() and self._on_committed(target):
            # Loading the URL a page already shows, fragment and all, is a
            # same-document navigation: it scrolls, and does not reload.
            # Claude's usage view is /new#settings/usage, so a retry from that
            # page would have re-read the very page that just failed. Reload
            # it instead, one event-loop turn later, once the stop() has
            # settled. Anywhere else - nothing committed yet, or a page that
            # dropped the fragment - load() is right: it fetches the document,
            # or changes the fragment, which is how Claude opens the view.
            attempt = self._attempt
            QTimer.singleShot(0, lambda: self._reload_for(attempt))
        else:
            self._page.load(target)

    def _on_committed(self, target: QUrl) -> bool:
        """Is ``target`` the page's committed entry, exactly?

        ``page.url()`` is not enough: while a document is still being fetched
        it is the URL being loaded, and a reload then has no committed entry to
        reload - the retry sent no request at all."""
        history = self._page.history()
        return history.count() > 0 and history.currentItem().url() == target

    def _reload_for(self, attempt: int) -> None:
        if self._finished or attempt != self._attempt:
            return
        self._page.triggerAction(self._page.WebAction.Reload)

    def _on_load_progress(self, progress: int) -> None:
        self._max_progress = max(self._max_progress, progress)

    def _on_url_changed(self, url: QUrl) -> None:
        safe = _safe_url(url)
        if safe and safe != _safe_url(self._last_url):
            self._last_url = safe
            self._url_change_count += 1
            log.info(
                "scrape url changed provider=%s url=%s changes=%s",
                self._provider,
                safe,
                self._url_change_count,
            )

    def _on_render_process_terminated(self, status: Any, exit_code: int) -> None:
        self._render_terminated = True
        log.warning(
            "scrape render process terminated provider=%s status=%s exit_code=%s "
            "url=%s title=%r progress=%s",
            self._provider,
            _enum_name(status),
            exit_code,
            _safe_url(self._page.url()),
            _clip(self._page.title(), _LOG_TITLE_LIMIT),
            self._max_progress,
        )

    def _on_loading_changed(self, info: Any) -> None:
        self._last_load_status = _enum_name(_call_or_empty(info, "status"))
        self._last_load_url = _safe_url(_call_or_empty(info, "url"))
        self._last_load_error_code = _call_or_empty(info, "errorCode")
        self._last_load_error_domain = _enum_name(_call_or_empty(info, "errorDomain"))
        self._last_load_error_string = str(_call_or_empty(info, "errorString") or "")
        self._last_load_is_error_page = _call_or_empty(info, "isErrorPage")
        status = self._last_load_status.lower()
        if "stopped" in status or "failed" in status:
            self._last_failure_aborted = (
                str(self._last_load_error_code).strip() == "-3"
                or self._last_load_error_string.strip() == "net::ERR_ABORTED"
            )
        if "fail" in self._last_load_status.lower() or self._last_load_error_string:
            log.warning(
                "scrape load event provider=%s status=%s url=%s error_code=%s "
                "error_domain=%s error_string=%r is_error_page=%s",
                self._provider,
                self._last_load_status,
                self._last_load_url,
                self._last_load_error_code,
                self._last_load_error_domain,
                _clip(self._last_load_error_string, _LOG_ERROR_LIMIT),
                self._last_load_is_error_page,
            )

    def _on_load_finished(self, ok: bool) -> None:
        if self._finished:
            return
        log.info(
            "scrape load finished provider=%s ok=%s url=%s title=%r progress=%s "
            "load_status=%s is_error_page=%s",
            self._provider,
            ok,
            _safe_url(self._page.url()),
            _clip(self._page.title(), _LOG_TITLE_LIMIT),
            self._max_progress,
            self._last_load_status,
            self._last_load_is_error_page,
        )
        if not ok:
            # Chromium emits loadFinished BEFORE the loadingChanged that
            # carries the actual failure (error code, domain, string,
            # isErrorPage). Finishing synchronously snapshotted those fields
            # while they were still empty, so every load failure reached the
            # log and "Copy diagnostics" reporting load_error_code=0,
            # NoErrorDomain and an empty error string - precisely the fields
            # this context exists to supply, and precisely when they matter.
            # Yield one event-loop turn so the detail lands first.
            QTimer.singleShot(0, self._on_load_failed)
            return
        if self._extracting:
            # The early read below already started; a second chain would
            # only spend the rerun budget twice as fast.
            return
        self._extracting = True
        # Page DOM may render asynchronously — give React a moment, then evaluate.
        self._schedule_extractor(self._wait_ms)

    def _load_was_superseded(self) -> bool:
        """Is this the abort of the load the retry itself stopped?

        The retry stops the attempt that timed out, and that load's
        ``net::ERR_ABORTED`` arrived during the *new* attempt and failed it
        before it had loaded anything - every Claude retry on 2026-10-02 ended
        "page failed to load" that way. That one abort is expected and waited
        out. Any other - a page calling ``window.stop()``, say - fails at once,
        as it always has.
        """
        return self._expect_abort and self._last_failure_aborted

    def _on_load_failed(self) -> None:
        if self._finished:
            return
        if self._load_was_superseded():
            self._expect_abort = False
            log.info(
                "scrape load superseded provider=%s attempt=%s; the retry "
                "stopped it, waiting for the retry's own load",
                self._provider,
                self._attempt,
            )
            return
        self._finish(None, "page failed to load")

    def _on_soft_ready(self) -> None:
        if self._finished or self._extracting:
            return
        attempt = self._attempt
        self._page.runJavaScript(
            _READY_PROBE_JS, lambda result: self._on_ready_probe(result, attempt)
        )

    def _on_ready_probe(self, result: Any, attempt: int) -> None:
        if self._finished or self._extracting or attempt != self._attempt:
            return
        # Not before a real document has been parsed: an extractor run on a
        # blank page reports a page with no usage on it, which reads as a
        # layout change rather than the slow load it is. Asked of the page
        # rather than taken from Qt's progress figure, which does not reliably
        # report a reload that follows a stalled load. Check again shortly; the
        # timeout still bounds the wait.
        ready = (
            isinstance(result, list)
            and len(result) == 2
            and result[0] in ("interactive", "complete")
            and str(result[1]).startswith(("https://", "http://"))
        )
        if not ready:
            self._soft_ready.start(1000)
            return
        self._extracting = True
        log.info(
            "scrape reading before load finished provider=%s attempt=%s "
            "progress=%s load_status=%s url=%s",
            self._provider,
            self._attempt,
            self._max_progress,
            self._last_load_status,
            _safe_url(self._page.url()),
        )
        self._run_extractor(attempt)

    def _schedule_extractor(self, delay_ms: int) -> None:
        attempt = self._attempt
        QTimer.singleShot(delay_ms, lambda: self._run_extractor(attempt))

    def _run_extractor(self, attempt: int | None = None) -> None:
        # ``attempt`` pins a run to the attempt that scheduled it. A retry
        # starts a fresh chain, and the previous attempt's pending reruns
        # would otherwise go on reading the new page alongside it.
        if self._finished or (attempt is not None and attempt != self._attempt):
            return
        self._page.runJavaScript(
            self._extractor_js, lambda result: self._on_js_result(result, attempt)
        )

    def _load_failure_context(
        self, *, error: str = "", elapsed_s: float | None = None
    ) -> dict[str, Any]:
        """Everything Chromium told us about a load that never completed.

        Deliberately excludes page content: no extractor ran, so there is none,
        and the URL goes through _safe_url so query strings and fragments (which
        can carry session material on provider auth hops) never reach the log or
        the clipboard.
        """
        context: dict[str, Any] = {
            "load_failed": True,
            "failure": error,
            "page_url": _safe_url(self._page.url()),
            "title": self._page.title(),
            "max_progress": self._max_progress,
            "load_status": self._last_load_status,
            "load_error_code": self._last_load_error_code,
            "load_error_domain": self._last_load_error_domain,
            "load_error_string": self._last_load_error_string,
            "is_error_page": self._last_load_is_error_page,
            "url_changes": self._url_change_count,
            "attempt": self._attempt,
            "render_terminated": self._render_terminated,
        }
        if elapsed_s is not None:
            context["elapsed_s"] = round(elapsed_s, 1)
        return context

    def _on_js_result(self, result: Any, attempt: int | None = None) -> None:
        if self._finished or (attempt is not None and attempt != self._attempt):
            return
        if result is None:
            self._finish(None, "extractor returned null")
            return
        if isinstance(result, dict) and "__retry_after_ms" in result:
            self._extractor_reruns += 1
            if self._extractor_reruns > self._max_extractor_reruns:
                # Hand back the last payload alongside the error. It carries the
                # page text the extractor was looking at, and discarding it made
                # a layout change undiagnosable: the snapshot reached the log and
                # "Copy diagnostics" with raw_keys=[] and nothing to inspect, so
                # the only way to see why a provider stopped parsing was to
                # rebuild the app with extra logging.
                self._finish(result, "extractor retry limit exceeded")
                return
            try:
                delay_ms = int(result.get("__retry_after_ms") or 0)
            except (TypeError, ValueError):
                delay_ms = 0
            delay_ms = max(0, min(delay_ms, 5000))
            log.info(
                "scrape extractor rerun provider=%s rerun=%s delay_ms=%s reason=%s",
                self._provider,
                self._extractor_reruns,
                delay_ms,
                result.get("__retry_reason", ""),
            )
            self._schedule_extractor(delay_ms)
            return
        self._finish(result, "")

    def _finish(self, result: Any, error: str) -> None:
        if self._finished:
            return
        elapsed = time.monotonic() - self._started_at
        # Attach what Chromium reported to EVERY failure that has no payload of
        # its own. Doing this at the call sites meant patching them one at a
        # time as each new failure mode showed up in the wild - "page failed to
        # load" was fixed while "timeout" and "extractor returned null" kept
        # arriving as raw={}, which tells the user nothing and tells us less.
        # A single chokepoint cannot be missed by a future error path.
        #
        # Keyed on "not a dict" rather than "is None": ScrapeRunner discards
        # any non-dict result and substitutes raw={}, so a future path handing
        # back a string or False would silently reproduce the empty-diagnostics
        # bug through a condition that looked like it covered everything.
        if error and not isinstance(result, dict):
            # Reading the page is a diagnostic, and a page whose C++ half Qt
            # has already deleted raises instead of answering. Losing the
            # detail is a worse log line; losing the `done` signal below
            # parks the account's live-scrape guard for the life of the
            # process, so the detail is what gives way.
            try:
                result = self._load_failure_context(error=error, elapsed_s=elapsed)
            except Exception:  # noqa: BLE001
                result = {"load_failed": True, "failure": error}
        if error == "timeout" and self._is_resume_artifact(elapsed):
            # Not the provider failing: the app was suspended mid-scrape and
            # the wall clock carried on. Labelled here so the scheduler above
            # does not count it toward that provider's error retry - every
            # resume used to cost one spurious failure per provider.
            budget = self._timeout_ms / 1000 * self._max_attempts
            log.warning(
                "scrape timeout classification=resume_artifact provider=%s "
                "elapsed_s=%.1f budget_s=%.1f",
                self._provider,
                elapsed,
                budget,
            )
            if isinstance(result, dict):
                result["classification"] = "resume_artifact"
        if error and error in self._RETRYABLE_ERRORS and self._attempt < self._max_attempts:
            log.warning(
                "scrape retry provider=%s attempt=%s/%s elapsed=%.1fs error=%s "
                "progress=%s load_status=%s",
                self._provider,
                self._attempt,
                self._max_attempts,
                elapsed,
                error,
                self._max_progress,
                self._last_load_status,
            )
            try:
                # Stopping a load that is still in progress aborts it, and
                # that abort reaches the next attempt. It is expected; one
                # that this stop did not cause is not.
                self._expect_abort = "started" in str(self._last_load_status).lower()
                self._view.stop()
                self._begin_attempt()
            except Exception:  # noqa: BLE001
                # A retry that cannot even start is a scrape that is over:
                # fall through and report it, rather than return without
                # ever emitting `done`.
                log.exception(
                    "scrape retry could not start provider=%s", self._provider
                )
            else:
                return
        self._finished = True
        # The signal is what releases the account's live-scrape guard and
        # hands the snapshot back, so it is emitted whatever the
        # diagnostics around it do: `self._page` belongs to a profile that
        # may already have been destroyed under this scrape, and a
        # RuntimeError from reading it used to take `done` with it.
        try:
            self._timeout.stop()
            self._soft_ready.stop()
            if error:
                log.warning(
                    "scrape fail provider=%s url=%s page_url=%s elapsed=%.1fs "
                    "error=%s load_status=%s load_url=%s load_error_code=%s "
                    "load_error_domain=%s load_error_string=%r load_is_error_page=%s "
                    "title=%r progress=%s url_changes=%s render_terminated=%s "
                    "attempts=%s",
                    self._provider,
                    _safe_url(self._url),
                    _safe_url(self._page.url()),
                    elapsed,
                    error,
                    self._last_load_status,
                    self._last_load_url,
                    self._last_load_error_code,
                    self._last_load_error_domain,
                    _clip(self._last_load_error_string, _LOG_ERROR_LIMIT),
                    self._last_load_is_error_page,
                    _clip(self._page.title(), _LOG_TITLE_LIMIT),
                    self._max_progress,
                    self._url_change_count,
                    self._render_terminated,
                    self._attempt,
                )
            else:
                log.info(
                    "scrape ok provider=%s elapsed=%.1fs page_url=%s load_status=%s "
                    "load_url=%s load_is_error_page=%s title=%r progress=%s "
                    "url_changes=%s render_terminated=%s attempts=%s result_keys=%s",
                    self._provider,
                    elapsed,
                    _safe_url(self._page.url()),
                    self._last_load_status,
                    self._last_load_url,
                    self._last_load_is_error_page,
                    _clip(self._page.title(), _LOG_TITLE_LIMIT),
                    self._max_progress,
                    self._url_change_count,
                    self._render_terminated,
                    self._attempt,
                    _result_keys_for_log(result),
                )
        except Exception:  # noqa: BLE001
            log.warning(
                "scrape finished but its diagnostics could not be read "
                "provider=%s error=%s",
                self._provider,
                error or "-",
            )
        finally:
            self.done.emit(result, error)
        # Release Chromium resources after connected callbacks have had a
        # chance to clear their Python references.
        QTimer.singleShot(0, self._cleanup)

    def _is_resume_artifact(self, elapsed: float) -> bool:
        budget = (self._timeout_ms / 1000) * max(1, self._max_attempts)
        return elapsed > budget * RESUME_ARTIFACT_FACTOR

    def _cleanup(self) -> None:
        try:
            self._page.loadFinished.disconnect(self._on_load_finished)
            self._page.loadProgress.disconnect(self._on_load_progress)
            self._page.urlChanged.disconnect(self._on_url_changed)
            self._page.renderProcessTerminated.disconnect(
                self._on_render_process_terminated
            )
        except (TypeError, RuntimeError):
            pass
        loading_changed = getattr(self._page, "loadingChanged", None)
        if loading_changed is not None:
            try:
                loading_changed.disconnect(self._on_loading_changed)
            except (TypeError, RuntimeError):
                pass
        self._view.stop()
        self._view.setPage(None)
        try:
            # Hidden first: Qt will not discard a visible page.
            self._page.release_visible()
            self._page.setLifecycleState(self._page.LifecycleState.Discarded)
        except RuntimeError:
            pass
        self._view.deleteLater()
        self._page.deleteLater()
        self.deleteLater()


def scrape(
    provider: str,
    url: str,
    extractor_js: str,
    on_done: Callable[[Any, str], None],
    parent: QObject | None = None,
    wait_ms: int = 4000,
    max_attempts: int = 1,
) -> HeadlessScraper:
    """Convenience wrapper. Returns the scraper so the caller can keep a reference."""
    scraper = HeadlessScraper(
        provider,
        url,
        extractor_js,
        wait_ms=wait_ms,
        max_attempts=max_attempts,
        parent=parent,
    )
    scraper.done.connect(on_done)
    return scraper
