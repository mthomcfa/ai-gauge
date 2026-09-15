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


def _clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _result_keys_for_log(result: Any) -> str:
    """The extractor's top-level key names, bounded like app.py's."""
    if not isinstance(result, dict):
        return type(result).__name__
    keys = sorted(_clip(key, _LOG_KEY_LEN_LIMIT) for key in result)
    shown = keys[:_LOG_KEY_COUNT_LIMIT]
    if len(keys) > _LOG_KEY_COUNT_LIMIT:
        shown.append(f"... {len(keys) - _LOG_KEY_COUNT_LIMIT} more")
    return str(shown)


# A timeout is bounded by a QTimer, so a scrape cannot legitimately run for
# several times its own budget. When it reports that it did, the clock moved
# under it: the machine suspended mid-scrape. Observed on a laptop resumed
# after two days as elapsed_s=228477 against an 80 s budget. The multiple is
# deliberately wide so a merely slow machine is never misread as a resume.
RESUME_ARTIFACT_FACTOR = 3.0


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
        # Offscreen — never .show()
        self._view.resize(1280, 900)

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._finish(None, "timeout"))

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
        self._timeout.stop()
        self._timeout.start(self._timeout_ms)
        self._page.load(QUrl(self._url))

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
        if "fail" in self._last_load_status.lower() or self._last_load_error_string:
            log.warning(
                "scrape load event provider=%s status=%s url=%s error_code=%s "
                "error_domain=%s error_string=%r is_error_page=%s",
                self._provider,
                self._last_load_status,
                self._last_load_url,
                self._last_load_error_code,
                self._last_load_error_domain,
                self._last_load_error_string,
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
            QTimer.singleShot(0, lambda: self._finish(None, "page failed to load"))
            return
        # Page DOM may render asynchronously — give React a moment, then evaluate.
        QTimer.singleShot(self._wait_ms, self._run_extractor)

    def _run_extractor(self) -> None:
        if self._finished:
            return
        self._page.runJavaScript(self._extractor_js, self._on_js_result)

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

    def _on_js_result(self, result: Any) -> None:
        if self._finished:
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
            QTimer.singleShot(delay_ms, self._run_extractor)
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
                    self._last_load_error_string,
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
