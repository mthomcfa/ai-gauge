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
from aigauge.app import App
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


class _Provider:
    """Calls back synchronously unless told to hold the callback."""

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
    app._consecutive_error_cycles = 0  # noqa: SLF001
    app._cycle_active = False  # noqa: SLF001
    app._cycle_started_at = None  # noqa: SLF001
    app._cycle_reason = "startup"  # noqa: SLF001
    app._cycle_statuses = {}  # noqa: SLF001
    app._dispatch_times = {}  # noqa: SLF001
    app._next_refresh_reason = "startup"  # noqa: SLF001
    app._active_until = datetime.now() + timedelta(minutes=30)  # noqa: SLF001
    app._current_refresh_manual = False  # noqa: SLF001
    app._pending_manual_refresh = False  # noqa: SLF001
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
        m.startswith("refresh provider start provider=claude queued_s=")
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
    and nothing happened" was unprovable either way.
    """
    app = _app({"claude": _Provider(_ok("claude"), hold=True)})
    app.refresh_now(manual=False)
    assert app._inflight == {"claude"}  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.app"):
        app.refresh_now(manual=True)

    assert "refresh_now" in caplog.text
    assert "inflight=claude" in caplog.text


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
    assert "error_cycles=" in line
