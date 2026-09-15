"""Invariants the refresh scheduler must hold no matter what it is fed.

The tests beside this file pin individual behaviours - what a wake logs, which
provider a retry visits. These pin the two properties that make the scheduler
*safe*, over a fake clock long enough for a defect to compound:

1. **The scheduler cannot spin.** However a provider answers, an hour of wall
   clock buys a bounded number of cycles. A review of this PR measured 3 543
   cycles in an hour - a 1 Hz busy loop with `mark_loading` on every
   iteration, writing ~3.8 MB into a 1.5 MiB rotating log ring, which
   overwrites the whole diagnostic history every eight minutes.

2. **A browser provider never has two scrapes alive at once.** Each one is a
   `QWebEngineView` on the single cached `QWebEngineProfile` for that account
   (`webview/profile.py`), so two of them are two writers to one cookie store.
   The same review fuzzed cycles, watchdogs and manual refreshes and measured
   7 concurrent scrapes of one provider in an hour and 19 in six.

Both run the real `App` methods against a deterministic event queue, so a
regression shows up as a number, not a hang.
"""

from __future__ import annotations

import heapq
import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import responses

import aigauge.app as app_module
import aigauge.providers.azure as az
from aigauge.app import App
from aigauge.config import AzureConfig, Config
from aigauge.models import SnapshotStatus, UsageSnapshot

BASE = datetime(2026, 9, 15, 9, 0, 0)
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"


class _Clock:
    """A deterministic event queue standing in for the Qt event loop."""

    def __init__(self) -> None:
        self.t = 0.0
        self._queue: list[tuple[float, int, object]] = []
        self._seq = 0

    def now(self) -> datetime:
        return BASE + timedelta(seconds=self.t)

    def at(self, delay: float, callback) -> int:
        self._seq += 1
        heapq.heappush(self._queue, (self.t + max(0.0, delay), self._seq, callback))
        return self._seq

    def cancel(self, token: int) -> None:
        self._queue = [entry for entry in self._queue if entry[1] != token]
        heapq.heapify(self._queue)

    def run_until(self, end: float, *, max_events: int = 200_000) -> None:
        fired = 0
        while self._queue and self._queue[0][0] <= end:
            due, _seq, callback = heapq.heappop(self._queue)
            self.t = max(self.t, due)
            callback()
            fired += 1
            if fired > max_events:
                raise AssertionError(
                    f"the scheduler fired {fired} events before {end}s - runaway"
                )
        self.t = end


class _Signal:
    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            slot(*args)


def _timer_class(clock: _Clock):
    class _ClockTimer:
        """A QTimer whose expiry is an entry in ``clock``."""

        def __init__(self, parent=None) -> None:
            self.parent = parent
            self.timeout = _Signal()
            self.deleted = False
            self._token: int | None = None
            self._active = False

        def setSingleShot(self, value) -> None:
            pass

        def start(self, ms=None) -> None:
            self.stop()
            self._active = True

            def _fire() -> None:
                self._active = False
                self._token = None
                self.timeout.emit()

            self._token = clock.at((ms or 0) / 1000.0, _fire)

        def stop(self) -> None:
            if self._token is not None:
                clock.cancel(self._token)
                self._token = None
            self._active = False

        def deleteLater(self) -> None:
            self.deleted = True

        def isActive(self) -> bool:
            return self._active

        def remainingTime(self) -> int:
            return 0

        @staticmethod
        def singleShot(ms, callback) -> None:
            clock.at((ms or 0) / 1000.0, callback)

    return _ClockTimer


@pytest.fixture()
def clock(monkeypatch):
    clk = _Clock()
    timer_cls = _timer_class(clk)
    monkeypatch.setattr(app_module, "QTimer", timer_cls)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clk.now()

    monkeypatch.setattr(app_module, "datetime", _FakeDatetime)
    monkeypatch.setattr(
        app_module, "time", SimpleNamespace(monotonic=lambda: clk.t)
    )
    monkeypatch.setattr(az, "datetime", _FakeDatetime)
    clk.timer_cls = timer_cls
    yield clk


def _build_app(clock: _Clock, providers: dict, config: Config) -> App:
    app = App.__new__(App)
    app._config = config  # noqa: SLF001
    app._providers = dict(providers)  # noqa: SLF001
    app._snapshots = {}  # noqa: SLF001
    app._inflight = set()  # noqa: SLF001
    app._refresh_queue = []  # noqa: SLF001
    app._cycle_signatures = {}  # noqa: SLF001
    app._last_cycle_signatures = None  # noqa: SLF001
    app._unchanged_cycles = 0  # noqa: SLF001
    app._error_retry = {}  # noqa: SLF001
    app._active_until = clock.now() + timedelta(minutes=30)  # noqa: SLF001
    app._current_refresh_manual = False  # noqa: SLF001
    app._pending_manual_refresh = False  # noqa: SLF001
    app._pending_manual_providers = []  # noqa: SLF001
    app._watchdogs = {}  # noqa: SLF001
    app._cycle_active = False  # noqa: SLF001
    app._cycle_started_at = None  # noqa: SLF001
    app._cycle_reason = "startup"  # noqa: SLF001
    app._cycle_statuses = {}  # noqa: SLF001
    app._cycle_names = set()  # noqa: SLF001
    app._cycle_total = 0  # noqa: SLF001
    app._cycle_partial = False  # noqa: SLF001
    app._dispatch_times = {}  # noqa: SLF001
    app._dispatch_epoch = {}  # noqa: SLF001
    app._abandoned = {}  # noqa: SLF001
    app._pool_wait_budgets = {}  # noqa: SLF001
    app._pending_profile_purges = []  # noqa: SLF001
    app._dispatching = False  # noqa: SLF001
    app._next_refresh_reason = "startup"  # noqa: SLF001
    app._started_at = clock.now()  # noqa: SLF001
    app._ui_mode = "floating_widget"  # noqa: SLF001
    timer_cls = clock.timer_cls
    app._timer = timer_cls()  # noqa: SLF001
    app._timer.setSingleShot(True)  # noqa: SLF001
    app._timer.timeout.connect(app._on_refresh_timer)  # noqa: SLF001
    app._signals = SimpleNamespace(snapshot_ready=_Signal())  # noqa: SLF001
    app._signals.snapshot_ready.connect(app._on_snapshot)  # noqa: SLF001
    app._history = SimpleNamespace(record_snapshot=lambda snap: [])  # noqa: SLF001
    app._ratio = SimpleNamespace(  # noqa: SLF001
        record_snapshot=lambda snap: None,
        display_estimate=lambda name: None,
        current_estimate=lambda name: None,
    )
    app._ratio_recent = lambda name: []  # noqa: SLF001
    app._update_tray = lambda: None  # noqa: SLF001
    app._widget = SimpleNamespace(  # noqa: SLF001
        set_refreshing=lambda *a, **k: None,
        set_refresh_progress=lambda *a, **k: None,
        mark_loading=lambda *a, **k: None,
        set_refresh_state=lambda **k: None,
        update_snapshot=lambda *a, **k: None,
        set_ratio=lambda *a, **k: None,
        remove_tile=lambda *a, **k: None,
        isVisible=lambda: True,
    )
    return app


class _SyncPool:
    """A QThreadPool whose worker runs inline."""

    def start(self, runnable) -> None:
        runnable.run()


class _DeadPool:
    """A QThreadPool whose worker never runs - a wedged fetch.

    `AzureProvider.refresh` sets `in_flight = True` before handing the
    runnable to the pool, so this is the state the App watchdog exists for.
    """

    def __init__(self) -> None:
        self.queued: list = []

    def start(self, runnable) -> None:
        self.queued.append(runnable)


def _azure_config() -> Config:
    config = Config()
    config.azure = AzureConfig(
        tenant_id=TENANT,
        client_id=CLIENT,
        subscription_id=SUBSCRIPTION,
        monthly_allowance=150.0,
        reset_day=1,
    )
    return config


# An hour at the app's own floor is 12 cycles for a 5-minute active cadence,
# plus the three fast retries a real failure earns. Fifteen is that, with no
# room for a loop.
_CYCLES_PER_HOUR_CEILING = 15


@pytest.fixture()
def azure_state(monkeypatch):
    az.reset_states()
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    yield
    az.reset_states()


@responses.activate
def _run_azure_hour(clock, caplog, *, watchdog_route: bool) -> int:
    """Drive the real AzureProvider for a fake hour and count cycles."""
    responses.add(
        responses.POST,
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
        body=Exception("connection refused"),
    )
    responses.add(
        responses.POST, "https://management.azure.com/", body=Exception("x")
    )
    config = _azure_config()

    if watchdog_route:
        # No user action at all. The first fetch wedges - the worker never
        # reports - so azure holds `in_flight`. The App watchdog gives up and
        # synthesises an ERROR that carries no error_class, which arms the
        # fast retry; the re-dispatch a minute later meets azure's in_flight
        # gate, which answers `throttled`, and nothing ever clears the due.
        provider = az.AzureProvider(config, pool=_DeadPool())
        app = _build_app(clock, {"azure": provider}, config)
        app.refresh_now(manual=True)
        assert app._inflight == {"azure"}  # noqa: SLF001
        # Azure's watchdog budget is its real worst case, so give the
        # watchdog time to fire before the hour under test starts.
        clock.run_until(400.0)
        assert app._snapshots["azure"].status is SnapshotStatus.ERROR  # noqa: SLF001
    else:
        # The ordinary user sequence. The offline burst: one live fetch that
        # fails at the transport layer. It arms azure's fast retry and sets
        # last_fetch_at, so the hourly gate is now shut with nothing cached.
        # Then the user edits an Azure query field and clicks OK: azure
        # clears state.last_error but keeps last_fetch_at, so every refresh
        # from here answers throttled_no_cache from memory.
        provider = az.AzureProvider(config, pool=_SyncPool())
        app = _build_app(clock, {"azure": provider}, config)
        app.refresh_now(manual=True)
        clock.run_until(5.0)
        assert app._error_retry.get("azure", (0, None))[0] == 1  # noqa: SLF001
        clock.run_until(10.0)
        config.azure.reset_day = 15
        app._providers["azure"] = az.AzureProvider(  # noqa: SLF001
            config, pool=_SyncPool()
        )
        app.refresh_now(manual=True)
        clock.run_until(15.0)

    caplog.clear()
    clock.run_until(clock.t + 3600.0)
    return sum(
        1
        for record in caplog.records
        if record.getMessage().startswith("refresh cycle start")
    )


@pytest.mark.parametrize("watchdog_route", [False, True])
def test_a_provider_that_only_ever_answers_throttled_cannot_spin_the_scheduler(
    clock, caplog, azure_state, watchdog_route
):
    """The hard bound, driven through the real AzureProvider.

    `_record_provider_outcome` returns early for a `throttled` answer. The
    early return left an already-owed, now-past `due` in `_error_retry`;
    `_error_retry_time` clamps a past due to *now*, `_schedule_next_refresh`
    floors the delay at 1 000 ms, and the wake produces the same answer. Both
    routes in were measured at over 3 500 cycles in the hour.
    """
    caplog.set_level("INFO", logger="aigauge")
    cycles = _run_azure_hour(clock, caplog, watchdog_route=watchdog_route)

    assert cycles <= _CYCLES_PER_HOUR_CEILING, (
        f"{cycles} cycles in one hour - the scheduler is spinning"
    )


class _FuzzProvider:
    """A provider shaped like the real browser ones.

    It refuses a re-entrant refresh exactly as `ClaudeProvider`,
    `CodexProvider` and `OpenCodeGoProvider` now do - `ScrapeRunner.busy()`
    is what a second `QWebEngineView` on one profile would have to get past -
    and counts both the scrapes that really start (``max_live``) and the
    dispatches that met the guard (``refused``). A dispatch that never
    answers stays live until the test releases it, which is what makes the
    concurrency invariant measurable.
    """

    def __init__(self, name: str, *, uses_browser: bool, clock: _Clock, rng):
        self.name = name
        self.uses_browser = uses_browser
        self._clock = clock
        self._rng = rng
        self.live = 0
        self.max_live = 0
        self.dispatches = 0
        self.refused = 0
        self.outstanding: list = []

    def refresh(self, on_done) -> None:
        self.dispatches += 1
        if self.live:
            self.refused += 1
            on_done(
                UsageSnapshot(
                    provider=self.name,
                    status=SnapshotStatus.ERROR,
                    error="A refresh is already running.",
                    error_class="throttled",
                )
            )
            return
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        roll = self._rng.random()
        if roll < 0.12:
            # Never answers on its own: the watchdog is what ends it.
            self.outstanding.append(on_done)
            return
        delay = self._rng.uniform(0.5, 30.0)
        status = (
            SnapshotStatus.OK if self._rng.random() < 0.6 else SnapshotStatus.ERROR
        )

        def _answer() -> None:
            self.live -= 1
            on_done(
                UsageSnapshot(
                    provider=self.name,
                    status=status,
                    error=None if status == SnapshotStatus.OK else "boom",
                )
            )

        self._clock.at(delay, _answer)

    def release_one(self) -> bool:
        """Let one abandoned worker finally report back."""
        if not self.outstanding:
            return False
        on_done = self.outstanding.pop(0)
        self.live -= 1
        on_done(
            UsageSnapshot(provider=self.name, status=SnapshotStatus.OK)
        )
        return True


@pytest.mark.parametrize("seed", [1, 7, 19, 42, 101])
def test_a_browser_provider_never_has_two_scrapes_alive(clock, seed):
    """Six fake hours of cycles, watchdogs, manual refreshes and settings
    saves. Two live scrapes on one account means two `QWebEngineView`s on the
    single cached `QWebEngineProfile` for it - two writers to one cookie
    store, which is how a spurious sign-out happens.
    """
    rng = random.Random(seed)
    config = Config()
    providers = {
        "claude": _FuzzProvider("claude", uses_browser=True, clock=clock, rng=rng),
        "codex": _FuzzProvider("codex", uses_browser=True, clock=clock, rng=rng),
        "copilot": _FuzzProvider("copilot", uses_browser=False, clock=clock, rng=rng),
        "openrouter": _FuzzProvider(
            "openrouter", uses_browser=False, clock=clock, rng=rng
        ),
    }
    app = _build_app(clock, providers, config)
    # Record every dispatch against what the App believed at the time.
    dispatched_while_parked: list[str] = []
    real_dispatch = app._dispatch  # noqa: SLF001

    def _watched_dispatch(name, _real=real_dispatch):
        if name in app._abandoned:  # noqa: SLF001
            dispatched_while_parked.append(name)
        _real(name)

    app._dispatch = _watched_dispatch  # noqa: SLF001
    app.refresh_now(manual=False)

    six_hours = 6 * 3600.0
    while clock.t < six_hours:
        clock.run_until(min(six_hours, clock.t + rng.uniform(5.0, 90.0)))
        roll = rng.random()
        if roll < 0.25:
            app.refresh_now(manual=True)
        elif roll < 0.40:
            app.refresh_provider(rng.choice(list(providers)))
        elif roll < 0.55:
            # A settings save: rebuild, restart the timer, refresh.
            app._restart_timer()  # noqa: SLF001
            app.refresh_now(manual=True)
        elif roll < 0.75:
            providers[rng.choice(list(providers))].release_one()

    for provider in providers.values():
        if provider.uses_browser:
            assert provider.max_live <= 1, (
                f"{provider.name}: {provider.max_live} scrapes alive at once "
                "on one QtWebEngine profile"
            )
    # And the App is not leaning on that guard: while a provider is parked,
    # nothing dispatches it. Only the assumed-dead ceiling lets it go, and
    # that is the one case the provider's own guard is there to catch.
    assert dispatched_while_parked == [], (
        f"dispatched while the App had it parked: {sorted(set(dispatched_while_parked))}"
    )
    assert any(p.dispatches for p in providers.values()), "nothing ever ran"
