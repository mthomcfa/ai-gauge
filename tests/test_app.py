from datetime import datetime, timedelta
from types import SimpleNamespace

from aigauge.app import (
    App,
    _acquire_instance_lock,
    _adaptive_refresh_minutes,
    _enabled_providers,
    _preserve_error_metrics,
    _refresh_provider_order,
    _raw_summary,
)
from aigauge.config import BrowserAccount, Config
from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot


class _Timer:
    def __init__(self):
        self.stopped = False
        self.started_ms: int | None = None
        self.active = False
        self.remaining_ms = 0

    def stop(self):
        self.stopped = True
        self.active = False

    def start(self, ms: int):
        self.started_ms = ms
        self.remaining_ms = ms
        self.active = True

    def isActive(self):
        return self.active

    def remainingTime(self):
        return self.remaining_ms


class _Widget:
    def __init__(self):
        self.loading_calls = []
        self.refreshing = []
        self.refresh_state_calls = []
        self.visible = True

    def set_refreshing(self, refreshing):
        self.refreshing.append(refreshing)

    def mark_loading(self, providers):
        self.loading_calls.append(providers)

    def set_refresh_state(self, *, active, minutes, next_at=None):
        self.refresh_state_calls.append(
            {"active": active, "minutes": minutes, "next_at": next_at}
        )

    def isVisible(self):
        return self.visible


class _Dialog:
    def __init__(self):
        self.calls = []

    def isMinimized(self):
        return False

    def show(self):
        self.calls.append("show")

    def showNormal(self):
        self.calls.append("showNormal")

    def raise_(self):
        self.calls.append("raise")

    def activateWindow(self):
        self.calls.append("activate")


def _refresh_app_stub() -> App:
    app = App.__new__(App)
    app._providers = {"claude": object(), "codex": object()}  # noqa: SLF001
    app._inflight = set()  # noqa: SLF001
    app._refresh_queue = []  # noqa: SLF001
    app._active_until = datetime.now() - timedelta(minutes=1)  # noqa: SLF001
    app._unchanged_cycles = 3  # noqa: SLF001
    app._timer = _Timer()  # noqa: SLF001
    app._current_refresh_manual = False  # noqa: SLF001
    app._cycle_signatures = {"old": ()}  # noqa: SLF001
    app._widget = _Widget()  # noqa: SLF001
    app._start_next_refresh = lambda: None  # noqa: SLF001
    app._config = SimpleNamespace()  # noqa: SLF001
    return app


def test_adaptive_refresh_uses_active_interval_when_active():
    assert _adaptive_refresh_minutes(
        active=True,
        active_minutes=5,
        unchanged_cycles=10,
        max_minutes=15,
    ) == 5


def test_adaptive_refresh_backs_off_when_unchanged():
    assert _adaptive_refresh_minutes(
        active=False,
        active_minutes=5,
        unchanged_cycles=0,
        max_minutes=60,
    ) == 5
    assert _adaptive_refresh_minutes(
        active=False,
        active_minutes=5,
        unchanged_cycles=1,
        max_minutes=60,
    ) == 10
    assert _adaptive_refresh_minutes(
        active=False,
        active_minutes=5,
        unchanged_cycles=3,
        max_minutes=60,
    ) == 40


def test_adaptive_refresh_caps_at_configured_max():
    assert _adaptive_refresh_minutes(
        active=False,
        active_minutes=5,
        unchanged_cycles=8,
        max_minutes=15,
    ) == 15


def test_adaptive_refresh_respects_short_user_interval():
    assert _adaptive_refresh_minutes(
        active=True,
        active_minutes=5,
        unchanged_cycles=0,
        max_minutes=2,
    ) == 2


def test_adaptive_refresh_uses_configured_active_rate():
    assert _adaptive_refresh_minutes(
        active=True,
        active_minutes=1,
        unchanged_cycles=0,
        max_minutes=60,
    ) == 1
    assert _adaptive_refresh_minutes(
        active=False,
        active_minutes=15,
        unchanged_cycles=1,
        max_minutes=120,
    ) == 30


def test_manual_refresh_marks_tiles_loading():
    app = _refresh_app_stub()

    app.refresh_now(manual=True)

    assert app._widget.loading_calls == [  # noqa: SLF001
        {"claude": "Claude", "codex": "Codex"}
    ]
    assert app._refresh_queue == ["claude", "codex"]  # noqa: SLF001
    assert app._unchanged_cycles == 0  # noqa: SLF001


def test_scheduled_refresh_keeps_existing_tiles_visible():
    app = _refresh_app_stub()

    app.refresh_now(manual=False)

    assert app._widget.loading_calls == []  # noqa: SLF001
    assert app._refresh_queue == ["claude", "codex"]  # noqa: SLF001
    assert app._unchanged_cycles == 3  # noqa: SLF001


def test_refresh_order_prioritizes_openrouter_without_reordering_tiles():
    providers = {
        "claude": object(),
        "codex": object(),
        "copilot": object(),
        "openrouter": object(),
    }

    assert _refresh_provider_order(providers) == [
        "openrouter",
        "claude",
        "codex",
        "copilot",
    ]


def test_enabled_providers_includes_enabled_browser_accounts():
    config = Config()
    config.browser_accounts.append(
        BrowserAccount(id="claude-team", kind="claude", name="Team", enabled=True)
    )
    config.providers.codex = False

    assert _enabled_providers(config) == (
        "claude",
        "claude-team",
        "copilot",
    )




def test_enabled_providers_includes_opencode_go_when_enabled():
    config = Config()
    config.providers.opencode_go = True

    assert "opencode_go" in _enabled_providers(config)

def test_widget_activation_raises_open_settings_dialog():
    app = App.__new__(App)
    dialog = _Dialog()
    app._settings_dialog = dialog  # noqa: SLF001

    app._on_widget_activated()  # noqa: SLF001

    assert dialog.calls == ["show", "raise", "activate"]




def test_tile_expanded_changed_persists_browser_tile_collapsed_state():
    app = App.__new__(App)
    app._config = Config()  # noqa: SLF001

    app._on_tile_expanded_changed("claude", False)  # noqa: SLF001

    assert app._config.collapsed_tiles == ["claude"]
    assert app._config.expanded_tiles == []

    app._on_tile_expanded_changed("claude", True)  # noqa: SLF001

    assert app._config.collapsed_tiles == []


def test_tile_expanded_changed_keeps_openrouter_model_expansion_state():
    app = App.__new__(App)
    app._config = Config()  # noqa: SLF001

    app._on_tile_expanded_changed("openrouter", True)  # noqa: SLF001

    assert app._config.expanded_tiles == ["openrouter"]
    assert app._config.collapsed_tiles == []

def test_raw_summary_includes_sanitized_payload_details():
    summary = _raw_summary(
        {
            "session": None,
            "weekly": {
                "raw": "x" * 400,
                "percent": None,
            },
            "items": [{"a": 1}, {"b": 2}, {"c": 3}, {"d": 4}, {"e": 5}, {"f": 6}],
        }
    )

    assert '"session": null' in summary
    assert '"percent": null' in summary
    assert "xxx" in summary
    assert "more" in summary
    assert len(summary) < 700


def test_error_snapshot_preserves_previous_metrics():
    previous = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.OK,
        metrics=[UsageMetric("Session", 42.0, None)],
    )
    current = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="extractor retry limit exceeded",
    )

    merged = _preserve_error_metrics(current, previous)

    assert merged.status == SnapshotStatus.ERROR
    assert merged.error == "extractor retry limit exceeded"
    assert [(m.label, m.percent_used) for m in merged.metrics] == [("Session", 42.0)]


def test_repeated_error_snapshot_keeps_stale_metrics():
    previous = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="previous failure",
        metrics=[UsageMetric("Session", 42.0, None)],
    )
    current = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="extractor retry limit exceeded",
    )

    merged = _preserve_error_metrics(current, previous)

    assert [(m.label, m.percent_used) for m in merged.metrics] == [("Session", 42.0)]


def test_lifecycle_context_includes_refresh_state():
    app = App.__new__(App)
    app._started_at = datetime.now() - timedelta(seconds=90)  # noqa: SLF001
    app._ui_mode = "floating_widget"  # noqa: SLF001
    app._widget = _Widget()  # noqa: SLF001
    app._config = SimpleNamespace(  # noqa: SLF001
        providers=SimpleNamespace(
            claude=True, codex=False, copilot=True, openrouter=False
        )
    )
    app._inflight = {"claude"}  # noqa: SLF001
    app._refresh_queue = ["copilot"]  # noqa: SLF001
    app._unchanged_cycles = 2  # noqa: SLF001
    app._consecutive_error_cycles = 0  # noqa: SLF001
    app._timer = _Timer()  # noqa: SLF001
    app._timer.start(125_000)  # noqa: SLF001

    context = app._lifecycle_context()  # noqa: SLF001

    assert context["uptime_s"] >= 89
    assert context["ui_mode"] == "floating_widget"
    assert context["widget_visible"] is True
    assert context["providers"] == "claude,copilot"
    assert context["inflight"] == "claude"
    assert context["queue"] == "copilot"
    assert context["next_refresh_s"] == 125
    assert context["unchanged_cycles"] == 2


def test_instance_lock_prevents_second_running_copy(tmp_path, monkeypatch):
    monkeypatch.setattr("aigauge.app.app_data_dir", lambda: tmp_path)

    first = _acquire_instance_lock()
    assert first is not None
    try:
        assert _acquire_instance_lock() is None
    finally:
        first.unlock()


def _schedule_app_stub() -> App:
    app = App.__new__(App)
    app._inflight = set()  # noqa: SLF001
    app._refresh_queue = []  # noqa: SLF001
    app._active_until = datetime.now() - timedelta(minutes=1)  # noqa: SLF001
    app._unchanged_cycles = 5  # noqa: SLF001
    app._consecutive_error_cycles = 0  # noqa: SLF001
    app._timer = _Timer()  # noqa: SLF001
    app._widget = _Widget()  # noqa: SLF001
    app._snapshots = {}  # noqa: SLF001
    app._config = SimpleNamespace(
        active_refresh_interval_minutes=5,
        refresh_interval_minutes=60,
    )
    return app


def test_schedule_pulls_refresh_forward_to_known_reset():
    app = _schedule_app_stub()
    soon = datetime.now() + timedelta(minutes=10)
    app._snapshots = {  # noqa: SLF001
        "claude": UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric(label="Session", percent_used=80.0, resets_at=soon),
            ],
        ),
    }

    app._schedule_next_refresh()  # noqa: SLF001

    # Default backoff would be way longer; reset+grace is ~11 minutes.
    assert app._timer.started_ms is not None  # noqa: SLF001
    scheduled_minutes = app._timer.started_ms / 60_000  # noqa: SLF001
    assert 9 <= scheduled_minutes <= 13


def test_schedule_ignores_unused_metric_resets():
    app = _schedule_app_stub()
    soon = datetime.now() + timedelta(minutes=10)
    app._snapshots = {  # noqa: SLF001
        # 0% used — resetting changes nothing visible.
        "claude": UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric(label="Session", percent_used=0.0, resets_at=soon),
            ],
        ),
    }

    app._schedule_next_refresh()  # noqa: SLF001

    assert app._timer.started_ms is not None  # noqa: SLF001
    scheduled_minutes = app._timer.started_ms / 60_000  # noqa: SLF001
    # Falls back to adaptive backoff (5 min × 2^5 = 160, capped at 60).
    assert scheduled_minutes >= 30


def test_schedule_pulls_stale_error_refresh_forward():
    app = _schedule_app_stub()
    app._snapshots = {  # noqa: SLF001
        "claude": UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.ERROR,
            error="Could not read usage from page.",
            metrics=[UsageMetric(label="Session", percent_used=80.0)],
        ),
    }

    app._schedule_next_refresh()  # noqa: SLF001

    assert app._timer.started_ms is not None  # noqa: SLF001
    scheduled_minutes = app._timer.started_ms / 60_000  # noqa: SLF001
    assert 0 < scheduled_minutes <= 1.2


def test_log_summary_is_bounded_against_a_page_controlled_payload():
    """The log is the one artifact that makes a provider failure explainable.

    Lists were already bounded; dictionaries were not. Measured before this
    cap: a 50,000-key payload produced a 1 MB log line against a 512 KiB
    rotation, so a single poisoned scrape discarded the user's existing
    diagnostics - losing the evidence is the expensive part, not the noise.
    """
    from aigauge.app import _raw_summary

    hostile = {"/evil": {"planted": "ATTACKER-CONTROLLED-STRING"}}
    hostile.update({f"flood{i}": i for i in range(50000)})

    line = _raw_summary({"api": hostile})

    assert len(line) < 20_000, f"log line was {len(line)} bytes"
    assert "more keys" in line, "truncation must be visible, not silent"


def _errored(provider: str, *, metrics=()) -> UsageSnapshot:
    return UsageSnapshot(
        provider=provider,
        status=SnapshotStatus.ERROR,
        error="extractor retry limit exceeded",
        metrics=list(metrics),
    )


def test_an_error_with_no_metrics_earns_the_fast_retry():
    """The cold-start case, and the one that was missed.

    The fast retry used to require the errored snapshot to still carry stale
    metrics - so a provider that had never succeeded this run, showing nothing
    at all, waited a full interval, while one showing a stale number was
    retried within the minute. That is backwards, and it is exactly what a
    fresh launch produces: Claude's settings page resolves eight endpoints
    before requesting usage, and on a cold cache the first scrape can exceed
    its budget. Every restart therefore showed a broken tile for five minutes,
    at the moment a user is most likely to be looking.
    """
    app = _schedule_app_stub()
    app._snapshots = {"claude": _errored("claude")}  # noqa: SLF001

    app._schedule_next_refresh()  # noqa: SLF001

    # One minute, not the five-minute active interval.
    assert app._timer.started_ms is not None  # noqa: SLF001
    assert app._timer.started_ms <= 65_000, (  # noqa: SLF001
        f"waited {app._timer.started_ms}ms before retrying a failed provider"  # noqa: SLF001
    )


def test_an_error_that_kept_stale_metrics_still_earns_it():
    # Pre-existing behaviour must survive the generalisation.
    app = _schedule_app_stub()
    metric = UsageMetric(label="Session", percent_used=42.0)
    app._snapshots = {"claude": _errored("claude", metrics=[metric])}  # noqa: SLF001

    app._schedule_next_refresh()  # noqa: SLF001

    assert app._timer.started_ms <= 65_000  # noqa: SLF001


def test_a_clean_cycle_uses_the_normal_cadence():
    app = _schedule_app_stub()
    app._snapshots = {  # noqa: SLF001
        "claude": UsageSnapshot(provider="claude", status=SnapshotStatus.OK)
    }

    app._schedule_next_refresh()  # noqa: SLF001

    assert app._timer.started_ms > 65_000, "healthy providers must not be hammered"  # noqa: SLF001


def test_auth_required_is_not_retried_quickly():
    # Signing in is the user's move; retrying every minute only burns page
    # loads against a provider that will keep saying no.
    app = _schedule_app_stub()
    app._snapshots = {  # noqa: SLF001
        "claude": UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in to Claude.",
        )
    }

    app._schedule_next_refresh()  # noqa: SLF001

    assert app._timer.started_ms > 65_000  # noqa: SLF001


def test_a_persistently_broken_provider_stops_being_hammered():
    """The bound. A transient failure deserves a fast retry; a broken one does
    not deserve one every minute forever."""
    app = _schedule_app_stub()
    app._snapshots = {"claude": _errored("claude")}  # noqa: SLF001

    app._consecutive_error_cycles = 3  # noqa: SLF001
    app._schedule_next_refresh()  # noqa: SLF001
    assert app._timer.started_ms <= 65_000, "gave up while still within the bound"  # noqa: SLF001

    app._consecutive_error_cycles = 4  # noqa: SLF001
    app._schedule_next_refresh()  # noqa: SLF001
    assert app._timer.started_ms > 65_000, "kept retrying past the bound"  # noqa: SLF001


def test_consecutive_error_cycles_reset_once_a_cycle_comes_back_clean():
    """The bound must be a backoff, not a permanent demotion.

    Without the reset, a provider that failed past the bound and later
    recovered would never earn a fast retry again for the life of the process.
    """
    app = _schedule_app_stub()

    app._snapshots = {"claude": _errored("claude")}  # noqa: SLF001
    for expected in (1, 2, 3):
        app._record_cycle_outcome()  # noqa: SLF001
        assert app._consecutive_error_cycles == expected  # noqa: SLF001

    app._snapshots = {  # noqa: SLF001
        "claude": UsageSnapshot(provider="claude", status=SnapshotStatus.OK)
    }
    app._record_cycle_outcome()  # noqa: SLF001

    assert app._consecutive_error_cycles == 0  # noqa: SLF001


def test_an_auth_required_cycle_does_not_count_as_a_failing_one():
    # It is not a transient failure to back off from; it needs the user.
    app = _schedule_app_stub()
    app._consecutive_error_cycles = 2  # noqa: SLF001
    app._snapshots = {  # noqa: SLF001
        "claude": UsageSnapshot(
            provider="claude", status=SnapshotStatus.AUTH_REQUIRED, error="x"
        )
    }

    app._record_cycle_outcome()  # noqa: SLF001

    assert app._consecutive_error_cycles == 0  # noqa: SLF001


def test_the_app_can_actually_be_constructed(qapp, tmp_path, monkeypatch):
    """Constructs the real App, which nothing else here does.

    Every other test in this file uses ``App.__new__(App)`` and sets the
    attributes it needs by hand. That skips ``__init__`` entirely, so an
    attribute read during startup but never assigned there is invisible to the
    whole suite - and to CI.

    It shipped exactly that way: `_consecutive_error_cycles` was read by
    `_error_retry_time`, reached from `__init__` via `_restart_timer`, but the
    assignment landed outside `__init__`. 609 tests passed, six CI jobs passed,
    and the app raised AttributeError before its window appeared. The stand-in
    stubs had the attribute set, so they proved the logic and hid the wiring.

    This is deliberately a smoke test rather than a targeted one: it fails for
    *any* attribute the startup path reads and `__init__` does not provide.
    """
    import aigauge.app as app_module
    import aigauge.config as config_module

    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)

    app = App()

    # __init__ already reaches _schedule_next_refresh via _restart_timer; call
    # it again explicitly so the failing path is named in the test, not just
    # traversed by construction.
    app._schedule_next_refresh()  # noqa: SLF001

    assert app._consecutive_error_cycles == 0  # noqa: SLF001
