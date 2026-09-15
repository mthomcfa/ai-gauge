from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable

from PyQt6.QtCore import QObject

from ..models import SnapshotStatus, UsageSnapshot
from ..webview.scraper import HeadlessScraper

log = logging.getLogger(__name__)


# The one class a scrape may name about itself. `throttled` is deliberately
# absent: a page must not be able to tell the scheduler to stop retrying it.
_SCRAPER_ERROR_CLASSES = ("resume_artifact",)


def _allowed_error_class(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    value = result.get("classification")
    return value if value in _SCRAPER_ERROR_CLASSES else None


# Account ids with a live `HeadlessScraper`. Module state, keyed by account
# rather than held on the provider object, because `App._build_providers()`
# runs on every settings save - a colour-only one included - and replaces
# that object with a fresh one whose runner is `None`. The App's park on an
# abandoned dispatch expires at twice the watchdog budget, so past that
# ceiling nothing refused: measured over six fake hours with a wedged scrape
# and a settings save every 400 s, nine `QWebEngineView`s were alive at once
# on the one cached `QWebEngineProfile` for that account - nine writers to
# one cookie store, which is how a spurious sign-out happens. Without the
# saves the same run started one scrape and refused eleven times.
#
# An entry normally clears the moment the scrape reports: `HeadlessScraper`
# arms its own timeout, so `done` is emitted whatever the page does, and
# `_handle` discards before it answers. That invariant lives in another
# module, so the entry carries the instant it started and the longest its
# scrape can legitimately take, and expires past it. Without the expiry a
# `_finish` that raised before its emit - a page whose C++ half Qt had
# already deleted, which is exactly what purging a profile under a live page
# produces - parked the account for the life of the process: the tile read
# "A refresh is already running." forever, and no settings save could clear
# it, because not being clearable by a rebuild is the whole point of keeping
# this in module state.
_ACTIVE_ACCOUNTS: dict[str, tuple[float, float]] = {}

# What a stale entry is allowed beyond the scrape's own worst case: enough
# that a scrape finishing right at its bound is never called dead, little
# enough that a lost `done` costs one refresh and not the process. The App
# arms its watchdog on the same figure with 20 s of slack, and parks the
# provider for twice it, so the guard outlives both the wait and the park.
_ACTIVE_GUARD_SLACK_SECONDS = 60.0
# Used only when the runner was handed a timeout that is not a usable number.
_FALLBACK_SCRAPE_BUDGET_SECONDS = 240.0


def _mark_account_busy(account_id: str, budget_seconds: float) -> None:
    _ACTIVE_ACCOUNTS[account_id] = (
        time.monotonic(),
        max(0.0, budget_seconds) + _ACTIVE_GUARD_SLACK_SECONDS,
    )


def _release_account(account_id: str) -> None:
    _ACTIVE_ACCOUNTS.pop(account_id, None)


def account_is_busy(account_id: str) -> bool:
    """Is a scrape of this account still loading a page - whoever started it?"""
    entry = _ACTIVE_ACCOUNTS.get(account_id)
    if entry is None:
        return False
    started_at, budget = entry
    age = time.monotonic() - started_at
    if age <= budget:
        return True
    # Past its own worst case every attempt the scrape could make is over, so
    # nothing of it is still holding the profile. Logged once - the entry is
    # gone after this - because reaching here means a `done` was lost, which
    # is a bug and not a routine expiry.
    _ACTIVE_ACCOUNTS.pop(account_id, None)
    log.warning(
        "live scrape guard expired account=%s age_s=%.0f", account_id, age
    )
    return False


class ScrapeRunner:
    """Drives a HeadlessScraper and feeds the payload to a builder.

    Retries the whole scrape when the builder returns an ERROR snapshot, up
    to ``build_max_attempts`` times. Holds the active scraper reference so
    the caller (a Provider) only needs to hold the runner.

    Closures over locals are used for the signal handler rather than a bound
    method on ``self`` — PyQt6's frozen Windows build was observed to drop
    bound-method temporaries on non-QObject receivers, silently breaking the
    ``done`` signal.
    """

    def __init__(
        self,
        *,
        account_id: str,
        url: str,
        extractor_js: str,
        build: Callable[[dict[str, Any]], UsageSnapshot],
        log: logging.Logger,
        wait_ms: int = 5000,
        transport_max_attempts: int = 1,
        build_max_attempts: int = 2,
        capture_api: bool = False,
        max_extractor_reruns: int = 5,
        timeout_ms: int = 25000,
        parent: QObject | None = None,
    ):
        self._account_id = account_id
        self._url = url
        self._extractor_js = extractor_js
        self._build = build
        self._log = log
        self._wait_ms = wait_ms
        self._transport_max_attempts = max(1, transport_max_attempts)
        self._build_max_attempts = max(1, build_max_attempts)
        self._capture_api = capture_api
        self._max_extractor_reruns = max_extractor_reruns
        self._timeout_ms = timeout_ms
        self._parent = parent
        self._scraper: HeadlessScraper | None = None

    def busy(self) -> bool:
        """Is a scrape of this account still loading a page?

        ``_handle`` discards the account before it answers, so this is False
        for as long as there is no live ``HeadlessScraper`` for it - and for a
        scrape that never reported at all, once its own worst case has passed
        (see `account_is_busy`). A provider asks before starting another: `webview.profile.get_profile`
        returns one cached ``QWebEngineProfile`` per account, so two live
        scrapers are two ``QWebEngineView``s writing one cookie store. The
        answer is per account, not per runner, so it survives the settings
        save that rebuilds the provider holding this runner.
        """
        return account_is_busy(self._account_id)

    def _scrape_budget_seconds(self) -> float:
        """The longest this scrape can legitimately take.

        The scraper's own timeout bounds each transport attempt, an extractor
        rerun happens inside that timeout, and the runner may rebuild the
        scraper once per build attempt - so this is the figure the browser
        providers publish as `refresh_budget_seconds` and the App arms its
        watchdog from, derived here rather than restated so the guard's expiry
        cannot drift away from the bound the scrape really enforces.
        """
        try:
            budget = (
                float(self._timeout_ms)
                / 1000.0
                * self._transport_max_attempts
                * self._build_max_attempts
            )
        except (TypeError, ValueError):
            budget = 0.0
        if not math.isfinite(budget) or budget <= 0:
            return _FALLBACK_SCRAPE_BUDGET_SECONDS
        return budget

    def run(self, on_done: Callable[[UsageSnapshot], None]) -> None:
        attempts = [0]

        def _handle(result: Any, error: str) -> None:
            self._scraper = None
            _release_account(self._account_id)
            if error or not isinstance(result, dict):
                # Keep the payload when the scraper managed to capture one. It
                # is what makes a layout change diagnosable from the log and
                # from "Copy diagnostics" instead of needing a debug rebuild.
                # error_dialog._sanitize_raw still redacts emails and truncates
                # body_text before any of it reaches the clipboard.
                snapshot = UsageSnapshot(
                    provider=self._account_id,
                    status=SnapshotStatus.ERROR,
                    error=error or "no data extracted",
                    # The scraper labels a timeout it measured across a
                    # machine suspend. Carrying the label through is what
                    # lets the scheduler tell "this provider is broken" from
                    # "this laptop was asleep".
                    #
                    # Allowlisted, because on the "extractor retry limit
                    # exceeded" branch `result` is the extractor's own return
                    # dict - a value that came out of the provider page - and
                    # error_class is a scheduler input. No shipped extractor
                    # emits `classification`, and the scheduler only ever
                    # tests it for membership in a two-element tuple, so this
                    # is not reachable today; the allowlist is what keeps it
                    # unreachable when a future extractor adds a key or a
                    # future consumer formats the value.
                    error_class=_allowed_error_class(result),
                    raw=result if isinstance(result, dict) else {},
                )
                self._log.warning(
                    "provider snapshot error provider=%s reason=%s",
                    self._account_id,
                    snapshot.error,
                )
                on_done(snapshot)
                return

            snapshot = self._build(result)
            if (
                snapshot.status == SnapshotStatus.ERROR
                and attempts[0] < self._build_max_attempts
            ):
                self._log.warning(
                    "provider transient error provider=%s attempt=%s reason=%s — retrying",
                    self._account_id,
                    attempts[0],
                    snapshot.error,
                )
                _start_scrape()
                return

            if snapshot.status == SnapshotStatus.ERROR:
                self._log.warning(
                    "provider snapshot error provider=%s reason=%s",
                    self._account_id,
                    snapshot.error,
                )
            on_done(snapshot)

        def _start_scrape() -> None:
            attempts[0] += 1
            self._scraper = HeadlessScraper(
                provider=self._account_id,
                url=self._url,
                extractor_js=self._extractor_js,
                wait_ms=self._wait_ms,
                max_attempts=self._transport_max_attempts,
                capture_api=self._capture_api,
                max_extractor_reruns=self._max_extractor_reruns,
                timeout_ms=self._timeout_ms,
                parent=self._parent,
            )
            self._scraper.done.connect(_handle)
            _mark_account_busy(self._account_id, self._scrape_budget_seconds())

        _start_scrape()
