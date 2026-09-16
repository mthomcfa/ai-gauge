"""What the refresh scheduler writes to ai-gauge.log.

The scheduler used to log nothing at all: `refresh_now`, `_start_next_refresh`,
`_on_snapshot`'s success path, `_schedule_next_refresh` and the cycle outcome
were silent, so the only scheduler telemetry in a user's log was the 5-minute
heartbeat. Reading 4.5 days of a real desktop log meant reconstructing cycles
from provider lines and guessing at the rest: 137 cycles had to be inferred
from runs of provider activity separated by 45 s of quiet, and the one
question the user actually asked - "I clicked Refresh and nothing happened" -
was unanswerable, because a dropped manual refresh left no trace whatsoever.

These tests pin the lines that make the next log readable directly. They
assert the format strings, because the format strings are the interface the
log analyser parses.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import aigauge.app as app_module
from aigauge.app import App, _REST_PARK_BACKSTOP_SECONDS, app_data_dir
from aigauge.config import Config
from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot


class _FakeQTimer:
    """A QTimer stand-in that makes the cycle synchronous.

    `_advance_cycle` hops to the next provider through
    ``QTimer.singleShot(0, ...)`` and the watchdog arms a real single-shot
    timer. Running the callbacks inline keeps these tests free of an event
    loop - the same reason the rest of the suite stubs the refresh timer -
    and lets a test fire a watchdog on demand instead of waiting minutes.
    """

    armed: list["_FakeQTimer"] = []

    def __init__(self, parent=None):
        self.parent = parent
        self.single_shot = False
        self.interval_ms: int | None = None
        self.callbacks: list = []
        self.active = False
        self.deleted = False
        self.timeout = self

    def connect(self, callback):
        self.callbacks.append(callback)

    def setSingleShot(self, value):
        self.single_shot = value

    def start(self, ms=None):
        self.interval_ms = ms
        self.active = True
        _FakeQTimer.armed.append(self)

    def stop(self):
        self.active = False

    def deleteLater(self):
        self.deleted = True

    def isActive(self):
        return self.active

    def fire(self):
        self.active = False
        for callback in list(self.callbacks):
            callback()

    @staticmethod
    def singleShot(ms, callback):
        callback()


@pytest.fixture(autouse=True)
def immediate_timers(monkeypatch):
    _FakeQTimer.armed = []
    monkeypatch.setattr(app_module, "QTimer", _FakeQTimer)
    yield
    _FakeQTimer.armed = []


class _Timer:
    def __init__(self):
        self.started_ms: int | None = None
        self.active = False

    def stop(self):
        self.active = False

    def start(self, ms: int):
        self.started_ms = ms
        self.active = True

    def isActive(self):
        return self.active

    def remainingTime(self):
        return self.started_ms or 0


class _Widget:
    def __init__(self):
        self.loading_calls = []
        self.refreshing = []
        self.refresh_state_calls = []
        self.snapshots = []
        self.progress = []
        self.status_hints = []

    def set_status_hint(self, provider, text):
        self.status_hints.append((provider, text))

    def set_refreshing(self, refreshing, **kwargs):
        self.refreshing.append(refreshing)

    def set_refresh_progress(self, done, total):
        self.progress.append((done, total))

    def mark_loading(self, providers, **kwargs):
        self.loading_calls.append(providers)

    def set_refresh_state(self, *, active, minutes, next_at=None):
        self.refresh_state_calls.append(
            {"active": active, "minutes": minutes, "next_at": next_at}
        )

    def update_snapshot(self, snapshot, display_name):
        self.snapshots.append(snapshot)

    def set_ratio(self, *args, **kwargs):
        pass

    def isVisible(self):
        return True

    # What a settings save calls on its way through _on_settings_finished.
    def restore_always_on_top(self):
        pass

    def apply_gauge_colors(self):
        pass

    def apply_window_settings(self):
        pass

    def show(self):
        pass


class _Provider:
    """Calls back synchronously unless told to hold the callback."""

    uses_browser = False

    def __init__(self, snapshot: UsageSnapshot | None = None, *, hold: bool = False):
        self.snapshot = snapshot
        self.hold = hold
        self.pending = None
        self.calls = 0

    def refresh(self, on_done):
        self.calls += 1
        if self.hold:
            self.pending = on_done
            return
        on_done(self.snapshot)


class _BrowserProvider(_Provider):
    """A QtWebEngine-backed provider: these stay strictly serial."""

    uses_browser = True


def _ok(provider: str) -> UsageSnapshot:
    return UsageSnapshot(
        provider=provider,
        status=SnapshotStatus.OK,
        metrics=[UsageMetric("Session", 20.0)],
    )


def _error(provider: str, message: str = "boom") -> UsageSnapshot:
    return UsageSnapshot(
        provider=provider, status=SnapshotStatus.ERROR, error=message
    )


def _app(providers: dict[str, _Provider]) -> App:
    app = App.__new__(App)
    app._config = Config()  # noqa: SLF001
    app._providers = dict(providers)  # noqa: SLF001
    app._snapshots = {}  # noqa: SLF001
    app._inflight = set()  # noqa: SLF001
    app._refresh_queue = []  # noqa: SLF001
    app._cycle_signatures = {}  # noqa: SLF001
    app._last_cycle_signatures = None  # noqa: SLF001
    app._unchanged_cycles = 0  # noqa: SLF001
    app._error_retry = {}  # noqa: SLF001
    app._cycle_partial = False  # noqa: SLF001
    app._cycle_active = False  # noqa: SLF001
    app._cycle_started_at = None  # noqa: SLF001
    app._cycle_reason = "startup"  # noqa: SLF001
    app._cycle_statuses = {}  # noqa: SLF001
    app._cycle_names = set()  # noqa: SLF001
    app._cycle_total = 0  # noqa: SLF001
    app._dispatch_times = {}  # noqa: SLF001
    app._dispatch_epoch = {}  # noqa: SLF001
    app._dispatch_browser = {}  # noqa: SLF001
    app._abandoned = {}  # noqa: SLF001
    app._pool_wait_budgets = {}  # noqa: SLF001
    app._pending_profile_purges = []  # noqa: SLF001
    app._pending_data_clears = []  # noqa: SLF001
    app._dispatching = False  # noqa: SLF001
    app._next_refresh_reason = "startup"  # noqa: SLF001
    app._active_until = datetime.now() + timedelta(minutes=30)  # noqa: SLF001
    app._current_refresh_manual = False  # noqa: SLF001
    app._pending_manual_refresh = False  # noqa: SLF001
    app._pending_manual_asked = False  # noqa: SLF001
    app._pending_manual_providers = []  # noqa: SLF001
    app._watchdogs = {}  # noqa: SLF001
    app._timer = _Timer()  # noqa: SLF001
    app._widget = _Widget()  # noqa: SLF001
    app._started_at = datetime.now()  # noqa: SLF001
    app._ui_mode = "floating_widget"  # noqa: SLF001
    app._history = SimpleNamespace(record_snapshot=lambda snap: None)  # noqa: SLF001
    app._ratio = SimpleNamespace(  # noqa: SLF001
        record_snapshot=lambda snap: None,
        display_estimate=lambda provider: None,
        current_estimate=lambda provider: None,
    )
    app._ratio_recent = lambda provider: []  # noqa: SLF001
    app._update_tray = lambda: None  # noqa: SLF001
    app._signals = SimpleNamespace(  # noqa: SLF001
        snapshot_ready=SimpleNamespace(emit=app._on_snapshot)
    )
    return app


def _run_cycle(app: App, *, manual: bool = True) -> None:
    """Run a whole cycle. _FakeQTimer makes the provider hops synchronous."""
    app.refresh_now(manual=manual)


@pytest.fixture()
def two_providers():
    return {"claude": _Provider(_ok("claude")), "copilot": _Provider(_ok("copilot"))}


def test_a_cycle_announces_its_start(caplog, two_providers):
    app = _app(two_providers)

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        _run_cycle(app, manual=True)

    assert (
        "refresh cycle start manual=True reason=manual providers=copilot,claude"
        in caplog.text
    )


def test_a_cycle_announces_how_it_ended(caplog, two_providers):
    two_providers["claude"] = _Provider(_error("claude"))
    app = _app(two_providers)

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        _run_cycle(app, manual=True)

    line = next(
        rec.getMessage()
        for rec in caplog.records
        if rec.getMessage().startswith("refresh cycle end")
    )
    assert "changed=True" in line
    assert "errors=1" in line
    assert "auth_required=0" in line
    assert "duration_s=" in line


def test_every_provider_turn_is_timed(caplog, two_providers):
    app = _app(two_providers)

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        _run_cycle(app, manual=True)

    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        m.startswith("refresh provider start provider=claude epoch=1 queued_s=")
        for m in messages
    )
    assert any(
        m.startswith("refresh provider done provider=claude elapsed_s=")
        and m.endswith("status=ok")
        for m in messages
    )


def test_the_scheduler_says_when_it_will_wake_and_why(caplog):
    app = _app({"claude": _Provider(_ok("claude"))})
    app._snapshots = {"claude": _ok("claude")}  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app._schedule_next_refresh()  # noqa: SLF001

    line = next(
        rec.getMessage()
        for rec in caplog.records
        if rec.getMessage().startswith("refresh scheduled")
    )
    assert "reason=active" in line
    assert "active=True" in line
    assert "unchanged_cycles=0" in line
    assert "in_s=" in line


def test_a_manual_refresh_that_lands_mid_cycle_leaves_a_trace(caplog):
    """The one question 4.5 days of log could not answer.

    `refresh_now` returned early with no line at all, so "I clicked Refresh
    and nothing happened" was unprovable either way. It is queued now, and
    the queueing says so.
    """
    app = _app({"claude": _Provider(_ok("claude"), hold=True)})
    app.refresh_now(manual=False)
    assert app._inflight == {"claude"}  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app.refresh_now(manual=True)

    assert "refresh_now queued" in caplog.text
    assert "inflight=claude" in caplog.text


def test_a_scheduled_wake_that_lands_mid_cycle_is_ignored_not_queued(caplog):
    """A queued *scheduled* refresh would cascade: the cycle it landed in
    already covers every provider."""
    app = _app({"claude": _Provider(_ok("claude"), hold=True)})
    app.refresh_now(manual=False)

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app.refresh_now(manual=False)

    assert "refresh_now ignored" in caplog.text
    assert app._pending_manual_refresh is False  # noqa: SLF001


def test_a_manual_refresh_during_a_cycle_runs_when_the_cycle_ends():
    """_pending_manual_refresh was scaffolded in __init__ and never read.

    The widget's Refresh button is disabled for the whole cycle, so the
    reachable path is the tray menu's "Refresh now" - which silently did
    nothing, at precisely the moment a tile looks stale, which is usually
    mid-cycle.
    """
    claude = _Provider(_ok("claude"), hold=True)
    app = _app({"claude": claude})

    app.refresh_now(manual=False)
    app.refresh_now(manual=True)
    assert app._pending_manual_refresh is True  # noqa: SLF001

    claude.hold = False
    claude.pending(_ok("claude"))

    assert claude.calls == 2, "the queued manual refresh never ran"
    assert app._pending_manual_refresh is False  # noqa: SLF001


def test_a_queued_manual_refresh_runs_exactly_once():
    claude = _Provider(_ok("claude"), hold=True)
    app = _app({"claude": claude})

    app.refresh_now(manual=False)
    app.refresh_now(manual=True)
    app.refresh_now(manual=True)
    app.refresh_now(manual=True)

    claude.hold = False
    claude.pending(_ok("claude"))

    assert claude.calls == 2, "three clicks produced more than one refresh"


def test_refresh_provider_during_a_cycle_is_queued_for_that_provider():
    """The sign-in and cookie-paste paths call refresh_provider; both of them
    used to no-op if a cycle happened to be running."""
    claude = _Provider(_ok("claude"), hold=True)
    codex = _Provider(_ok("codex"))
    app = _app({"claude": claude, "codex": codex})

    app.refresh_now(manual=False)
    app.refresh_provider("codex")

    claude.hold = False
    claude.pending(_ok("claude"))

    assert app._pending_manual_providers == []  # noqa: SLF001
    assert codex.calls == 2, "the queued provider refresh never ran"
    assert claude.calls == 1, "a single-provider request refreshed everything"


def test_the_heartbeat_restarts_a_timer_that_died(caplog):
    """_schedule_next_refresh returns without starting the timer while
    anything is in flight. If the cycle then ends in a way that never
    reschedules, refreshes simply stop - the heartbeat is the only thing
    still running, so it is what notices."""
    app = _app({"claude": _Provider(_ok("claude"))})
    app._timer.active = False  # noqa: SLF001

    with caplog.at_level(logging.WARNING, logger="aigauge.app"):
        app._log_heartbeat()  # noqa: SLF001

    assert "refresh timer was not running" in caplog.text
    assert app._timer.active is True  # noqa: SLF001


def test_a_provider_that_never_calls_back_does_not_stall_the_cycle(caplog):
    """_inflight was cleared only by an arriving snapshot, and
    _schedule_next_refresh, refresh_now and refresh_provider all return early
    while it is non-empty. One provider that never reported back therefore
    stopped every refresh until the app was restarted.
    """
    claude = _BrowserProvider(_ok("claude"), hold=True)
    codex = _BrowserProvider(_ok("codex"))
    app = _app({"claude": claude, "codex": codex})

    with caplog.at_level(logging.WARNING, logger="aigauge.app"):
        app.refresh_now(manual=False)
        assert app._inflight == {"claude"}  # noqa: SLF001
        app._watchdogs["claude"].fire()  # noqa: SLF001

    assert "watchdog" in caplog.text
    assert app._snapshots["claude"].status == SnapshotStatus.ERROR  # noqa: SLF001
    assert "timed out" in (app._snapshots["claude"].error or "")  # noqa: SLF001
    assert codex.calls == 1, "the queue did not continue past the stuck provider"
    assert app._inflight == set()  # noqa: SLF001
    assert app._cycle_active is False  # noqa: SLF001


def test_a_provider_removed_mid_cycle_still_closes_the_cycle(caplog):
    """A settings save removes a provider that is still queued behind a
    running browser scrape.

    `_start_next_refresh` popped the dropped name and re-entered itself; when
    it was the last entry the re-entry returned on the empty-queue guard
    *without* closing the cycle. `_cycle_active` then stayed True forever:
    the timer was stopped by `_begin_cycle`, `_recover_dead_timer` early-
    returns on `_cycle_active`, `set_refreshing(False)` was never called so
    the widget's Refresh button stayed disabled, and a queued manual refresh
    was stranded. Only the tray menu could restart the app - the exact stall
    the watchdog was written to abolish.
    """
    claude = _BrowserProvider(_ok("claude"), hold=True)
    codex = _BrowserProvider(_ok("codex"), hold=True)
    app = _app({"claude": claude, "codex": codex})

    app.refresh_now(manual=False)
    assert app._refresh_queue == ["codex"]  # noqa: SLF001
    app.refresh_now(manual=True)  # the settings save's refresh, queued
    app._providers.pop("codex")  # noqa: SLF001 - what _build_providers does

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        claude.pending(_ok("claude"))

    assert "refresh cycle end" in caplog.text, "the cycle never closed"
    assert False in app._widget.refreshing, "Refresh stayed disabled"  # noqa: SLF001
    # and the queued manual refresh was not stranded
    assert claude.calls == 2
    claude.pending(_ok("claude"))
    assert app._cycle_active is False  # noqa: SLF001
    assert app._timer.active is True, "the scheduler was left with no timer"  # noqa: SLF001
    assert app._widget.refreshing[-1] is False  # noqa: SLF001


def test_a_clean_cycle_disarms_every_watchdog_it_armed():
    """Nothing pinned that a normal completion cancels the timer: making
    `_cancel_watchdog` a no-op passed the whole suite, which would leave a
    provider's watchdog to fire long after it answered."""
    providers = {
        "claude": _BrowserProvider(_ok("claude"), hold=True),
        "copilot": _Provider(_ok("copilot"), hold=True),
    }
    app = _app(providers)

    app.refresh_now(manual=False)
    armed = [timer for timer in _FakeQTimer.armed if timer.interval_ms]
    assert armed, "no watchdog was armed"

    providers["copilot"].pending(_ok("copilot"))
    providers["claude"].pending(_ok("claude"))

    assert app._watchdogs == {}  # noqa: SLF001
    assert not any(timer.active for timer in armed), "a watchdog is still armed"
    assert all(timer.deleted for timer in armed), "a watchdog was never destroyed"


def test_the_stuck_providers_late_snapshot_cannot_touch_a_second_cycle():
    """The old version of this test never started a second cycle, so it could
    not see what it was named for.

    `_on_snapshot` keyed on the provider name alone, with no dispatch
    identity. When an abandoned scrape finally answered, the *new* dispatch's
    `_inflight` entry made it look current: the stale answer was accepted, it
    cancelled the new dispatch's watchdog, and `_advance_cycle` closed the
    cycle while the new scrape was still running - a scrape live in neither
    `_inflight` nor `_watchdogs`. Each dispatch now carries an epoch, and an
    answer from an abandoned one is logged and dropped.
    """
    claude = _BrowserProvider(_ok("claude"), hold=True)
    codex = _BrowserProvider(_ok("codex"), hold=True)
    app = _app({"claude": claude, "codex": codex})

    app.refresh_now(manual=False)
    first_callback = claude.pending
    app._watchdogs["claude"].fire()  # noqa: SLF001
    # The abandoned scrape's turn is over; codex gets the browser slot.
    assert app._inflight == {"codex"}  # noqa: SLF001
    codex_watchdog = app._watchdogs["codex"]  # noqa: SLF001

    statuses = dict(app._cycle_statuses)  # noqa: SLF001

    # The abandoned scrape finally answers, mid-second-provider.
    first_callback(_ok("claude"))

    assert app._inflight == {"codex"}, "a late answer cleared someone else"  # noqa: SLF001
    assert app._watchdogs.get("codex") is codex_watchdog  # noqa: SLF001
    assert app._cycle_active is True, "a late answer closed a live cycle"  # noqa: SLF001
    assert app._cycle_statuses == statuses, "a late answer changed the verdict"  # noqa: SLF001
    # Its own tile does get the answer: claude's newest dispatch is still
    # epoch 1, so there is nothing fresher to paint over, and the alternative
    # is "Refresh timed out." on a provider that answered.
    assert app._snapshots["claude"].status == SnapshotStatus.OK  # noqa: SLF001


def test_a_provider_the_watchdog_gave_up_on_is_not_dispatched_again():
    """The watchdog gives up on the App's side only: the provider's work is
    untouched. `ClaudeProvider.refresh` rebuilds its runner unconditionally,
    so a second dispatch puts a second `QWebEngineView` on the one cached
    `QWebEngineProfile` for that account - two writers to one cookie store,
    and N times the load on the provider from one desktop app.
    """
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})

    app.refresh_now(manual=False)
    app._watchdogs["claude"].fire()  # noqa: SLF001
    assert app._cycle_active is False, "the cycle did not close"  # noqa: SLF001

    # None of the four ways back in may re-dispatch it.
    app.refresh_now(manual=False)
    app.refresh_now(manual=True)
    app.refresh_provider("claude")
    app._error_retry["claude"] = (1, datetime.now() - timedelta(seconds=1))  # noqa: SLF001
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    app._on_refresh_timer()  # noqa: SLF001

    assert claude.calls == 1, "a second scrape was started on a live profile"
    assert app._cycle_active is False, "a refused dispatch stalled the cycle"  # noqa: SLF001
    assert app._timer.active is True  # noqa: SLF001


def test_only_the_parked_dispatchs_own_answer_releases_the_park(caplog):
    """The epoch guard on the un-park, which nothing else pins.

    A REST park is normally released by its worker reporting back, so this
    line is the whole of "released by the worker, not by a timer". Without
    the epoch test, a *stale* worker's answer - epoch N, reporting long after
    the hourly backstop already let epoch N+1 go out - lifts a park that is
    bounding a dispatch still in flight, and the next cadence wake starts a
    third worker on the endpoint. That accumulation is what the backstop
    exists to stop.
    """
    caplog.set_level(logging.INFO, logger="aigauge.app")
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})
    app.refresh_now(manual=False)
    stale_answer = copilot.pending
    app._watchdogs["copilot"].fire()  # noqa: SLF001
    epoch, dead_at = app._abandoned["copilot"]  # noqa: SLF001

    # The backstop let that park go, a newer dispatch went out and wedged in
    # its turn, and only now does the first worker report.
    app._abandoned["copilot"] = (epoch + 1, dead_at)  # noqa: SLF001
    stale_answer(_ok("copilot"))

    assert "copilot" in app._abandoned, (  # noqa: SLF001
        "an older dispatch's answer released the park bounding a newer one"
    )
    assert app._abandoned["copilot"] == (epoch + 1, dead_at)  # noqa: SLF001
    assert "abandoned worker reported back" not in caplog.text

    # The current dispatch's own answer does release it.
    app._on_late_snapshot(_ok("copilot"), epoch + 1)  # noqa: SLF001

    assert "copilot" not in app._abandoned, (  # noqa: SLF001
        "the parked dispatch's own answer did not release the park"
    )
    assert "abandoned worker reported back" in caplog.text


def test_an_abandoned_worker_reporting_back_makes_its_provider_eligible():
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})
    app.refresh_now(manual=False)
    late = claude.pending
    app._watchdogs["claude"].fire()  # noqa: SLF001

    late(_ok("claude"))  # the abandoned scrape finally lets go

    app.refresh_now(manual=True)
    assert claude.calls == 2


def test_an_abandoned_worker_is_assumed_dead_after_twice_its_budget(monkeypatch):
    """A worker that never reports must not park its provider forever."""
    clock = {"t": 0.0}
    monkeypatch.setattr(
        app_module, "time", SimpleNamespace(monotonic=lambda: clock["t"])
    )
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})
    app.refresh_now(manual=False)
    watchdog = app._watchdogs["claude"]  # noqa: SLF001
    budget_s = (watchdog.interval_ms or 0) / 1000.0
    watchdog.fire()

    clock["t"] = 2 * budget_s - 1
    app.refresh_now(manual=True)
    assert claude.calls == 1, "released before the ceiling"

    clock["t"] = 2 * budget_s + 1
    app.refresh_now(manual=True)
    assert claude.calls == 2, "parked past the ceiling"


def test_a_removed_browser_account_still_parks_under_the_browser_rule(
    monkeypatch, caplog
):
    """The park rule follows the dispatch, not the current config.

    A settings save that removes an account rebuilds `_providers` while that
    account's scrape is still out, so asking `_uses_browser` at watchdog time
    answered False for a browser scrape: the account was parked for an hour
    under `ceiling=rest_backstop` instead of twice its budget. Its on-disk
    profile - which holds the session cookie - then waited 60 minutes for
    deletion rather than 10, and re-adding the same account left its tile
    refused for the rest of that hour.
    """
    caplog.set_level(logging.WARNING, logger="aigauge.app")
    clock = {"t": 0.0}
    monkeypatch.setattr(
        app_module, "time", SimpleNamespace(monotonic=lambda: clock["t"])
    )
    claude = _BrowserProvider(_ok("claude-ab12cd34"), hold=True)
    claude.refresh_budget_seconds = 240.0
    app = _app({"claude-ab12cd34": claude})
    app.refresh_now(manual=False)
    watchdog = app._watchdogs["claude-ab12cd34"]  # noqa: SLF001
    budget_s = (watchdog.interval_ms or 0) / 1000.0

    app._providers.pop("claude-ab12cd34")  # noqa: SLF001 - the settings save
    watchdog.fire()

    _epoch, dead_at = app._abandoned["claude-ab12cd34"]  # noqa: SLF001
    assert dead_at - clock["t"] == 2 * budget_s, (
        f"parked for {dead_at - clock['t']:.0f}s, not twice its {budget_s:.0f}s "
        "budget"
    )
    assert "ceiling=browser_2x" in caplog.text
    assert "ceiling=rest_backstop" not in caplog.text


def test_a_removed_rest_provider_still_parks_under_the_backstop(
    monkeypatch, caplog
):
    """The other direction: a REST dispatch keeps the hour."""
    caplog.set_level(logging.WARNING, logger="aigauge.app")
    clock = {"t": 0.0}
    monkeypatch.setattr(
        app_module, "time", SimpleNamespace(monotonic=lambda: clock["t"])
    )
    app = _app({"copilot": _Provider(_ok("copilot"), hold=True)})
    app.refresh_now(manual=False)
    watchdog = app._watchdogs["copilot"]  # noqa: SLF001

    app._providers.pop("copilot")  # noqa: SLF001
    watchdog.fire()

    _epoch, dead_at = app._abandoned["copilot"]  # noqa: SLF001
    assert dead_at - clock["t"] == _REST_PARK_BACKSTOP_SECONDS
    assert "ceiling=rest_backstop" in caplog.text
    assert "ceiling=browser_2x" not in caplog.text


def test_a_snapshot_from_outside_the_cycle_is_not_folded_into_it():
    """A cycle's accounting is its own membership. A snapshot from a provider
    this cycle never dispatched counted toward its progress, its `errors=`
    line and its `changed` verdict."""
    claude = _BrowserProvider(_ok("claude"), hold=True)
    copilot = _Provider(_ok("copilot"))
    app = _app({"claude": claude, "copilot": copilot})

    app._begin_cycle(["claude"], manual=False, reason="error_retry")  # noqa: SLF001
    progress_before = list(app._widget.progress)  # noqa: SLF001

    app._on_snapshot(_ok("copilot"))  # noqa: SLF001

    assert app._cycle_statuses == {}, "a foreign snapshot joined the cycle"  # noqa: SLF001
    assert app._cycle_signatures == {}  # noqa: SLF001
    assert app._widget.progress == progress_before  # noqa: SLF001
    # The tile still gets its number.
    assert app._snapshots["copilot"].status == SnapshotStatus.OK  # noqa: SLF001


def test_the_rest_watchdog_allows_for_thread_pool_queue_time():
    """`_arm_watchdog` starts its clock at dispatch, but a REST provider's
    work starts when a `QThreadPool` thread frees up - and this release hands
    all three REST providers to the pool at once. On a one-core host the
    third runnable waits behind the first two while its own budget is already
    running, so the watchdog would fire inside a refresh that has not
    exceeded its own bound."""
    providers = {
        "copilot": _Provider(_ok("copilot"), hold=True),
        "openrouter": _Provider(_ok("openrouter"), hold=True),
        "azure": _Provider(_ok("azure"), hold=True),
    }
    app = _app(providers)

    app.refresh_now(manual=False)

    alone = _app({"copilot": _Provider(_ok("copilot"), hold=True)})
    alone.refresh_now(manual=False)
    solo_ms = alone._watchdogs["copilot"].interval_ms  # noqa: SLF001

    assert app._watchdogs["copilot"].interval_ms > solo_ms, (  # noqa: SLF001
        "the watchdog counts no queue time at all"
    )




def test_the_watchdog_budget_follows_the_providers_own_bound():
    from aigauge.app import _refresh_budget_seconds
    from aigauge.providers.azure import AzureProvider
    from aigauge.providers.claude import ClaudeProvider
    from aigauge.providers.codex import CodexProvider
    from aigauge.providers.copilot import CopilotProvider

    # 40 s timeout x 2 transport attempts x 2 build attempts.
    assert _refresh_budget_seconds(ClaudeProvider) == 160
    # 25 s x 1 x 2.
    assert _refresh_budget_seconds(CodexProvider) == 50
    # Azure's REFRESH_DEADLINE_SECONDS bounds its *page loops* only; the
    # fixed handful of calls around them is outside it by design. The
    # watchdog has to allow for the whole refresh, so the provider reports
    # its real worst case, not the floor.
    from aigauge.providers.azure import (
        MAX_FIXED_REQUESTS_PER_REFRESH,
        REFRESH_DEADLINE_SECONDS,
        REQUEST_WORST_CASE_SECONDS,
    )

    # The per-request term is a whole *call's* worst case, not the per-socket
    # REQUEST_TIMEOUT it used to be: a timeout bounds one read, and until
    # 1.3.2+cfa.7 nothing bounded the exchange the watchdog has to allow for.
    assert _refresh_budget_seconds(AzureProvider) == (
        REFRESH_DEADLINE_SECONDS
        + (MAX_FIXED_REQUESTS_PER_REFRESH + 1) * REQUEST_WORST_CASE_SECONDS
    )
    assert _refresh_budget_seconds(AzureProvider) > REFRESH_DEADLINE_SECONDS
    # Copilot and OpenRouter now name their own bound too - three bounded
    # calls do not fit the flat 60 s default.
    from aigauge.providers.copilot import REFRESH_WORST_CASE_SECONDS as COPILOT_WORST
    from aigauge.providers.openrouter import (
        OpenRouterProvider,
        REFRESH_WORST_CASE_SECONDS as OPENROUTER_WORST,
    )

    assert _refresh_budget_seconds(CopilotProvider) == COPILOT_WORST
    assert _refresh_budget_seconds(OpenRouterProvider) == OPENROUTER_WORST
    assert COPILOT_WORST > 60 and OPENROUTER_WORST > 60


def test_a_retry_wake_refreshes_only_the_providers_that_are_due(caplog):
    """The wake a failing provider earns is its own, not everyone's.

    Under the cycle-wide retry the whole queue ran again every minute - in
    the user's log that meant re-scraping Claude (median 18.5 s, p90 48 s of
    browser time) because OpenCode was not signed in.
    """
    claude = _Provider(_error("claude"))
    codex = _Provider(_ok("codex"))
    app = _app({"claude": claude, "codex": codex})

    _run_cycle(app, manual=False)
    assert claude.calls == 1 and codex.calls == 1
    # Its retry is owed now.
    app._error_retry["claude"] = (1, datetime.now() - timedelta(seconds=1))  # noqa: SLF001
    app._next_refresh_reason = "error_retry"  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app._on_refresh_timer()  # noqa: SLF001

    assert (
        "refresh cycle start manual=False reason=error_retry providers=claude"
        in caplog.text
    )
    assert claude.calls == 2
    assert codex.calls == 1, "a healthy provider was refreshed on someone else's retry"


def test_a_retry_cycle_does_not_advance_the_idle_backoff():
    """_unchanged_cycles is about "is this app idle", and a cycle that polled
    one provider cannot answer that."""
    claude = _Provider(_error("claude"))
    codex = _Provider(_ok("codex"))
    app = _app({"claude": claude, "codex": codex})
    _run_cycle(app, manual=False)
    _run_cycle(app, manual=False)
    before = app._unchanged_cycles  # noqa: SLF001

    app._error_retry["claude"] = (1, datetime.now() - timedelta(seconds=1))  # noqa: SLF001
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    app._on_refresh_timer()  # noqa: SLF001

    assert app._unchanged_cycles == before  # noqa: SLF001


def test_a_removed_accounts_profile_is_not_deleted_under_a_live_scrape(
    monkeypatch, caplog
):
    """`purge_profile` releases the cached `QWebEngineProfile` and rmtree's
    its directory. A settings save can remove an account while its refresh is
    still out - F14's own comment names that scenario, and F14 drops the
    *snapshot* without stopping the *scrape*. Qt requires a profile to
    outlive its pages, and a page that survives its profile can flush rotated
    session cookies back into the directory that was just deleted, putting a
    removed account's live credential back on disk.
    """
    purged: list[str] = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})
    app.refresh_now(manual=False)
    assert app._inflight == {"claude"}  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app._providers.pop("claude")  # noqa: SLF001 - what _build_providers does
        app._purge_removed_profiles(["claude"])  # noqa: SLF001

    assert purged == [], "a profile was deleted under a live page"
    assert "profile purge deferred account=claude" in caplog.text

    claude.pending(_ok("claude"))

    assert purged == ["claude"], "the deferred purge never ran"
    assert app._pending_profile_purges == []  # noqa: SLF001


def test_a_profile_waiting_on_an_abandoned_scrape_is_purged_when_it_lets_go(
    monkeypatch,
):
    purged: list[str] = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})
    app.refresh_now(manual=False)
    late = claude.pending
    app._watchdogs["claude"].fire()  # noqa: SLF001

    app._providers.pop("claude")  # noqa: SLF001
    app._purge_removed_profiles(["claude"])  # noqa: SLF001
    assert purged == [], "the abandoned scrape still holds the profile"

    late(_ok("claude"))

    assert purged == ["claude"]


def test_a_wedged_cycle_is_ended_by_the_heartbeat(caplog):
    """`_recover_dead_timer` declined to act in the one state that needs it.

    A `_cycle_active` that is true with nothing in flight, nothing queued and
    no watchdog left is unrecoverable on its own: `_schedule_next_refresh`
    was never reached, so there is no timer, and the one mechanism that could
    start one opted out on `_cycle_active`. The fixed paths into that state
    are closed, but the recovery should not be the thing that cannot help.
    """
    app = _app({"claude": _Provider(_ok("claude"))})
    app._cycle_active = True  # noqa: SLF001
    app._cycle_started_at = None  # noqa: SLF001
    app._timer.active = False  # noqa: SLF001

    with caplog.at_level(logging.WARNING, logger="aigauge.app"):
        app._log_heartbeat()  # noqa: SLF001

    assert "cycle was wedged" in caplog.text
    assert app._cycle_active is False  # noqa: SLF001
    assert app._timer.active is True  # noqa: SLF001


def test_a_queued_per_provider_refresh_that_cannot_run_says_so(caplog):
    """`_ordered()` filters the queued names against `_providers`; with
    nothing left neither branch ran and nothing was logged, so a settings
    save that removed the provider a user had just asked to refresh looked
    identical to the request never having been made."""
    claude = _BrowserProvider(_ok("claude"), hold=True)
    codex = _BrowserProvider(_ok("codex"))
    app = _app({"claude": claude, "codex": codex})
    app.refresh_now(manual=False)
    app.refresh_provider("codex")
    app._providers.pop("codex")  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        claude.pending(_ok("claude"))

    assert "refresh pending manual dropped scope=codex" in caplog.text


def test_a_retry_cycle_does_not_erase_the_baseline_for_everyone_else():
    """`_last_cycle_signatures` is merged, not replaced.

    Replacing it passed the whole suite. It is what stops a partial cycle -
    one provider, on its own retry - from wiping the baseline for every
    provider it did not visit: with the baseline gone, the next *full* cycle
    reads as "changed" for all of them, re-arms the 30-minute active window
    and zeroes the idle backoff. On a provider that fails on a cadence, that
    is the backoff never engaging.
    """
    claude = _Provider(_ok("claude"))
    codex = _Provider(_ok("codex"))
    app = _app({"claude": claude, "codex": codex})

    _run_cycle(app, manual=False)  # baseline for both
    _run_cycle(app, manual=False)  # identical: the backoff starts counting
    before = app._unchanged_cycles  # noqa: SLF001
    assert before >= 1

    # A retry cycle visits claude alone.
    app._error_retry["claude"] = (1, datetime.now() - timedelta(seconds=1))  # noqa: SLF001
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    app._on_refresh_timer()  # noqa: SLF001

    # ...and then an ordinary full cycle, reporting exactly what it did before.
    app._next_refresh_reason = "idle"  # noqa: SLF001
    _run_cycle(app, manual=False)

    assert app._unchanged_cycles == before + 1, (  # noqa: SLF001
        "a one-provider retry erased the baseline for everyone else"
    )


def test_the_heartbeat_carries_the_error_retry_state(caplog):
    """`error_cycles` was computed in `_lifecycle_context` and then dropped by
    the format string, so the one counter that explains a 1-minute cadence
    never reached the file."""
    app = _app({"claude": _Provider(_ok("claude"))})

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app._log_heartbeat()  # noqa: SLF001

    line = next(
        rec.getMessage()
        for rec in caplog.records
        if rec.getMessage().startswith("heartbeat uptime_s=")
    )
    assert "error_cycles=-" in line

    app._error_retry["claude"] = (2, datetime.now())  # noqa: SLF001
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app._log_heartbeat()  # noqa: SLF001

    assert "error_cycles=claude:2" in caplog.text



def test_the_cheap_rest_providers_all_start_at_once():
    """Copilot, OpenRouter and Azure are plain HTTPS calls on a thread pool;
    only App's queue made them wait for a browser scrape. The log put cycles
    at a median of 48 s and a p90 of 79 s, almost all of it browser time, with
    the REST tiles filling behind it."""
    providers = {
        "claude": _BrowserProvider(_ok("claude"), hold=True),
        "codex": _BrowserProvider(_ok("codex"), hold=True),
        "copilot": _Provider(_ok("copilot"), hold=True),
        "openrouter": _Provider(_ok("openrouter"), hold=True),
        "azure": _Provider(_ok("azure"), hold=True),
    }
    app = _app(providers)

    app.refresh_now(manual=False)

    assert {"copilot", "openrouter", "azure"} <= app._inflight  # noqa: SLF001
    # Exactly one browser scrape is out; the other waits its turn.
    assert len(app._inflight & {"claude", "codex"}) == 1  # noqa: SLF001
    assert app._refresh_queue == ["codex"]  # noqa: SLF001


def test_browser_providers_stay_strictly_serial():
    """QtWebEngine is GUI-thread-only and each scrape holds a profile."""
    claude = _BrowserProvider(_ok("claude"), hold=True)
    codex = _BrowserProvider(_ok("codex"), hold=True)
    app = _app({"claude": claude, "codex": codex})

    app.refresh_now(manual=False)

    assert claude.calls == 1
    assert codex.calls == 0, "two browser scrapes were dispatched at once"

    claude.pending(_ok("claude"))
    assert codex.calls == 1


def test_a_rest_provider_answering_instantly_does_not_close_the_cycle_early():
    """Copilot with no PAT answers AUTH_REQUIRED synchronously, from inside
    the dispatch loop. The cycle must not end while the rest of the batch has
    not been dispatched."""
    copilot = _Provider(
        UsageSnapshot(
            provider="copilot",
            status=SnapshotStatus.AUTH_REQUIRED,
            error="no PAT",
        )
    )
    openrouter = _Provider(_ok("openrouter"), hold=True)
    app = _app({"copilot": copilot, "openrouter": openrouter})

    app.refresh_now(manual=False)

    assert app._cycle_active is True, "the cycle closed mid-dispatch"  # noqa: SLF001
    assert openrouter.calls == 1
    assert app._inflight == {"openrouter"}  # noqa: SLF001

    openrouter.pending(_ok("openrouter"))
    assert app._cycle_active is False  # noqa: SLF001


def test_the_cycle_ends_only_when_the_last_provider_reports():
    claude = _BrowserProvider(_ok("claude"), hold=True)
    azure = _Provider(_ok("azure"), hold=True)
    app = _app({"claude": claude, "azure": azure})

    app.refresh_now(manual=False)
    claude.pending(_ok("claude"))
    assert app._cycle_active is True, "closed while a REST provider was still out"

    azure.pending(_ok("azure"))
    assert app._cycle_active is False
    assert app._timer.started_ms is not None, "the next refresh was never scheduled"


def _cached_copilot() -> UsageSnapshot:
    """A cached OK snapshot shaped the way `_rerender_copilot` needs it."""
    return UsageSnapshot(
        provider="copilot",
        status=SnapshotStatus.OK,
        metrics=[UsageMetric("Premium", 20.0)],
        raw={"usageItems": []},
    )


def test_a_settings_rerender_does_not_end_a_live_dispatch(caplog):
    """Changing the Copilot quota mid-cycle is a repaint, not an answer.

    `_rerender_copilot` handed `_on_snapshot` a bare, epoch-less snapshot.
    The epoch gate only applies when an epoch is present, so the re-render
    took the live-answer path: it discarded `_inflight`, destroyed the
    watchdog, wrote a `refresh provider done ... status=ok` line for a
    dispatch that had not answered, recorded the cached value as that
    cycle's result, and could close the cycle. The real worker was then
    bounded by nothing, and its answer was dropped as late.
    """
    copilot = _Provider(_ok("copilot"), hold=True)
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"copilot": copilot, "claude": claude})
    app._snapshots["copilot"] = _cached_copilot()  # noqa: SLF001

    app.refresh_now(manual=False)
    assert app._inflight == {"copilot", "claude"}  # noqa: SLF001
    watchdog = app._watchdogs["copilot"]  # noqa: SLF001
    epoch = app._dispatch_epoch["copilot"]  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge"):
        app._rerender_copilot(1500)  # noqa: SLF001

    assert app._inflight == {"copilot", "claude"}, (  # noqa: SLF001
        "the re-render ended a live dispatch"
    )
    assert app._watchdogs.get("copilot") is watchdog, (  # noqa: SLF001
        "the dispatch lost its deadline"
    )
    assert app._dispatch_epoch["copilot"] == epoch  # noqa: SLF001
    assert app._cycle_active is True, "the re-render closed a live cycle"  # noqa: SLF001
    assert app._cycle_statuses == {}, (  # noqa: SLF001
        "a re-render was recorded as this cycle's answer"
    )
    assert "refresh provider done provider=copilot" not in caplog.text
    # It is still a repaint: the tile gets the new denominator now.
    assert app._widget.snapshots[-1].provider == "copilot"  # noqa: SLF001

    # And the dispatch it did not answer still lands.
    copilot.pending(
        UsageSnapshot(
            provider="copilot",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric("Session", 77.0)],
        )
    )
    assert app._snapshots["copilot"].metrics[0].percent_used == 77.0  # noqa: SLF001
    assert app._cycle_statuses.get("copilot") is SnapshotStatus.OK  # noqa: SLF001


def test_an_openrouter_rerender_does_not_end_a_live_dispatch():
    openrouter = _Provider(_ok("openrouter"), hold=True)
    app = _app({"openrouter": openrouter})
    app._snapshots["openrouter"] = UsageSnapshot(  # noqa: SLF001
        provider="openrouter",
        status=SnapshotStatus.OK,
        metrics=[UsageMetric("Spend", 10.0)],
        raw={"credits": {}, "key": {}, "top_models": [], "mgmt_key_configured": True},
    )

    app.refresh_now(manual=False)
    watchdog = app._watchdogs["openrouter"]  # noqa: SLF001

    app._rerender_openrouter(5.0)  # noqa: SLF001

    assert app._inflight == {"openrouter"}  # noqa: SLF001
    assert app._watchdogs.get("openrouter") is watchdog  # noqa: SLF001
    assert app._cycle_active is True  # noqa: SLF001


def test_a_quota_change_during_a_refresh_does_not_start_a_second_worker():
    """The settings save re-renders and then calls `refresh_now(manual=True)`.

    With the re-render read as the dispatch's answer the cycle closed, so
    that `refresh_now` found nothing in flight and dispatched a second
    worker beside the first: two live REST workers for one provider.
    """
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})
    app._snapshots["copilot"] = _cached_copilot()  # noqa: SLF001

    app.refresh_now(manual=False)
    app._rerender_copilot(1500)  # noqa: SLF001
    app.refresh_now(manual=True)  # what _on_settings_finished does next

    assert copilot.calls == 1, "a second worker was started beside the first"
    assert app._pending_manual_refresh is True, (  # noqa: SLF001
        "the manual refresh was neither run nor queued"
    )


def test_an_epochless_snapshot_for_a_live_dispatch_only_repaints():
    """Belt and braces for any future caller that reaches `_on_snapshot`
    without an epoch while that provider's dispatch is still out."""
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})

    app.refresh_now(manual=False)
    watchdog = app._watchdogs["copilot"]  # noqa: SLF001

    app._on_snapshot(_ok("copilot"))  # noqa: SLF001

    assert app._inflight == {"copilot"}  # noqa: SLF001
    assert app._watchdogs.get("copilot") is watchdog  # noqa: SLF001
    assert app._cycle_active is True  # noqa: SLF001


def test_the_heartbeat_leaves_a_healthy_cycle_alone():
    """The wedge recovery is safe only because of the order of its guards.

    A healthy cycle passes twice through "nothing in flight, no watchdog
    left, cycle still open" - between two browser dispatches - and only the
    `_refresh_queue` test in the first guard tells that apart from a wedge.
    Moving that test out of the first guard passes every other test in this
    suite, and turns the heartbeat into a thing that abandons the second half
    of every cycle whose browser queue is between dispatches. The only
    symptom would be providers quietly not refreshing.
    """
    claude = _BrowserProvider(_ok("claude"), hold=True)
    codex = _BrowserProvider(_ok("codex"), hold=True)
    app = _app({"claude": claude, "codex": codex})

    app.refresh_now(manual=False)
    assert app._inflight == {"claude"}  # noqa: SLF001
    assert app._refresh_queue == ["codex"]  # noqa: SLF001
    # Hold the queue where a real cycle holds it: `_advance_cycle` hands the
    # next provider to `QTimer.singleShot`, so between the two there is a
    # moment with an empty `_inflight` and no watchdog at all.
    app._start_next_refresh = lambda: None  # noqa: SLF001
    claude.pending(_ok("claude"))
    assert app._inflight == set()  # noqa: SLF001
    assert app._watchdogs == {}  # noqa: SLF001
    assert app._refresh_queue == ["codex"]  # noqa: SLF001
    assert app._cycle_active is True  # noqa: SLF001

    app._log_heartbeat()  # noqa: SLF001

    assert app._cycle_active is True, "a healthy cycle was declared wedged"  # noqa: SLF001
    assert app._refresh_queue == ["codex"], "the queued provider was dropped"  # noqa: SLF001


def test_the_heartbeat_still_ends_a_cycle_with_nothing_left_to_run():
    """The positive half, beside the negative one: an empty queue *and* an
    empty `_inflight` *and* no watchdog is the state that cannot recover on
    its own, because `_schedule_next_refresh` was never reached."""
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})

    app.refresh_now(manual=False)
    app._inflight.clear()  # noqa: SLF001 - a cycle that lost its dispatch
    app._watchdogs.clear()  # noqa: SLF001
    assert app._cycle_active is True  # noqa: SLF001

    app._log_heartbeat()  # noqa: SLF001

    assert app._cycle_active is False, "the wedged cycle was left open"  # noqa: SLF001


def test_a_late_answer_from_a_slow_provider_repaints_its_tile():
    """Dropping a late answer whole mislabels the provider it hits most.

    The drop is right for the accounting - that dispatch is over, and its
    cycle has closed. It is wrong for the tile: a provider that is merely
    slower than its budget answered correctly, and the tile kept "Refresh
    timed out." forever while its error streak survived into a retry it did
    not need.
    """
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})

    app.refresh_now(manual=False)
    late = copilot.pending
    app._watchdogs["copilot"].fire()  # noqa: SLF001
    assert app._snapshots["copilot"].error == "Refresh timed out."  # noqa: SLF001
    assert app._error_retry["copilot"][0] == 1  # noqa: SLF001
    statuses = dict(app._cycle_statuses)  # noqa: SLF001
    ratios: list[str] = []
    app._widget.set_ratio = lambda name, *a, **k: ratios.append(name)  # noqa: SLF001

    late(
        UsageSnapshot(
            provider="copilot",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric("Session", 77.0)],
        )
    )

    assert app._snapshots["copilot"].status is SnapshotStatus.OK  # noqa: SLF001
    assert app._snapshots["copilot"].metrics[0].percent_used == 77.0  # noqa: SLF001
    assert "copilot" not in app._error_retry, (  # noqa: SLF001
        "a provider that answered kept the streak its watchdog gave it"
    )
    assert app._cycle_statuses == statuses, "a late answer joined a cycle"  # noqa: SLF001
    assert app._cycle_active is False  # noqa: SLF001
    # `set_snapshot` hides the burn-rate row on anything but OK, so a repaint
    # back to OK has to ask for it again.
    assert ratios == ["copilot"], "the burn-rate row stayed hidden"


def test_a_skipped_provider_leaves_the_cycles_books_straight():
    """A provider dropped from the queue mid-cycle is not part of the cycle.

    Leaving it in `_cycle_names` and in `_cycle_total` makes the header count
    toward a denominator that never arrives and puts a name in the verdict
    set that never answered.
    """
    claude = _BrowserProvider(_ok("claude"), hold=True)
    codex = _BrowserProvider(_ok("codex"))
    app = _app({"claude": claude, "codex": codex})

    app.refresh_now(manual=False)
    assert app._cycle_total == 2  # noqa: SLF001
    assert app._cycle_names == {"claude", "codex"}  # noqa: SLF001

    # A settings save removes codex while it is queued behind claude.
    app._providers.pop("codex")  # noqa: SLF001
    claude.pending(_ok("claude"))

    assert codex.calls == 0, "a removed provider was dispatched"
    assert "codex" not in app._cycle_names, (  # noqa: SLF001
        "a provider that never ran is in the cycle's verdict set"
    )
    assert app._cycle_total == 1, "the header counts toward a name that skipped"  # noqa: SLF001
    assert app._cycle_active is False, "the cycle never closed"  # noqa: SLF001


def test_a_later_cycles_pool_budgets_replace_the_earlier_ones():
    """`_pool_wait_budgets` is what a dispatch may spend waiting for a thread.

    Accumulating it instead of replacing it inflates every later watchdog
    with the budgets of providers that are not in this cycle at all - a
    one-provider retry would be given the whole previous cycle's slack, and a
    watchdog that fires late is a watchdog that does not bound anything.
    """
    copilot = _Provider(_ok("copilot"), hold=True)
    openrouter = _Provider(_ok("openrouter"), hold=True)
    azure = _Provider(_ok("azure"), hold=True)
    app = _app({"copilot": copilot, "openrouter": openrouter, "azure": azure})

    app.refresh_now(manual=False)
    assert set(app._pool_wait_budgets) == {"copilot", "openrouter", "azure"}  # noqa: SLF001
    crowded = app._watchdogs["copilot"].interval_ms  # noqa: SLF001
    for provider in (copilot, openrouter, azure):
        provider.pending(_ok(provider.snapshot.provider))

    app.refresh_provider("copilot")

    assert app._pool_wait_budgets == {"copilot": 60.0}, (  # noqa: SLF001
        "a cycle inherited the budgets of the one before it"
    )
    # 60 s of REST budget plus 20 s of watchdog slack, and nothing ahead of
    # it in the pool.
    assert app._watchdogs["copilot"].interval_ms == 80_000  # noqa: SLF001
    assert crowded > 80_000, "the first cycle's slack was not measured at all"


def test_a_repaint_updates_the_tray():
    """Per snapshot, not per cycle: the tray dot and its tooltip used to be a
    whole cycle behind the tiles, which is minutes on a failing cycle. A
    repaint is a snapshot reaching a tile, so it is a tray update too."""
    copilot = _Provider(_ok("copilot"))
    app = _app({"copilot": copilot})
    app.refresh_now(manual=False)
    tray: list[int] = []
    app._update_tray = lambda: tray.append(1)  # noqa: SLF001

    app._repaint_snapshot(app._snapshots["copilot"])  # noqa: SLF001

    assert tray == [1], "the tray stayed a cycle behind the tile it just painted"


def test_a_late_answer_is_recorded_and_not_only_painted():
    """A slow-but-healthy provider had a healthy tile and no history at all.

    The only thing either store heard about that dispatch was the watchdog's
    synthetic `Refresh timed out.`, and both drop anything that is not OK. So
    the tile showed the right number while "Open ratio history" stayed empty
    and the burn-rate row never populated - for as long as the provider stayed
    slower than its budget, which for a REST provider on a one-core host is
    the six-minute window the pool slack deliberately makes generous.
    """
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})
    history: list[UsageSnapshot] = []
    recorded: list[UsageSnapshot] = []
    app._history = SimpleNamespace(record_snapshot=history.append)  # noqa: SLF001
    app._ratio = SimpleNamespace(  # noqa: SLF001
        record_snapshot=recorded.append,
        display_estimate=lambda provider: None,
        current_estimate=lambda provider: None,
    )

    for _ in range(12):
        app.refresh_now(manual=False)
        late = copilot.pending
        app._watchdogs["copilot"].fire()  # noqa: SLF001
        app._abandoned.clear()  # noqa: SLF001 - the assumed-dead ceiling passes
        late(
            UsageSnapshot(
                provider="copilot",
                status=SnapshotStatus.OK,
                metrics=[UsageMetric("Session", 41.0)],
            )
        )

    assert copilot.calls == 12
    # The real stores drop anything that is not OK (the watchdog's synthetic
    # ERROR reaches them and goes nowhere), so the OK rows are the measure.
    ok_rows = [
        snap.metrics[0].percent_used
        for snap in history
        if snap.status is SnapshotStatus.OK
    ]
    assert ok_rows == [41.0] * 12, (
        "twelve late-but-healthy cycles left the ratio history empty"
    )
    assert len([s for s in recorded if s.status is SnapshotStatus.OK]) == 12, (
        "the burn-rate estimator was never fed"
    )
    # Recorded, and still no scheduling side effect: the cycle's verdict is
    # what the watchdog gave it, and nothing is left in flight or armed.
    assert app._cycle_statuses == {"copilot": SnapshotStatus.ERROR}  # noqa: SLF001
    assert app._inflight == set() and app._watchdogs == {}  # noqa: SLF001


def test_a_settings_rerender_is_not_recorded_a_second_time():
    """The other caller of the same repaint means the opposite: that payload
    is an observation already recorded, re-rendered against a new
    denominator, and recording it again moves an average nothing new
    happened to."""
    copilot = _Provider(_ok("copilot"))
    app = _app({"copilot": copilot})
    app.refresh_now(manual=False)
    history: list[UsageSnapshot] = []
    recorded: list[UsageSnapshot] = []
    app._history = SimpleNamespace(record_snapshot=history.append)  # noqa: SLF001
    app._ratio = SimpleNamespace(  # noqa: SLF001
        record_snapshot=recorded.append,
        display_estimate=lambda provider: None,
        current_estimate=lambda provider: None,
    )

    app._repaint_snapshot(app._snapshots["copilot"])  # noqa: SLF001

    assert history == [] and recorded == [], (
        "a re-render moved an average nothing new happened to"
    )


def test_a_late_auth_required_is_still_shown():
    """The one status that tells the user to sign in again was never
    painted when it arrived past the watchdog."""
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})

    app.refresh_now(manual=False)
    late = copilot.pending
    app._watchdogs["copilot"].fire()  # noqa: SLF001

    late(
        UsageSnapshot(
            provider="copilot",
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Sign in to GitHub again.",
        )
    )

    assert app._snapshots["copilot"].status is SnapshotStatus.AUTH_REQUIRED  # noqa: SLF001
    assert app._snapshots["copilot"].error == "Sign in to GitHub again."  # noqa: SLF001
    assert "copilot" not in app._error_retry  # noqa: SLF001


def test_a_late_error_keeps_the_streak_the_watchdog_earned():
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})

    app.refresh_now(manual=False)
    late = copilot.pending
    app._watchdogs["copilot"].fire()  # noqa: SLF001
    owed = app._error_retry["copilot"]  # noqa: SLF001

    late(_error("copilot", "still broken"))

    assert app._snapshots["copilot"].error == "still broken"  # noqa: SLF001
    assert app._error_retry["copilot"] == owed, (  # noqa: SLF001
        "a late failure cleared the retry a failure had earned"
    )


def test_a_late_answer_from_an_older_epoch_repaints_nothing():
    """Only the newest dispatch's answer may reach the tile. An older one
    would paint over a dispatch that is still out, and the newer answer would
    then be overwritten by data older than itself."""
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})

    app.refresh_now(manual=False)
    first = claude.pending
    app._watchdogs["claude"].fire()  # noqa: SLF001
    app._abandoned.clear()  # noqa: SLF001 - the assumed-dead ceiling passes
    app.refresh_now(manual=True)
    assert app._dispatch_epoch["claude"] == 2  # noqa: SLF001
    watchdog = app._watchdogs["claude"]  # noqa: SLF001
    tile = app._snapshots["claude"]  # noqa: SLF001

    first(_ok("claude"))  # epoch 1, at last

    assert app._inflight == {"claude"}, "an older epoch cleared a live dispatch"  # noqa: SLF001
    assert app._watchdogs.get("claude") is watchdog, "it destroyed the watchdog"  # noqa: SLF001
    assert app._snapshots["claude"] is tile, "it painted over a newer dispatch"  # noqa: SLF001
    assert app._cycle_active is True  # noqa: SLF001


def test_a_late_answer_for_a_removed_provider_is_still_dropped():
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})

    app.refresh_now(manual=False)
    late = copilot.pending
    app._watchdogs["copilot"].fire()  # noqa: SLF001
    app._providers.pop("copilot")  # noqa: SLF001 - what _build_providers does
    app._snapshots.pop("copilot")  # noqa: SLF001

    late(_ok("copilot"))

    assert "copilot" not in app._snapshots, "a removed provider's tile came back"  # noqa: SLF001


def test_a_deferred_profile_purge_survives_a_quit(monkeypatch):
    """The deferral list was in memory only.

    `App` has no `aboutToQuit` hook that flushes it, and once the account is
    gone from `config.json` nothing at the next start looks for its
    directory: the only sweep of `profiles/` on disk is the manual Settings
    "Clear all browser data". What survives is the account's Chromium
    profile, which uses `ForcePersistentCookies` - the live session cookie
    itself - with no recovery path at all. The keyring secret is still
    cleared at the moment of removal; that half was always right.
    """
    from aigauge.config import Config as RealConfig

    purged: list[str] = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    claude = _BrowserProvider(_ok("claude-ab12cd34"), hold=True)
    app = _app({"claude-ab12cd34": claude})
    app.refresh_now(manual=False)

    app._providers.pop("claude-ab12cd34")  # noqa: SLF001
    app._purge_removed_profiles(["claude-ab12cd34"])  # noqa: SLF001
    assert purged == [], "the live scrape still holds the profile"

    # The user quits here. Whatever is still owed must be on disk.
    saved = RealConfig.load()
    assert saved.pending_profile_purges == ["claude-ab12cd34"]

    # Next start, before any cookie is hydrated and before any provider runs.
    fresh = _app({})
    fresh._config = saved  # noqa: SLF001
    fresh._drain_pending_purges()  # noqa: SLF001

    assert purged == ["claude-ab12cd34"], "the purge was lost across the quit"
    assert RealConfig.load().pending_profile_purges == [], (
        "a purge that ran is still recorded as owed"
    )


def test_a_deferred_browser_data_clear_survives_a_quit(monkeypatch, caplog):
    """"Clear all browser data" promises the saved credential is gone.

    The keyring copy goes at the click, so nothing in the UI will ever
    mention the profile again - and the profile a live scrape defers is a
    QtWebEngine directory with `ForcePersistentCookies`, i.e. the live
    session cookie itself. Held in memory only, a quit inside the deferral
    window left it on disk and clicking the button a second time was the
    only thing that reached it.

    Its drain has no configured-account skip: these ids belong to accounts
    the user still has, which is exactly what the removal list's skip is for,
    so routing them there would drop every deferred clear at the next start.
    """
    from aigauge.config import BrowserAccount, Config as RealConfig

    purged: list[str] = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    claude = _BrowserProvider(_ok("claude-ab12cd34"), hold=True)
    app = _app({"claude-ab12cd34": claude})
    app._config = RealConfig(  # noqa: SLF001
        browser_accounts=[BrowserAccount(id="claude-ab12cd34", kind="claude")]
    )
    app.refresh_now(manual=False)

    app._on_browser_data_clear_requested(["claude-ab12cd34"])  # noqa: SLF001
    assert purged == [], "a profile was deleted under a live scrape"

    # The user quits here. Whatever is still owed must be on disk.
    saved = RealConfig.load()
    assert saved.pending_data_clears == ["claude-ab12cd34"]
    assert saved.pending_profile_purges == []

    # Next start, before any cookie is hydrated and before any provider runs.
    fresh = _app({})
    fresh._config = saved  # noqa: SLF001
    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        fresh._drain_pending_purges()  # noqa: SLF001

    assert purged == ["claude-ab12cd34"], "the clear was lost across the quit"
    assert "reason=reconfigured" not in caplog.text, (
        "the clear drain skipped an account the user still has"
    )
    assert RealConfig.load().pending_data_clears == [], (
        "a clear that ran is still recorded as owed"
    )


def test_the_drain_does_not_purge_an_account_that_is_configured_again(
    monkeypatch, caplog
):
    """The drain reasons about ordering and not about membership.

    The list is persisted now, so an entry can outlive the removal that wrote
    it: a restored backup, a synced config directory or a hand-edited undo
    puts the same id in `pending_profile_purges` and in `browser_accounts`,
    and the next start deleted the live session cookie store of an account
    the user still has - no confirmation, and a tile that asks them to sign
    in again with nothing to explain why.
    """
    from aigauge.config import BrowserAccount, Config as RealConfig

    purged: list[str] = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    app = _app({})
    app._config = RealConfig(  # noqa: SLF001
        browser_accounts=[BrowserAccount(id="claude-ab12cd34", kind="claude")],
        pending_profile_purges=["claude-ab12cd34", "codex-99999999"],
    )

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app._drain_pending_purges()  # noqa: SLF001

    assert purged == ["codex-99999999"], "a configured account's profile was deleted"
    assert "purge skipped account=claude-ab12cd34 reason=reconfigured" in caplog.text
    assert app._pending_profile_purges == []  # noqa: SLF001
    assert app._config.pending_profile_purges == [], (  # noqa: SLF001
        "a skipped id stayed on the list and would be asked again every start"
    )


def test_the_startup_drain_line_is_bounded_by_a_hostile_config(monkeypatch, caplog):
    """`pending_profile_purges` is config-controlled and nothing bounds it.

    Echoing the list verbatim let a poisoned `config.json` write 800 KB of
    records at every start - one line 0.76x the whole 512 KiB rotation -
    which erases the diagnostics a compromise would otherwise leave behind.
    The line names a count and a bounded sample instead.
    """
    from aigauge.config import Config as RealConfig
    from aigauge.webview.profile import purge_profile

    hostile = (
        ["../../OUTSIDE", "a" * 200_000, "claude\x00/../../OUTSIDE", "%2e%2e%2fx"]
        + [f"codex-{index:08d}" for index in range(4996)]
    )
    app = _app({})
    app._config = RealConfig(pending_profile_purges=hostile)  # noqa: SLF001
    # The validator now drops the 200 000-character entry and caps the list,
    # which is defence in depth ahead of this line rather than instead of it:
    # what survives is still config-controlled, and the traversal payloads
    # are still in it.
    kept = app._config.pending_profile_purges  # noqa: SLF001
    assert len(kept) == 64
    assert "../../OUTSIDE" in kept and "%2e%2e%2fx" in kept
    assert "a" * 200_000 not in kept
    outside = app_data_dir().parent / "treasure.txt"
    outside.write_text("decoy")

    with caplog.at_level(logging.INFO, logger="aigauge"):
        # The real purge, so the refusal lines are the real ones too.
        monkeypatch.setattr(app_module, "purge_profile", purge_profile)
        app._drain_pending_purges()  # noqa: SLF001

    opening = next(
        rec.getMessage()
        for rec in caplog.records
        if rec.getMessage().startswith("profile purge owed")
    )
    assert "count=64" in opening
    assert len(opening) < 500, "the whole list reached the log"
    refusals = [
        rec.getMessage()
        for rec in caplog.records
        if "refusing unsafe account id" in rec.getMessage()
    ]
    assert refusals, "the traversal payloads were never actually refused"
    assert max(len(rec.getMessage()) for rec in caplog.records) < 500
    assert outside.read_text() == "decoy", "the purge escaped the profiles root"
    assert app._config.pending_profile_purges == []  # noqa: SLF001


def test_a_purge_that_runs_is_taken_off_the_pending_list(monkeypatch):
    from aigauge.config import Config as RealConfig

    purged: list[str] = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    app = _app({})

    app._purge_removed_profiles(["codex-99999999"])  # noqa: SLF001

    assert purged == ["codex-99999999"]
    assert app._pending_profile_purges == []  # noqa: SLF001
    assert RealConfig.load().pending_profile_purges == []


def test_the_pending_purges_run_before_any_provider_is_built():
    """Order matters: a provider built first can start a scrape on the very
    profile that is owed a deletion, and a cookie hydrated first re-creates
    the directory the purge is about to delete - putting a removed account's
    live session credential back on disk."""
    import inspect

    source = inspect.getsource(App.__init__)
    assert "_drain_pending_purges" in source
    assert source.index("_drain_pending_purges") < source.index(
        "self._build_providers()"
    ), "a provider could be scraping the profile that is owed a deletion"
    assert source.index("_drain_pending_purges") < source.index(
        "hydrate_all_from_keyring"
    ), "a cookie was hydrated into a profile that is owed a deletion"


def test_a_retry_due_on_a_parked_provider_is_kept_for_when_the_park_lifts(
    monkeypatch, caplog
):
    """The due was spent before the cycle filtered, so a provider that was
    due *and* parked lost its retry entirely: the streak stayed, the deadline
    became None, and nothing ran until the next full cadence cycle. When it
    was the only due name the wake also bought a complete empty cycle - the
    timer stopped, `set_refreshing(True, total=0)` and `mark_loading({})`
    reached the widget, and `refresh cycle start ... providers=` was logged
    for nothing.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(
        app_module, "time", SimpleNamespace(monotonic=lambda: clock["t"])
    )
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})

    app.refresh_now(manual=False)
    watchdog = app._watchdogs["claude"]  # noqa: SLF001
    budget_s = (watchdog.interval_ms or 0) / 1000.0
    watchdog.fire()
    assert "claude" in app._abandoned  # noqa: SLF001

    app._error_retry["claude"] = (1, datetime.now() - timedelta(seconds=1))  # noqa: SLF001
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    cycles = len(app._widget.loading_calls)  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge"):
        caplog.clear()
        app._on_refresh_timer()  # noqa: SLF001

    assert claude.calls == 1, "a parked provider was dispatched"
    assert len(app._widget.loading_calls) == cycles, "an empty cycle ran"  # noqa: SLF001
    assert "refresh cycle start" not in caplog.text
    assert "refresh retry deferred provider=claude reason=abandoned" in caplog.text
    errors, due = app._error_retry["claude"]  # noqa: SLF001
    assert errors == 1, "the streak was lost"
    assert due is not None, "the due was spent on a provider that could not run"
    # Kept, and never owed earlier than the next cadence wake: a due armed
    # for the instant the park lifts is one extra dispatch per hour for a
    # provider that is hung, and for a REST one - which has no re-entrancy
    # guard of its own - one more worker holding a slot of the global thread
    # pool. Never in the past either, which would pin every later wake at the
    # timer's 1 000 ms floor.
    lifts_in = 2 * budget_s
    cadence_at, _reason, _minutes = app._cadence_refresh_time(datetime.now())  # noqa: SLF001
    assert due >= datetime.now() + timedelta(seconds=lifts_in - 1), (
        "owed before the park it is waiting on lifts"
    )
    assert due >= cadence_at - timedelta(seconds=1), (
        "the retry bought a wake ahead of the cadence"
    )
    assert app._timer.active is True, "the scheduler was left with no timer"  # noqa: SLF001
    assert app._timer.started_ms >= (cadence_at - datetime.now()).total_seconds() * 1000 - 2000  # noqa: SLF001

    # The park lifts, the wall clock reaches the deadline it kept, and the
    # retry is what runs.
    clock["t"] = lifts_in + 1
    app._error_retry["claude"] = (errors, datetime.now())  # noqa: SLF001
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    app._on_refresh_timer()  # noqa: SLF001

    assert claude.calls == 2, "the retry it kept never ran"


def test_a_kept_retry_rides_out_an_hour_long_rest_park(monkeypatch, caplog):
    """The same kept due, against the park a wedged REST worker now earns.

    A REST park lasts until its worker reports or an hour passes, so the due
    it keeps has to survive several cadence wakes without buying a wake of
    its own and without spinning: the wake it would have armed is an hour
    out, the cadence is five minutes, and every one of those five-minute
    wakes must refuse the parked provider and go back to sleep.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(
        app_module, "time", SimpleNamespace(monotonic=lambda: clock["t"])
    )
    copilot = _Provider(_ok("copilot"), hold=True)
    openrouter = _Provider(_ok("openrouter"))
    app = _app({"copilot": copilot, "openrouter": openrouter})
    app._config.refresh_interval_minutes = 5  # noqa: SLF001
    app._config.active_refresh_interval_minutes = 5  # noqa: SLF001

    app.refresh_now(manual=False)
    app._watchdogs["copilot"].fire()  # noqa: SLF001
    _epoch, assumed_dead_at = app._abandoned["copilot"]  # noqa: SLF001
    assert assumed_dead_at - clock["t"] == 3600.0, "a REST park is the backstop"

    cadence_ms = 5 * 60 * 1000
    app._error_retry["copilot"] = (1, datetime.now() - timedelta(seconds=1))  # noqa: SLF001
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        caplog.clear()
        app._on_refresh_timer()  # noqa: SLF001

    assert "refresh retry deferred provider=copilot reason=abandoned" in caplog.text
    assert app._timer.started_ms <= cadence_ms + 2000, (  # noqa: SLF001
        "the kept due armed a wake of its own an hour out"
    )

    dispatches = copilot.calls
    wakes = 0
    while clock["t"] + 300.0 < 3600.0:
        clock["t"] += 300.0
        wakes += 1
        with caplog.at_level(logging.INFO, logger="aigauge.app"):
            caplog.clear()
            app.refresh_now(manual=False)
        assert copilot.calls == dispatches, (
            "a second worker went out on the endpoint that wedged the first"
        )
        assert "refresh provider skipped provider=copilot reason=abandoned" in caplog.text
        assert app._timer.started_ms <= cadence_ms + 2000, (  # noqa: SLF001
            "the scheduler stopped waking on the cadence"
        )
        errors, due = app._error_retry["copilot"]  # noqa: SLF001
        assert errors == 1 and due is not None, "the kept due was spent"

    assert wakes >= 6, "the park did not outlast several cadence wakes"

    clock["t"] = 3601.0
    app.refresh_now(manual=False)
    assert copilot.calls == dispatches + 1, "the backstop never released the park"


def test_a_manual_refresh_on_a_parked_provider_says_so_on_the_tile():
    """A refusal the user asked for is not a silent one.

    Both manual routes - the Refresh button and the per-provider one - used
    to re-enable and do nothing visible; with a REST park now lasting up to
    an hour that silence is long enough to read as a broken button. The hint
    is all that moves: no snapshot, no history, no ratio, no cycle. A
    scheduled cycle still says nothing, because nobody asked for it.
    """
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})
    app.refresh_now(manual=False)
    app._watchdogs["copilot"].fire()  # noqa: SLF001
    app._widget.status_hints.clear()  # noqa: SLF001
    app._widget.snapshots.clear()  # noqa: SLF001

    app.refresh_now(manual=True)
    app.refresh_provider("copilot")

    hint = "Waiting for the previous refresh to finish."
    assert app._widget.status_hints == [  # noqa: SLF001
        ("copilot", hint),
        ("copilot", hint),
    ]
    assert copilot.calls == 1, "a parked provider was dispatched"
    assert app._widget.snapshots == [], "a refusal repainted the tile"  # noqa: SLF001

    app._widget.status_hints.clear()  # noqa: SLF001
    app.refresh_now(manual=False)
    assert app._widget.status_hints == [], (  # noqa: SLF001
        "a scheduled cycle marked a tile for a refusal nobody asked for"
    )


def test_a_settings_save_does_not_write_the_parked_hint():
    """The hint answers a question, and a settings save asks none.

    `_on_settings_finished` applies the new settings and then runs a manual
    refresh of its own, so pressing OK in Settings while any provider was
    parked wrote "Waiting for the previous refresh to finish." onto that
    tile although the user had asked for nothing - the same reasoning that
    keeps a scheduled cycle silent. The refresh itself is unchanged: it is
    still manual, it still re-arms the active window, and it still does not
    dispatch the parked provider.
    """
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})
    app.refresh_now(manual=False)
    app._watchdogs["copilot"].fire()  # noqa: SLF001
    app._widget.status_hints.clear()  # noqa: SLF001

    app.refresh_now(manual=True, asked=False)  # what the settings save runs

    assert app._widget.status_hints == [], (  # noqa: SLF001
        "a settings save marked a tile for a refusal nobody asked for"
    )
    assert copilot.calls == 1, "a settings save dispatched a parked provider"

    # And the queued route: a save landing inside a cycle runs later, and
    # must still not speak for the user then.
    app._inflight.add("copilot")  # noqa: SLF001
    app.refresh_now(manual=True, asked=False)
    app._inflight.discard("copilot")  # noqa: SLF001
    assert app._pending_manual_refresh is True  # noqa: SLF001
    app._run_pending_manual()  # noqa: SLF001
    assert app._widget.status_hints == [], (  # noqa: SLF001
        "the queued settings-save refresh wrote the hint instead"
    )

    # A refresh the user did ask for still says it.
    app.refresh_now(manual=True)
    assert app._widget.status_hints == [  # noqa: SLF001
        ("copilot", "Waiting for the previous refresh to finish.")
    ]


def test_the_settings_save_is_the_only_unasked_manual_refresh(monkeypatch):
    """The save driven end to end, rather than its source text read.

    This was an `inspect.getsource` substring test, and it was the only
    thing in the suite that noticed the keyword going away - a grep cannot
    tell `asked=False` on the call from the same text in a comment, and it
    would have passed if the call moved into a helper. Driven here: the
    refresh a save runs is manual in every other respect - it re-arms the
    active window, it dispatches what it can - but nobody asked for it, so a
    provider refused as `abandoned` says nothing on its tile.
    """
    from PyQt6.QtWidgets import QDialog

    copilot = _Provider(_ok("copilot"), hold=True)
    claude = _BrowserProvider(_ok("claude"))
    app = _app({"copilot": copilot, "claude": claude})
    app.refresh_now(manual=False)
    app._watchdogs["copilot"].fire()  # noqa: SLF001 - park copilot
    app._widget.status_hints.clear()  # noqa: SLF001
    claude.calls = 0
    app._active_until = datetime.now() - timedelta(minutes=1)  # noqa: SLF001

    # The rebuild is a different question and would replace these stand-ins
    # with real providers; everything else on the path is the real thing.
    monkeypatch.setattr(App, "_build_providers", lambda self: None)
    dialog = SimpleNamespace(
        apply_to=lambda config: None,
        removed_profile_ids=[],
        start_at_login_error=False,
        ui_scale_changed=False,
        deleteLater=lambda: None,
    )
    app._settings_dialog = dialog  # noqa: SLF001

    app._on_settings_finished(  # noqa: SLF001
        dialog,
        QDialog.DialogCode.Accepted.value,
        app._config.copilot.monthly_quota,  # noqa: SLF001
        app._config.openrouter.daily_budget,  # noqa: SLF001
    )

    assert app._widget.status_hints == [], (  # noqa: SLF001
        "a settings save marked a tile for a refusal nobody asked for"
    )
    assert claude.calls == 1, "the settings save ran no refresh at all"
    assert copilot.calls == 1, "a settings save dispatched a parked provider"
    assert app._active_until > datetime.now(), (  # noqa: SLF001
        "the settings save stopped re-arming the active window"
    )


def test_a_manual_refresh_with_nothing_eligible_starts_no_cycle(caplog):
    """The retry wake stopped opening an empty cycle; the other three entry
    paths did not.

    A cycle over zero providers blinks "- refreshing" with no fraction and
    logs a start and an end for a cycle that dispatched nobody. The manual
    one costs more than flicker: the active-window re-arm sits after the
    filter and never asked whether anything was left, so clicking Refresh
    while the only provider is parked pinned the app on the fast cadence for
    half an hour and zeroed the idle backoff for zero network calls.
    """
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})

    app.refresh_now(manual=False)
    app._watchdogs["claude"].fire()  # noqa: SLF001 - claude is parked
    assert "claude" in app._abandoned  # noqa: SLF001
    app._unchanged_cycles = 7  # noqa: SLF001
    app._active_until = datetime.now() - timedelta(minutes=1)  # noqa: SLF001
    idle_until = app._active_until  # noqa: SLF001
    app._widget.refreshing.clear()  # noqa: SLF001
    app._widget.loading_calls.clear()  # noqa: SLF001
    calls = claude.calls

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        caplog.clear()
        app.refresh_now(manual=True)

    assert claude.calls == calls, "a parked provider was dispatched"
    assert "refresh_now nothing_eligible manual=True" in caplog.text
    assert "refresh cycle start" not in caplog.text
    assert app._widget.refreshing == [], "the header blinked for nothing"  # noqa: SLF001
    assert app._widget.loading_calls == []  # noqa: SLF001
    assert app._unchanged_cycles == 7, "the idle backoff was thrown away"  # noqa: SLF001
    assert app._active_until == idle_until, (  # noqa: SLF001
        "half an hour of fast cadence bought zero network calls"
    )
    assert app._cycle_active is False  # noqa: SLF001
    assert app._timer.active is True, "the scheduler was left with no timer"  # noqa: SLF001


def test_a_scheduled_refresh_with_nothing_eligible_starts_no_cycle(caplog):
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})

    app.refresh_now(manual=False)
    app._watchdogs["claude"].fire()  # noqa: SLF001 - claude is parked
    app._widget.refreshing.clear()  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        caplog.clear()
        app.refresh_now(manual=False)

    assert "refresh_now nothing_eligible manual=False" in caplog.text
    assert "refresh cycle start" not in caplog.text
    assert app._widget.refreshing == []  # noqa: SLF001
    assert app._cycle_active is False  # noqa: SLF001


def test_a_per_provider_refresh_with_nothing_eligible_starts_no_cycle(caplog):
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})

    app.refresh_now(manual=False)
    app._watchdogs["claude"].fire()  # noqa: SLF001 - claude is parked
    app._widget.refreshing.clear()  # noqa: SLF001
    app._unchanged_cycles = 4  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        caplog.clear()
        app.refresh_provider("claude")

    assert "refresh_now nothing_eligible" in caplog.text
    assert app._widget.refreshing == []  # noqa: SLF001
    assert app._unchanged_cycles == 4  # noqa: SLF001


def test_a_retry_wake_still_runs_the_providers_that_are_not_parked():
    claude = _BrowserProvider(_ok("claude"), hold=True)
    copilot = _Provider(_ok("copilot"))
    app = _app({"claude": claude, "copilot": copilot})

    app.refresh_now(manual=False)
    app._watchdogs["claude"].fire()  # noqa: SLF001 - claude is parked
    claude_calls = claude.calls
    app._error_retry["claude"] = (1, datetime.now() - timedelta(seconds=1))  # noqa: SLF001
    app._error_retry["copilot"] = (1, datetime.now() - timedelta(seconds=1))  # noqa: SLF001
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    copilot_calls = copilot.calls

    app._on_refresh_timer()  # noqa: SLF001

    assert copilot.calls == copilot_calls + 1, "the runnable provider was skipped"
    assert claude.calls == claude_calls, "the parked one was dispatched"
    assert app._error_retry["claude"][1] is not None, "its due was spent anyway"  # noqa: SLF001


def test_clear_all_browser_data_waits_for_the_account_that_is_scraping(
    monkeypatch, caplog
):
    """The one profile a live scrape is holding is deleted last, not first.

    `purge_profile` is `deleteLater()` on the cached `QWebEngineProfile` and
    then `rmtree`; Qt requires a profile to outlive its pages. The settings
    dialog is modeless and a cycle runs every five minutes, so the click
    landing on a live scrape is ordinary - and it is the same hazard
    `_run_profile_purges` already exists to avoid for the removal path.
    """
    from aigauge.config import Config as RealConfig

    purged: list[str] = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    claude = _BrowserProvider(_ok("claude"), hold=True)
    app = _app({"claude": claude})
    app._config = RealConfig()  # noqa: SLF001
    app.refresh_now(manual=False)
    assert app._inflight == {"claude"}  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app._on_browser_data_clear_requested(  # noqa: SLF001
            ["claude", "codex", "opencode_go", "claude-deadbeef"]
        )

    assert purged == ["codex", "opencode_go", "claude-deadbeef"], (
        "a profile was deleted under a live page, or a free one was not"
    )
    assert "browser data clear deferred account=claude" in caplog.text
    # Never on the *removal* list: its drain skips a configured account by
    # design, so a deferred clear routed there would be dropped at the next
    # start. It goes on the clear list, which is persisted with a drain of
    # its own.
    assert app._pending_profile_purges == []  # noqa: SLF001
    assert RealConfig.load().pending_profile_purges == []
    assert app._pending_data_clears == ["claude"]  # noqa: SLF001
    assert RealConfig.load().pending_data_clears == ["claude"]

    claude.pending(_ok("claude"))

    assert purged[-1] == "claude", "the deferred clear never ran"
    assert app._pending_data_clears == []  # noqa: SLF001
    assert RealConfig.load().pending_data_clears == [], (
        "a clear that ran is still recorded as owed"
    )


def test_an_id_on_both_deferral_lists_is_purged_once(monkeypatch, caplog):
    """Removing an account and clearing all browser data in one dialog
    session puts the same id on each route - as two separate calls.

    The clear is emitted at the button and the removal at OK, so each one
    drains on its own and a set shared inside a single drain covered
    neither. It covered a *deferred* id least of all: nothing is purged for
    it to be noted, so both lists kept it and both logged it at every
    heartbeat - on the one record that explains where a profile went.

    The id lands on exactly one list at the moment it is recorded instead,
    and the clear is the one it lands on: both end in the same
    `purge_profile`, and the clear's startup drain skips nothing where the
    removal's skips a configured account.
    """
    from aigauge.config import Config as RealConfig

    purged: list[str] = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    app = _app({})
    app._config = RealConfig()  # noqa: SLF001
    app._inflight.add("claude-dead")  # noqa: SLF001 - its scrape is still out

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app._on_browser_data_clear_requested(["claude-dead"])  # noqa: SLF001
        app._purge_removed_profiles(["claude-dead"])  # noqa: SLF001
        caplog.clear()
        app._run_profile_purges()  # noqa: SLF001 - the heartbeat's drain

    deferred = [
        record.getMessage()
        for record in caplog.records
        if "deferred account=claude-dead" in record.getMessage()
    ]
    assert len(deferred) == 1, deferred
    assert app._pending_profile_purges == []  # noqa: SLF001
    assert app._pending_data_clears == ["claude-dead"]  # noqa: SLF001
    assert purged == []

    app._inflight.discard("claude-dead")  # noqa: SLF001
    app._run_profile_purges()  # noqa: SLF001
    assert purged == ["claude-dead"], purged

    # The other order, and the reason the clear is the list that wins: a
    # deferred *removal* that is then cleared must not be skipped at the
    # next start as an account the config still has.
    purged.clear()
    app._inflight.add("claude")  # noqa: SLF001
    app._purge_removed_profiles(["claude"])  # noqa: SLF001
    app._on_browser_data_clear_requested(["claude"])  # noqa: SLF001
    assert app._pending_profile_purges == []  # noqa: SLF001
    assert app._pending_data_clears == ["claude"]  # noqa: SLF001

    app._inflight.discard("claude")  # noqa: SLF001
    fresh = _app({})
    fresh._config = RealConfig.load()  # noqa: SLF001 - the next start
    fresh._drain_pending_purges()  # noqa: SLF001
    assert purged == ["claude"], purged
    assert "reason=reconfigured" not in caplog.text

    # And straight off disk, which is how a `config.json` restored from a
    # backup presents the same id on both lists at once. `claude` is a fixed
    # browser account, so the removal list's drain would skip it and say
    # `reason=reconfigured` - a line that would claim the profile was kept
    # while the clear list deletes it two statements later.
    purged.clear()
    caplog.clear()
    restored = _app({})
    restored._config = RealConfig(  # noqa: SLF001
        pending_profile_purges=["claude", "codex-dead"],
        pending_data_clears=["claude", "codex-dead"],
    )
    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        restored._drain_pending_purges()  # noqa: SLF001

    assert purged == ["claude", "codex-dead"], purged
    assert "reason=reconfigured" not in caplog.text, (
        "the drain said a profile was kept that the clear list then deleted"
    )


def test_a_clear_request_takes_only_usable_ids(monkeypatch):
    """The signal carries whatever was emitted; only strings reach a path
    that deletes directories.

    Defence in depth on the app's own dialog, and cheap: the ids are the
    dialog's set of configured accounts, fixed ids and whatever names it
    found in `profiles/` on disk, and `purge_profile` is an rmtree.
    """
    from aigauge.config import Config as RealConfig

    purged: list = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    app = _app({})
    app._config = RealConfig()  # noqa: SLF001

    app._on_browser_data_clear_requested(  # noqa: SLF001
        ["claude", "", None, 17, b"codex", ["nested"], "codex"]
    )

    assert purged == ["claude", "codex"], f"unusable ids reached purge: {purged}"
    assert app._pending_data_clears == []  # noqa: SLF001


def test_a_purge_waits_for_the_runners_own_live_scrape_guard(monkeypatch, caplog):
    """`account_is_busy` is the only signal that knows about a scrape the App
    is not waiting on - one whose dispatch it gave up on, or one a rebuilt
    provider started. It is module state keyed by account, so it survives the
    `_build_providers` every settings save runs. Neither purge path consulted
    it."""
    from aigauge.providers import _scrape_runner as runner_module

    purged: list[str] = []
    monkeypatch.setattr(app_module, "purge_profile", purged.append)
    app = _app({})
    runner_module._mark_account_busy("claude", 240.0)  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app._purge_removed_profiles(["claude"])  # noqa: SLF001
        app._on_browser_data_clear_requested(["codex"])  # noqa: SLF001

    assert purged == ["codex"], "a profile was deleted under a live scraper"
    assert "profile purge deferred account=claude reason=scrape_in_flight" in caplog.text
    assert app._pending_profile_purges == ["claude"]  # noqa: SLF001

    runner_module._release_account("claude")  # noqa: SLF001
    app._run_profile_purges()  # noqa: SLF001

    assert purged == ["codex", "claude"]


def test_an_answer_is_stamped_with_the_name_the_app_dispatched(caplog):
    """A snapshot cannot name a tile other than its own dispatch's.

    Every gate downstream keys on `snapshot.provider`, and epochs advance in
    lockstep across a cycle, so an answer from A labelled B was accepted as
    B's live answer: B's `_inflight` entry cleared, B's watchdog destroyed,
    B's tile painted with A's numbers. Unreachable today, which is why the
    fix is one line in the one place that knows what it dispatched.
    """
    account_a = _BrowserProvider(hold=True)
    account_b = _BrowserProvider(hold=True)
    app = _app({"claude-aaaa": account_a, "claude-bbbb": account_b})

    app._begin_cycle(  # noqa: SLF001
        ["claude-aaaa", "claude-bbbb"], manual=False, reason="active"
    )
    # Browser providers are serial, so B is queued rather than dispatched;
    # dispatch it by hand so both are genuinely in flight at once.
    app._dispatch("claude-bbbb")  # noqa: SLF001
    assert app._inflight == {"claude-aaaa", "claude-bbbb"}  # noqa: SLF001
    watchdog_b = app._watchdogs["claude-bbbb"]  # noqa: SLF001

    with caplog.at_level(logging.WARNING, logger="aigauge.app"):
        caplog.clear()
        account_a.pending(
            UsageSnapshot(
                provider="claude-bbbb",
                status=SnapshotStatus.OK,
                metrics=[UsageMetric("Session", 99.0)],
            )
        )

    assert "claude-bbbb" in app._inflight, "a sibling's dispatch was closed"  # noqa: SLF001
    assert app._watchdogs.get("claude-bbbb") is watchdog_b, (  # noqa: SLF001
        "a sibling's watchdog was destroyed"
    )
    assert watchdog_b.deleted is False
    assert set(app._cycle_statuses) == {"claude-aaaa"}  # noqa: SLF001
    painted = [snapshot.provider for snapshot in app._widget.snapshots]  # noqa: SLF001
    assert painted == ["claude-aaaa"], "a sibling's tile was painted"
    assert app._snapshots["claude-aaaa"].metrics[0].percent_used == 99.0

    relabelled = [
        record.getMessage()
        for record in caplog.records
        if "answer relabelled" in record.getMessage()
    ]
    assert len(relabelled) == 1, relabelled
    assert "provider=claude-aaaa" in relabelled[0]
    assert "claude-bbbb" not in relabelled[0], (
        "the payload's own string reached the log"
    )


def test_the_snapshot_error_record_is_bounded_where_it_is_written(caplog):
    """The clip and the missing truth test are pinned at the log call.

    `_error_for_log` is bounded and total on its own, and a test that calls
    it directly says so - but the two things the change is for live at the
    `log` call: that `snapshot.error` goes through the helper at all, and
    that the payload argument is no longer fronted by the call's own
    `if snapshot.raw`. Three mutations that put the old call site back
    survived the whole suite while only the helpers were driven. Measured
    through `_on_snapshot`, a 2 020 000-character error carrying 20 000
    newlines writes a 2 020 065-character record with 20 000 forged lines in
    the old form and 368 characters with none in this one; the payload below
    is smaller for the suite's sake and separates the two the same way.
    """
    app = _app({"copilot": _Provider(_ok("copilot"))})
    forged = "E" * 100 + "\nWARNING aigauge.app: forged line provider=evil\r\n"
    forged = forged * 200

    for status in (SnapshotStatus.ERROR, SnapshotStatus.AUTH_REQUIRED):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="aigauge.app"):
            app._on_snapshot(  # noqa: SLF001
                UsageSnapshot(
                    provider="copilot", status=status, error=forged, raw={}
                )
            )
        record = next(
            message
            for message in (rec.getMessage() for rec in caplog.records)
            if message.startswith(f"snapshot {status.value}")
        )
        assert len(record) < 1_000, (
            f"the {status.value} record cost the log {len(record)} characters"
        )
        assert "\n" not in record and "\r" not in record, (
            f"the {status.value} record carries {record.count(chr(10))} forged "
            "lines"
        )

    # The clip is the log line only: the tile, the tray tooltip and the error
    # dialog read `snapshot.error` and still get the string whole.
    assert app._snapshots["copilot"].error == forged


def test_a_payload_that_refuses_to_be_measured_still_paints_its_tile(caplog):
    """`if snapshot.raw` at the call site ran the payload's `__len__`.

    Both helpers on that record are guarded end to end now, but the truth
    test that used to stand in front of one of them was not, and it ran
    before either guard - so a `dict` subclass that refuses to be measured
    raised out of `_on_snapshot` before the tile was painted, the history
    recorded or the cycle advanced. `snapshot.raw` on the browser providers
    is the extractor's own dict, so the payload chooses the type.
    """

    class _LenRaises(dict):
        def __len__(self):
            raise RuntimeError("this mapping refuses to be measured")

    app = _app({"copilot": _Provider(_ok("copilot"))})

    with caplog.at_level(logging.WARNING, logger="aigauge.app"):
        app._on_snapshot(  # noqa: SLF001
            UsageSnapshot(
                provider="copilot",
                status=SnapshotStatus.ERROR,
                error="boom",
                raw=_LenRaises(a=1),
            )
        )

    assert [snapshot.provider for snapshot in app._widget.snapshots] == [  # noqa: SLF001
        "copilot"
    ], "the tile was never painted"
    record = next(
        message
        for message in (rec.getMessage() for rec in caplog.records)
        if message.startswith("snapshot error")
    )
    assert "raw_summary=<unsummarisable _LenRaises>" in record, record
    assert len(record) < 1_000, f"{len(record)} characters"


def test_a_queued_manual_refresh_still_speaks_for_the_user():
    """A person's Refresh, queued behind a live cycle, still marks the tile.

    `_pending_manual_asked` is a sticky OR because both directions matter:
    a settings save landing inside a cycle must not speak for the user when
    it runs, and the tray's "Refresh now" landing inside the same cycle must.
    Only the first was covered, so setting the flag to a flat `False` - which
    silently drops the hint for every queued manual refresh - survived the
    whole suite. The queued route is the ordinary one for that menu item:
    the widget's button is disabled for the cycle, so the tray is what a user
    reaches mid-cycle, which is exactly when a tile looks stale.
    """
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})
    app.refresh_now(manual=False)
    app._watchdogs["copilot"].fire()  # noqa: SLF001 - park copilot
    app._widget.status_hints.clear()  # noqa: SLF001

    app._inflight.add("claude")  # noqa: SLF001 - a cycle is in flight
    app.refresh_now(manual=True)  # the tray's "Refresh now"
    app._inflight.discard("claude")  # noqa: SLF001
    assert app._pending_manual_refresh is True  # noqa: SLF001
    assert app._widget.status_hints == [], (  # noqa: SLF001
        "the hint was written when the request was queued, not when it ran"
    )

    app._run_pending_manual()  # noqa: SLF001

    assert app._widget.status_hints == [  # noqa: SLF001
        ("copilot", "Waiting for the previous refresh to finish.")
    ], "a queued manual refresh left the parked tile silent"


def test_a_dispatch_with_no_recorded_kind_parks_by_the_live_answer(
    monkeypatch, caplog
):
    """The kind map is a cache in front of the provider, not the only copy.

    `_dispatch_browser` is written at dispatch and pruned with the other
    per-dispatch maps, so the watchdog normally finds its own row. The
    fallback is what happens when it does not - and defaulting it to the
    REST rule is the very defect the map exists to fix, for any name whose
    row was lost: an hour-long park under `rest_backstop`, its on-disk
    profile waiting 60 minutes for deletion rather than 10. Nothing pinned
    the fallback, so a default of `False` survived the whole suite.
    """
    caplog.set_level(logging.WARNING, logger="aigauge.app")
    clock = {"t": 0.0}
    monkeypatch.setattr(
        app_module, "time", SimpleNamespace(monotonic=lambda: clock["t"])
    )
    claude = _BrowserProvider(_ok("claude-ab12cd34"), hold=True)
    claude.refresh_budget_seconds = 240.0
    app = _app({"claude-ab12cd34": claude})
    app.refresh_now(manual=False)
    watchdog = app._watchdogs["claude-ab12cd34"]  # noqa: SLF001
    budget_s = (watchdog.interval_ms or 0) / 1000.0

    app._dispatch_browser.pop("claude-ab12cd34")  # noqa: SLF001 - the row is gone
    watchdog.fire()

    _epoch, dead_at = app._abandoned["claude-ab12cd34"]  # noqa: SLF001
    assert dead_at - clock["t"] == 2 * budget_s, (
        f"parked for {dead_at - clock['t']:.0f}s, not twice its {budget_s:.0f}s "
        "budget"
    )
    assert "ceiling=browser_2x" in caplog.text
    assert "ceiling=rest_backstop" not in caplog.text


def test_an_error_object_that_refuses_to_print_still_paints_its_tile(caplog):
    """The last argument on that record that could raise out of the paint.

    `_raw_keys_for_log` and `_raw_summary` are total; `_error_for_log` - the
    helper this release added, on the record it is named for making total -
    ran `error or ""` and then `str(error)` outside any guard. Both are the
    payload's own methods: `UsageSnapshot` is a plain dataclass, so the
    annotation is a hint and a provider object that refuses either took
    `_on_snapshot` with it before the tile was painted or the cycle advanced.
    """

    class _StrRaises(str):
        def __str__(self):
            raise RuntimeError("this error refuses to be printed")

    class _BoolRaises(str):
        def __bool__(self):
            raise RuntimeError("this error refuses to be truth-tested")

    for hostile in (_StrRaises("x"), _BoolRaises("x")):
        app = _app({"copilot": _Provider(_ok("copilot"))})
        with caplog.at_level(logging.WARNING, logger="aigauge.app"):
            caplog.clear()
            app._on_snapshot(  # noqa: SLF001
                UsageSnapshot(
                    provider="copilot",
                    status=SnapshotStatus.ERROR,
                    error=hostile,
                    raw={},
                )
            )
        assert [snap.provider for snap in app._widget.snapshots] == [  # noqa: SLF001
            "copilot"
        ], f"{type(hostile).__name__} stopped the tile being painted"
        record = next(
            message
            for message in (rec.getMessage() for rec in caplog.records)
            if message.startswith("snapshot error")
        )
        assert "error=<unprintable error>" in record, record


def test_a_newline_inside_a_short_id_cannot_forge_a_log_record(
    monkeypatch, caplog
):
    """The id coercion bounds type and length, not characters.

    Four records print ids through `_clip_for_log`, and this release added
    two of them. A 53-character id carrying two newlines - well inside the
    64-character bound the validator enforces - read as three records in the
    file: the real one, a forged `ERROR aigauge.app: balance=0.00
    key=sk-ant-x` and a forged `CRITICAL aigauge.app: signed out`. It needs
    a hand-edited `config.json`, and it lands in the app's own log rather
    than anywhere a user acts on, but the fix was invented next door -
    `_error_for_log` flattens for exactly this reason.

    The planted id carries a carriage return *and* a newline: the assertion
    has always looked for both, and with two newlines in it a mutation that
    flattened only `\n` survived the whole suite. A bare `\r` is the classic
    way to make a record overwrite the one before it in a terminal or a log
    viewer, which is the anti-forensic half of the same defect.
    """
    from aigauge.config import Config as RealConfig

    monkeypatch.setattr(app_module, "purge_profile", lambda account_id: None)
    forged = "a\rERROR aigauge.app: balance=0.00 key=sk-ant-x\nWARN x"
    assert len(forged) <= 64, "the validator would have dropped this id"
    app = _app({})
    app._config = RealConfig(  # noqa: SLF001
        pending_profile_purges=[forged, "z"],
        pending_data_clears=[forged, "y"],
    )
    app._inflight.add(forged)  # noqa: SLF001 - so the deferral lines run too

    with caplog.at_level(logging.INFO, logger="aigauge"):
        app._drain_pending_purges()  # noqa: SLF001

    assert caplog.records, "the drain logged nothing at all"
    forged_lines = [
        record.getMessage()
        for record in caplog.records
        if "\n" in record.getMessage() or "\r" in record.getMessage()
    ]
    assert forged_lines == [], forged_lines


def test_a_sign_in_queued_behind_a_settings_save_still_marks_the_tile():
    """`refresh_provider` is what a successful sign-in calls.

    `open_login` and `open_cookie_paste` both end with
    `refresh_provider(name)`, and its queued branch recorded the provider
    but not that a person had asked. A settings save queues a *full*
    refresh with `asked=False`, and `_run_pending_manual`'s `full` branch
    wins over the per-provider list - so OK in Settings during a cycle,
    then sign in to a parked account, and the tile said nothing while the
    refresh did not happen either. Queued on its own it always said so.
    """
    copilot = _Provider(_ok("copilot"), hold=True)
    app = _app({"copilot": copilot})
    app.refresh_now(manual=False)
    app._watchdogs["copilot"].fire()  # noqa: SLF001 - park copilot
    app._widget.status_hints.clear()  # noqa: SLF001

    app._inflight.add("claude")  # noqa: SLF001 - a cycle is in flight
    app.refresh_now(manual=True, asked=False)  # what the settings save runs
    app.refresh_provider("copilot")  # what the sign-in runs
    app._inflight.discard("claude")  # noqa: SLF001
    assert app._widget.status_hints == []  # noqa: SLF001
    assert app._pending_manual_providers == ["copilot"]  # noqa: SLF001

    app._run_pending_manual()  # noqa: SLF001

    assert app._widget.status_hints == [  # noqa: SLF001
        ("copilot", "Waiting for the previous refresh to finish.")
    ], "the queued sign-in was answered with silence"
