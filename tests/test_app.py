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
    _snapshot_signature,
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
        self.loading_kwargs = []
        self.refreshing = []
        self.refresh_state_calls = []
        self.progress = []
        self.visible = True

    def set_refreshing(self, refreshing, *, total=None):
        self.refreshing.append(refreshing)

    def set_refresh_progress(self, done, total=None):
        self.progress.append((done, total))

    def mark_loading(self, providers, *, subtle=False):
        self.loading_calls.append(providers)
        self.loading_kwargs.append({"subtle": subtle})

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
    # Browser-backed: these are the ones that queue rather than going out
    # together, which is what the queueing assertions below are about.
    app._providers = {  # noqa: SLF001
        "claude": SimpleNamespace(uses_browser=True),
        "codex": SimpleNamespace(uses_browser=True),
    }
    app._inflight = set()  # noqa: SLF001
    app._refresh_queue = []  # noqa: SLF001
    app._active_until = datetime.now() - timedelta(minutes=1)  # noqa: SLF001
    app._unchanged_cycles = 3  # noqa: SLF001
    app._timer = _Timer()  # noqa: SLF001
    app._current_refresh_manual = False  # noqa: SLF001
    app._cycle_signatures = {"old": ()}  # noqa: SLF001
    app._cycle_statuses = {}  # noqa: SLF001
    app._cycle_names = set()  # noqa: SLF001
    app._cycle_active = False  # noqa: SLF001
    app._cycle_started_at = None  # noqa: SLF001
    app._cycle_reason = "startup"  # noqa: SLF001
    app._dispatch_times = {}  # noqa: SLF001
    app._dispatch_epoch = {}  # noqa: SLF001
    app._abandoned = {}  # noqa: SLF001
    app._pool_wait_budgets = {}  # noqa: SLF001
    app._pending_profile_purges = []  # noqa: SLF001
    app._dispatching = False  # noqa: SLF001
    app._watchdogs = {}  # noqa: SLF001
    app._pending_manual_refresh = False  # noqa: SLF001
    app._pending_manual_providers = []  # noqa: SLF001
    app._next_refresh_reason = "startup"  # noqa: SLF001
    app._widget = _Widget()  # noqa: SLF001
    app._start_next_refresh = lambda: None  # noqa: SLF001
    app._config = Config()  # noqa: SLF001
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


def test_scheduled_refresh_marks_its_tiles_more_quietly():
    """A scheduled cycle used to show nothing at all - mark_loading was
    manual-only - so with cycles running a median of 48 s the only evidence a
    refresh was happening was tiles changing one at a time while the header
    said "next now"."""
    app = _refresh_app_stub()

    app.refresh_now(manual=False)

    assert app._widget.loading_calls == [  # noqa: SLF001
        {"claude": "Claude", "codex": "Codex"}
    ]
    assert app._widget.loading_kwargs == [{"subtle": True}]  # noqa: SLF001
    assert app._refresh_queue == ["claude", "codex"]  # noqa: SLF001
    # A scheduled cycle must not re-arm the active window or zero the backoff.
    assert app._unchanged_cycles == 3  # noqa: SLF001


def test_manual_refresh_dims_its_tiles_outright():
    app = _refresh_app_stub()

    app.refresh_now(manual=True)

    assert app._widget.loading_kwargs == [{"subtle": False}]  # noqa: SLF001


def test_refresh_order_prioritizes_openrouter_without_reordering_tiles():
    providers = {
        "claude": object(),
        "codex": object(),
        "copilot": object(),
        "openrouter": object(),
    }

    assert _refresh_provider_order(providers) == [
        "openrouter",
        "copilot",
        "claude",
        "codex",
    ]


def test_refresh_order_puts_both_cheap_rest_providers_first():
    """Azure is a handful of JSON calls and self-throttles to one live fetch
    per hour, so it costs nothing to refresh early - and an early tile fills
    while a Claude/Codex scrape is still loading a page."""
    providers = {
        "claude": object(),
        "copilot": object(),
        "azure": object(),
        "openrouter": object(),
    }

    assert _refresh_provider_order(providers) == [
        "openrouter",
        "azure",
        "copilot",
        "claude",
    ]


def test_refresh_order_puts_copilot_with_the_cheap_rest_providers():
    """Copilot is five plain HTTPS calls, and it used to run behind every
    browser scrape because _build_providers inserts browser accounts first.
    In 4.5 days of desktop log its payload line landed near the end of cycles
    with a median duration of 48 s - for an answer that takes about a second.
    """
    providers = {
        "claude": object(),
        "codex": object(),
        "copilot": object(),
        "openrouter": object(),
        "azure": object(),
    }

    assert _refresh_provider_order(providers) == [
        "openrouter",
        "azure",
        "copilot",
        "claude",
        "codex",
    ]


def test_browser_account_enabled_is_not_a_switch():
    """`BrowserAccount.enabled` is parsed and then ignored, deliberately.

    Nothing in the app ever writes it. The one place that ever set it to
    anything but the default is the config migration, which stamps
    `bool(providers.<kind>)` when it inserts a missing fixed account - so a
    config migrated while `providers.claude` was false would carry
    `enabled: false` forever, and the Settings checkbox, which only flips
    `providers.claude`, could never undo it. Honouring the field made that
    checkbox a permanent no-op. The provider toggle is the only switch.
    """
    config = Config()
    for account in config.browser_accounts:
        account.enabled = False
    config.browser_accounts.append(
        BrowserAccount(id="claude-team", kind="claude", name="Team", enabled=False)
    )

    enabled = _enabled_providers(config)

    assert "claude" in enabled
    assert "codex" in enabled
    assert "claude-team" in enabled

    config.providers.claude = False
    enabled = _enabled_providers(config)
    assert "claude" not in enabled
    assert "claude-team" not in enabled
    assert "codex" in enabled


def test_a_legacy_config_with_no_accounts_is_still_listed_as_enabled():
    """The name used to promise tiles, and only `_enabled_providers` was
    checked.

    `_build_providers` has no counterpart to this fallback: with
    `browser_accounts == []` it builds copilot alone, so the tray and the
    menu-bar item would iterate two names that can never have a snapshot.
    That state is unreachable through `Config.load()` - the migration always
    re-inserts both fixed accounts - which is why the fallback is left as it
    is; the test should not claim more than it checks.
    """
    config = Config()
    config.browser_accounts = []

    assert _enabled_providers(config)[:2] == ("claude", "codex")


def test_enabled_providers_places_azure_next_to_copilot():
    config = Config()
    config.providers.azure = True

    enabled = _enabled_providers(config)
    assert "azure" in enabled
    assert abs(enabled.index("azure") - enabled.index("copilot")) == 1


def test_enabled_providers_omits_azure_by_default():
    # Azure needs an app registration before it can report anything; an
    # on-by-default tile would show every user an auth error they never asked for.
    assert "azure" not in _enabled_providers(Config())


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
    app._error_retry = {  # noqa: SLF001
        "claude": (2, datetime.now() + timedelta(minutes=1))
    }
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
    assert context["error_cycles"] == "claude:2"


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
    app._error_retry = {}  # noqa: SLF001
    # A retry wake asks whether each due provider is parked before it spends
    # that provider's deadline on a dispatch it cannot make.
    app._abandoned = {}  # noqa: SLF001
    app._providers = {"claude": object(), "codex": object()}  # noqa: SLF001
    app._next_refresh_reason = "startup"  # noqa: SLF001
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


def test_schedule_pulls_a_failed_providers_refresh_forward():
    app = _schedule_app_stub()
    app._record_provider_outcome(  # noqa: SLF001
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.ERROR,
            error="Could not read usage from page.",
            metrics=[UsageMetric(label="Session", percent_used=80.0)],
        )
    )

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
    app._record_provider_outcome(_errored("claude"))  # noqa: SLF001

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
    app._record_provider_outcome(_errored("claude", metrics=[metric]))  # noqa: SLF001

    app._schedule_next_refresh()  # noqa: SLF001

    assert app._timer.started_ms <= 65_000  # noqa: SLF001


def test_a_clean_cycle_uses_the_normal_cadence():
    app = _schedule_app_stub()
    app._record_provider_outcome(  # noqa: SLF001
        UsageSnapshot(provider="claude", status=SnapshotStatus.OK)
    )

    app._schedule_next_refresh()  # noqa: SLF001

    assert app._timer.started_ms > 65_000, "healthy providers must not be hammered"  # noqa: SLF001


def _snapshot_with(*metrics: UsageMetric) -> UsageSnapshot:
    return UsageSnapshot(
        provider="claude", status=SnapshotStatus.OK, metrics=list(metrics)
    )


def test_the_cadence_signature_ignores_informational_meters():
    """Otherwise every breakdown row can reset the adaptive backoff.

    A provider that renders a dozen tagged meters offers a dozen numbers that
    twitch on their own; the cadence should follow the meters the tile is
    about.
    """
    primary = UsageMetric(label="Session", percent_used=64.0)
    quiet = _snapshot_with(primary, UsageMetric(label="Opus only", percent_used=91.0,
                                                tag="meter_breakdown"))
    moved = _snapshot_with(primary, UsageMetric(label="Opus only", percent_used=92.0,
                                                tag="meter_breakdown"))

    assert _snapshot_signature(quiet) == _snapshot_signature(moved)


def test_a_caption_change_alone_is_not_a_cadence_change():
    """reset_label is a caption - a ticking countdown, or Azure's spend to the
    cent. Either one reset the adaptive backoff for *every* provider and
    pushed the whole app back into active-cadence polling."""
    before = _snapshot_with(
        UsageMetric(label="Spend this month", percent_used=24.0,
                    reset_label="CAD 36.10 of 150.00 · resets 1 Oct")
    )
    after = _snapshot_with(
        UsageMetric(label="Spend this month", percent_used=24.0,
                    reset_label="CAD 36.42 of 150.00 · resets 1 Oct")
    )

    assert _snapshot_signature(before) == _snapshot_signature(after)


def test_a_changing_error_message_is_not_a_cadence_change():
    """The signature hashed snapshot.error verbatim.

    Azure's fail-closed message counts a minute down - "Waiting for the next
    Azure fetch window (43 min)" - so it differed on every single cycle, which
    read as "this provider changed": the 30-minute active window was re-armed
    and _unchanged_cycles was zeroed, for every provider, by a clock. The log
    shows the result: unchanged_cycles was 0 in 55 of 204 heartbeats and only
    ever reached 9-12 in a few overnight stretches, so the documented idle
    backoff almost never engaged. The status is what the cadence is about.
    """
    before = UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.ERROR,
        error="Waiting for the next Azure fetch window (43 min).",
    )
    after = UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.ERROR,
        error="Waiting for the next Azure fetch window (42 min).",
    )

    assert _snapshot_signature(before) == _snapshot_signature(after)


def test_a_provider_that_starts_failing_is_a_cadence_change():
    ok = UsageSnapshot(provider="claude", status=SnapshotStatus.OK)
    broken = UsageSnapshot(
        provider="claude", status=SnapshotStatus.ERROR, error="boom"
    )

    assert _snapshot_signature(ok) != _snapshot_signature(broken)


def test_the_cadence_signature_still_follows_the_primary_meters():
    before = _snapshot_with(UsageMetric(label="Session", percent_used=64.0))
    after = _snapshot_with(UsageMetric(label="Session", percent_used=65.0))

    assert _snapshot_signature(before) != _snapshot_signature(after)


def test_auth_required_is_not_retried_quickly():
    # Signing in is the user's move; retrying every minute only burns page
    # loads against a provider that will keep saying no.
    app = _schedule_app_stub()
    app._record_provider_outcome(  # noqa: SLF001
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in to Claude.",
        )
    )

    app._schedule_next_refresh()  # noqa: SLF001

    assert app._timer.started_ms > 65_000  # noqa: SLF001


def test_the_fast_retry_backs_off_one_two_four_minutes():
    """Three attempts, doubling, then the normal cadence.

    A flat one-minute retry is what the desktop log caught in the act: during
    a 34-minute offline burst every provider was re-scraped every minute, and
    Claude alone takes a median of 18.5 s and a p90 of 48 s of browser time
    per attempt - so the app was essentially always refreshing.
    """
    app = _schedule_app_stub()

    for ceiling_ms in (65_000, 125_000, 245_000):
        app._record_provider_outcome(_errored("claude"))  # noqa: SLF001
        app._schedule_next_refresh()  # noqa: SLF001
        assert app._timer.started_ms <= ceiling_ms  # noqa: SLF001
        assert app._timer.started_ms > (ceiling_ms - 5_000) // 2  # noqa: SLF001


def test_a_persistently_broken_provider_stops_being_hammered():
    """The bound. A transient failure deserves a fast retry; a broken one does
    not deserve one every minute forever.

    OpenCode never succeeded once in 4.5 days: 95 AUTH_REQUIRED and 38 ERROR
    snapshots, every one of them costing about 6 s of browser time.
    """
    app = _schedule_app_stub()

    for _ in range(3):
        app._record_provider_outcome(_errored("claude"))  # noqa: SLF001
    app._schedule_next_refresh()  # noqa: SLF001
    assert app._timer.started_ms <= 245_000, "gave up while still within the bound"  # noqa: SLF001

    app._record_provider_outcome(_errored("claude"))  # noqa: SLF001
    app._schedule_next_refresh()  # noqa: SLF001
    assert app._timer.started_ms > 245_000, "kept retrying past the bound"  # noqa: SLF001


def test_a_broken_provider_no_longer_drags_the_healthy_ones_with_it():
    """The point of the whole change.

    The retry used to be cycle-wide: any ERROR snapshot meant the *next
    cycle* ran in one minute, for every provider. 124 of 137 cycles in the
    user's log contained at least one ERROR or AUTH_REQUIRED, and a third of
    all cycles started within two minutes of the previous one - so a single
    broken tile put five healthy providers on a one-minute cadence.
    """
    app = _schedule_app_stub()
    app._record_provider_outcome(_errored("claude"))  # noqa: SLF001
    app._record_provider_outcome(  # noqa: SLF001
        UsageSnapshot(provider="codex", status=SnapshotStatus.OK)
    )

    app._schedule_next_refresh()  # noqa: SLF001

    assert app._timer.started_ms <= 65_000, "the failing provider lost its retry"  # noqa: SLF001
    assert app._due_error_providers(  # noqa: SLF001
        datetime.now() + timedelta(minutes=2)
    ) == ["claude"], "a healthy provider was pulled into the retry"


def test_a_provider_that_recovers_earns_its_fast_retry_back():
    """The bound must be a backoff, not a permanent demotion.

    Without the reset, a provider that failed past the bound and later
    recovered would never earn a fast retry again for the life of the process
    - and under the old cycle-wide counter, one permanently broken provider
    meant no cycle was ever clean, so nothing could reset it for anyone.
    """
    app = _schedule_app_stub()
    for _ in range(4):
        app._record_provider_outcome(_errored("claude"))  # noqa: SLF001
    app._schedule_next_refresh()  # noqa: SLF001
    assert app._timer.started_ms > 245_000  # noqa: SLF001

    app._record_provider_outcome(  # noqa: SLF001
        UsageSnapshot(provider="claude", status=SnapshotStatus.OK)
    )
    assert "claude" not in app._error_retry  # noqa: SLF001

    app._record_provider_outcome(_errored("claude"))  # noqa: SLF001
    app._schedule_next_refresh()  # noqa: SLF001
    assert app._timer.started_ms <= 65_000  # noqa: SLF001


def test_an_auth_required_snapshot_clears_that_providers_error_streak():
    # It is not a transient failure to back off from; it needs the user.
    app = _schedule_app_stub()
    app._record_provider_outcome(_errored("claude"))  # noqa: SLF001

    app._record_provider_outcome(  # noqa: SLF001
        UsageSnapshot(
            provider="claude", status=SnapshotStatus.AUTH_REQUIRED, error="x"
        )
    )

    assert "claude" not in app._error_retry  # noqa: SLF001


def test_a_suspend_artifact_is_not_a_failure_either():
    """A scrape whose clock ran across a machine suspend reports `timeout`
    with a nonsense elapsed. It is not evidence that the provider is broken,
    and it used to arm the fast retry once per provider on every resume."""
    app = _schedule_app_stub()

    app._record_provider_outcome(  # noqa: SLF001
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.ERROR,
            error="timeout",
            error_class="resume_artifact",
        )
    )

    app._schedule_next_refresh()  # noqa: SLF001

    assert "claude" not in app._error_retry  # noqa: SLF001
    assert app._timer.started_ms > 65_000  # noqa: SLF001


def test_a_provider_waiting_on_its_own_throttle_is_not_a_failure():
    """Azure fails closed inside its hourly window: with nothing cached it
    returns an ERROR snapshot naming the wait. Counting that as a failure put
    the app on the fast retry for a provider that is deliberately not
    fetching - and the countdown in the message changed every minute, so it
    also read as "this provider changed"."""
    app = _schedule_app_stub()
    app._providers["azure"] = object()  # noqa: SLF001

    app._record_provider_outcome(  # noqa: SLF001
        UsageSnapshot(
            provider="azure",
            status=SnapshotStatus.ERROR,
            error="Waiting for the next Azure fetch window (43 min).",
            error_class="throttled",
        )
    )

    app._schedule_next_refresh()  # noqa: SLF001

    assert "azure" not in app._error_retry  # noqa: SLF001
    assert app._timer.started_ms > 65_000  # noqa: SLF001


def test_a_hostile_payload_cannot_flood_the_snapshot_log_line():
    """`raw_summary` was bounded and `raw_keys` was not.

    `snapshot.raw` on a browser provider is the extractor's own dict, so its
    key names come off the provider page. One payload with tens of thousands
    of keys is a megabyte-long record against a 512 KiB x 3 rotation.
    """
    from aigauge.app import _raw_keys_for_log

    line = _raw_keys_for_log({f"{'k' * 500}{index}": index for index in range(5000)})

    assert len(line) < 4000, f"one raw_keys field was {len(line)} bytes"
    assert "more" in line, "the count of what was left out is the diagnostic bit"
    assert _raw_keys_for_log(None) == "[]"
    assert _raw_keys_for_log({"a": 1}) == "['a']"


def test_which_providers_are_browser_backed_is_pinned():
    """Nothing asserted this, so flipping `ClaudeProvider.uses_browser` to
    False passed the whole suite - while the app would then dispatch Claude
    alongside Codex, two `QWebEngineView`s on two profiles at once, on the
    GUI thread. That is precisely what the concurrent-REST change was scoped
    not to do, and `_uses_browser` is the only thing that keeps the browser
    queue serial.

    The reverse matters too: a REST provider mislabelled as browser-backed
    would be dragged into the serial queue and lose the whole point of
    dispatching the cheap ones together.
    """
    from aigauge.providers.azure import AzureProvider
    from aigauge.providers.claude import ClaudeProvider
    from aigauge.providers.codex import CodexProvider
    from aigauge.providers.copilot import CopilotProvider
    from aigauge.providers.opencode_go import OpenCodeGoProvider
    from aigauge.providers.openrouter import OpenRouterProvider

    for cls in (ClaudeProvider, CodexProvider, OpenCodeGoProvider):
        assert cls.uses_browser is True, f"{cls.__name__} lost its browser queue"
    for cls in (CopilotProvider, OpenRouterProvider, AzureProvider):
        assert cls.uses_browser is False, f"{cls.__name__} joined the serial queue"


def test_a_browser_provider_refuses_a_refresh_while_one_is_running(monkeypatch):
    """The last line of defence for one profile, one scrape.

    The App parks a provider its watchdog gave up on, but it cannot park it
    forever - a worker that is genuinely dead would stall that tile for the
    life of the process - so after twice its budget the name is eligible
    again. If that worker is in fact still loading a page,
    `ClaudeProvider.refresh` would rebuild its runner unconditionally and put
    a second `QWebEngineView` on the single cached `QWebEngineProfile` for
    the account: two writers to one cookie store. It refuses instead, and
    says so with an error class the scheduler does not read as a failure.

    The live scrape is registered by account id, not on the provider object,
    so the refusal also survives the `_build_providers()` that every settings
    save runs.
    """
    import aigauge.providers.claude as claude_module
    import aigauge.providers.codex as codex_module
    import aigauge.providers.opencode_go as opencode_module
    from aigauge.providers import _scrape_runner as runner_module

    config = Config()
    cases = [
        ("claude", claude_module, claude_module.ClaudeProvider(
            parent=None, account_id="claude", config=config)),
        ("codex", codex_module, codex_module.CodexProvider(
            parent=None, account_id="codex", config=config)),
        ("opencode_go", opencode_module,
         opencode_module.OpenCodeGoProvider(config, parent=None)),
    ]
    for account_id, module, provider in cases:
        # Stand in for ScrapeRunner so a regression fails the assertion below
        # instead of constructing a real QWebEngineView.
        built: list = []
        monkeypatch.setattr(
            module, "ScrapeRunner", lambda **kwargs: built.append(kwargs) or
            SimpleNamespace(run=lambda on_done: None, busy=lambda: True)
        )
        answers: list[UsageSnapshot] = []
        runner_module._mark_account_busy(account_id, 240.0)  # noqa: SLF001
        try:
            provider.refresh(answers.append)
        finally:
            runner_module._release_account(account_id)  # noqa: SLF001

        name = type(provider).__name__
        assert built == [], f"{name} started a second scrape on one profile"
        assert len(answers) == 1, f"{name} did not answer"
        assert answers[0].status == SnapshotStatus.ERROR
        assert answers[0].error_class == "throttled", (
            "a provider that is already working is not a provider that failed"
        )
        assert provider._runner is None, (  # noqa: SLF001
            f"{name} replaced its runner while refusing"
        )


def test_a_throttled_answer_clears_a_retry_that_was_already_owed():
    """The early return for a no-fast-retry class left the old `due` behind.

    The test above starts from an empty `_error_retry`, so its assertion
    holds trivially. A provider arrives here with an entry: a real failure is
    what armed one. `_error_retry_time` clamps a `due` that has passed to
    *now*, `_schedule_next_refresh` floors the delay at 1 000 ms, and the wake
    produces the same throttled answer - a 1 Hz cycle loop for as long as the
    throttle lasts.
    """
    app = _schedule_app_stub()
    app._providers["azure"] = object()  # noqa: SLF001
    app._error_retry["azure"] = (  # noqa: SLF001
        1,
        datetime.now() - timedelta(seconds=1),
    )

    app._record_provider_outcome(  # noqa: SLF001
        UsageSnapshot(
            provider="azure",
            status=SnapshotStatus.ERROR,
            error="Waiting for the next Azure fetch window (43 min).",
            error_class="throttled",
        )
    )
    app._schedule_next_refresh()  # noqa: SLF001

    assert "azure" not in app._error_retry  # noqa: SLF001
    assert app._timer.started_ms > 65_000, "the scheduler was left on a 1 s loop"  # noqa: SLF001


def test_a_retry_wake_consumes_the_due_it_ran_on():
    """A due is spent when it is dispatched, not when an answer clears it.

    Waiting for the answer means any answer that does not clear the entry -
    a throttle, a resume artifact, a provider removed between the wake and
    the dispatch - leaves a past due in place, and a past due pins the next
    wake at the timer floor.
    """
    app = _schedule_app_stub()
    app._providers = {"claude": object()}  # noqa: SLF001
    app._error_retry["claude"] = (  # noqa: SLF001
        2,
        datetime.now() - timedelta(seconds=1),
    )
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    begun = []
    app._begin_cycle = lambda names, **kw: begun.append(list(names))  # noqa: SLF001

    app._on_refresh_timer()  # noqa: SLF001

    assert begun == [["claude"]]
    errors, due = app._error_retry["claude"]  # noqa: SLF001
    assert errors == 2, "the streak is what bounds the ladder; it must survive"
    assert due is None, "the due was still owed after the wake that spent it"


def test_a_retry_wake_that_finds_nothing_due_does_not_become_a_full_cycle():
    """`_due_error_providers` tests `due <= now`. A Qt::CoarseTimer rounds its
    expiry and may fire a few milliseconds early, and the error may have been
    cleared between arming the timer and the wake. Falling through to
    `refresh_now` re-ran the whole queue - the cycle-wide retry this release
    removed.
    """
    app = _schedule_app_stub()
    app._providers = {"claude": object(), "codex": object()}  # noqa: SLF001
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    begun = []
    app._begin_cycle = lambda names, **kw: begun.append(list(names))  # noqa: SLF001

    # Nothing is owed at all: the provider recovered after the timer armed.
    app._on_refresh_timer()  # noqa: SLF001
    assert begun == []
    assert app._timer.started_ms is not None, "the scheduler was left unarmed"

    # A due 20 ms out is this wake's, not a reason to refresh everyone.
    app._error_retry["claude"] = (  # noqa: SLF001
        1,
        datetime.now() + timedelta(milliseconds=20),
    )
    app._next_refresh_reason = "error_retry"  # noqa: SLF001
    app._on_refresh_timer()  # noqa: SLF001
    assert begun == [["claude"]]


def test_the_watchdog_timers_do_not_accumulate(qapp):
    """`_arm_watchdog` builds a `QTimer(self)`; `_cancel_watchdog` stopped it
    and dropped the Python reference, but the C++ object stays parented to
    `App` for the life of the process. At ~180 dispatches a day in a tray app
    designed to run for weeks, that is tens of thousands of live QObjects.
    """
    from PyQt6.QtCore import QCoreApplication, QEvent, QObject, QTimer

    app = App.__new__(App)
    QObject.__init__(app)
    app._config = Config()  # noqa: SLF001
    app._inflight = set()  # noqa: SLF001
    app._watchdogs = {}  # noqa: SLF001
    app._abandoned = {}  # noqa: SLF001
    app._dispatch_epoch = {}  # noqa: SLF001
    app._pool_wait_budgets = {}  # noqa: SLF001
    app._pending_profile_purges = []  # noqa: SLF001
    provider = SimpleNamespace(uses_browser=False, refresh_budget_seconds=60.0)
    app._providers = {"copilot": provider}  # noqa: SLF001

    for _ in range(200):
        for name in ("copilot", "openrouter", "azure", "claude", "codex", "opencode_go"):
            app._arm_watchdog(name, provider)  # noqa: SLF001
            app._cancel_watchdog(name)  # noqa: SLF001
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert app._watchdogs == {}  # noqa: SLF001
    assert len(app.findChildren(QTimer)) < 50, (
        "watchdog timers are accumulating as children of App"
    )


def test_the_app_can_actually_be_constructed(qapp, tmp_path, monkeypatch):
    """Constructs the real App, which nothing else here does.

    Every other test in this file uses ``App.__new__(App)`` and sets the
    attributes it needs by hand. That skips ``__init__`` entirely, so an
    attribute read during startup but never assigned there is invisible to the
    whole suite - and to CI.

    It shipped exactly that way: the error-retry state was read by
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

    assert app._error_retry == {}  # noqa: SLF001


class _SnapshotWidget:
    """Just enough widget for _on_snapshot."""

    def __init__(self):
        self.snapshots = []

    def update_snapshot(self, snapshot, display_name):
        self.snapshots.append((snapshot, display_name))

    def set_ratio(self, *args, **kwargs):
        pass


def test_the_snapshot_error_log_line_redacts_azure_identifiers(qapp, caplog):
    """ai-gauge.log is the file the error dialog's own "Open log folder"
    button points at, and Copy diagnostics is the only exit that was redacted.
    A requests transport error stringifies with the whole request URL."""
    import logging

    app = App.__new__(App)
    app._snapshots = {}  # noqa: SLF001
    app._cycle_signatures = {}  # noqa: SLF001
    app._cycle_statuses = {}  # noqa: SLF001
    app._cycle_names = set()  # noqa: SLF001
    app._cycle_active = False  # noqa: SLF001
    app._dispatch_times = {}  # noqa: SLF001
    app._dispatch_epoch = {}  # noqa: SLF001
    app._abandoned = {}  # noqa: SLF001
    app._pool_wait_budgets = {}  # noqa: SLF001
    app._pending_profile_purges = []  # noqa: SLF001
    app._dispatching = False  # noqa: SLF001
    app._watchdogs = {}  # noqa: SLF001
    app._error_retry = {}  # noqa: SLF001
    app._inflight = set()  # noqa: SLF001
    app._providers = {"azure": object()}  # noqa: SLF001
    app._config = Config()  # noqa: SLF001
    app._widget = _SnapshotWidget()  # noqa: SLF001
    app._history = SimpleNamespace(record_snapshot=lambda snap: None)  # noqa: SLF001
    app._ratio = SimpleNamespace(  # noqa: SLF001
        record_snapshot=lambda snap: None,
        display_estimate=lambda provider: None,
        current_estimate=lambda provider: None,
    )
    app._ratio_recent = lambda provider: []  # noqa: SLF001
    app._refresh_queue = ["claude"]  # noqa: SLF001
    app._start_next_refresh = lambda: None  # noqa: SLF001
    app._update_tray = lambda: None  # noqa: SLF001

    sub = "11111111-2222-3333-4444-555555555555"
    snapshot = UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.ERROR,
        error=(
            "Azure request failed: HTTPSConnectionPool(host='management.azure.com'"
            f", port=443): Max retries exceeded with url: /subscriptions/{sub}"
            "/providers/Microsoft.CostManagement/query"
        ),
    )

    with caplog.at_level(logging.WARNING):
        app._on_snapshot(snapshot)  # noqa: SLF001

    assert sub not in caplog.text
    assert "<guid>" in caplog.text


def _mid_cycle_app(widget) -> App:
    app = App.__new__(App)
    app._snapshots = {}  # noqa: SLF001
    app._cycle_signatures = {}  # noqa: SLF001
    app._cycle_statuses = {}  # noqa: SLF001
    app._cycle_names = {"claude", "codex"}  # noqa: SLF001
    app._cycle_total = 2  # noqa: SLF001
    app._cycle_active = True  # noqa: SLF001
    app._cycle_started_at = None  # noqa: SLF001
    app._dispatch_times = {}  # noqa: SLF001
    # A mid-cycle answer is a *dispatch* answer, so it arrives in the tuple
    # form `_dispatch` emits, matched against the epoch it was sent with. An
    # epoch-less snapshot for a provider that is still in flight is a
    # settings re-render, and only repaints (see App._repaint_snapshot).
    app._dispatch_epoch = {"claude": 1}  # noqa: SLF001
    app._abandoned = {}  # noqa: SLF001
    app._pool_wait_budgets = {}  # noqa: SLF001
    app._pending_profile_purges = []  # noqa: SLF001
    app._dispatching = False  # noqa: SLF001
    app._watchdogs = {}  # noqa: SLF001
    app._error_retry = {}  # noqa: SLF001
    app._inflight = {"claude"}  # noqa: SLF001
    app._refresh_queue = ["codex"]  # noqa: SLF001
    app._providers = {"claude": object(), "codex": object()}  # noqa: SLF001
    app._config = Config()  # noqa: SLF001
    app._widget = widget  # noqa: SLF001
    app._history = SimpleNamespace(record_snapshot=lambda snap: None)  # noqa: SLF001
    app._ratio = SimpleNamespace(  # noqa: SLF001
        record_snapshot=lambda snap: None,
        display_estimate=lambda provider: None,
        current_estimate=lambda provider: None,
    )
    app._ratio_recent = lambda provider: []  # noqa: SLF001
    app._start_next_refresh = lambda: None  # noqa: SLF001
    return app


class _TrayWidget(_SnapshotWidget):
    def __init__(self):
        super().__init__()
        self.progress = []

    def set_refresh_progress(self, done, total=None):
        self.progress.append((done, total))


def test_the_tray_keeps_up_with_the_tiles(qapp):
    """_update_tray ran only at the end of a cycle, so the tray dot and its
    tooltip were a whole cycle behind the tiles - a median of 48 s, and
    minutes on a failing cycle."""
    widget = _TrayWidget()
    app = _mid_cycle_app(widget)
    tray_calls = []
    app._update_tray = lambda: tray_calls.append(1)  # noqa: SLF001

    app._on_snapshot(  # noqa: SLF001
        (
            UsageSnapshot(
                provider="claude",
                status=SnapshotStatus.OK,
                metrics=[UsageMetric("Session", 50.0)],
            ),
            1,
        )
    )

    assert tray_calls, "the tray waited for the end of the cycle"


def test_the_header_is_told_how_far_through_the_cycle_it_is(qapp):
    widget = _TrayWidget()
    app = _mid_cycle_app(widget)
    app._update_tray = lambda: None  # noqa: SLF001

    app._on_snapshot(  # noqa: SLF001
        (UsageSnapshot(provider="claude", status=SnapshotStatus.OK), 1)
    )

    assert widget.progress == [(1, 2)], "the header was not counted forward"


def test_a_snapshot_for_a_provider_the_user_removed_is_dropped(qapp):
    """A settings save rebuilds the providers while a refresh is still out.

    Storing the late snapshot ran it through widget.update_snapshot, whose
    ensure_tile re-created the tile _build_providers had just deleted - so a
    provider the user had switched off reappeared until the next cycle.
    """
    app = App.__new__(App)
    app._snapshots = {}  # noqa: SLF001
    app._cycle_signatures = {}  # noqa: SLF001
    app._cycle_statuses = {}  # noqa: SLF001
    app._cycle_names = set()  # noqa: SLF001
    app._cycle_active = False  # noqa: SLF001
    app._dispatch_times = {}  # noqa: SLF001
    app._dispatch_epoch = {"opencode_go": 1}  # noqa: SLF001
    app._abandoned = {}  # noqa: SLF001
    app._pool_wait_budgets = {}  # noqa: SLF001
    app._pending_profile_purges = []  # noqa: SLF001
    app._dispatching = False  # noqa: SLF001
    app._watchdogs = {}  # noqa: SLF001
    app._inflight = {"opencode_go"}  # noqa: SLF001
    app._refresh_queue = []  # noqa: SLF001
    app._providers = {"claude": object()}  # noqa: SLF001
    app._config = Config()  # noqa: SLF001
    app._widget = _SnapshotWidget()  # noqa: SLF001

    app._on_snapshot(  # noqa: SLF001
        (
            UsageSnapshot(
                provider="opencode_go",
                status=SnapshotStatus.AUTH_REQUIRED,
                error="Not signed in to OpenCode",
            ),
            1,
        )
    )

    assert app._snapshots == {}, "a removed provider was stored anyway"  # noqa: SLF001
    assert app._widget.snapshots == [], "the removed tile was re-created"  # noqa: SLF001
    assert app._inflight == set()  # noqa: SLF001


class _RecordingTimer:
    """QTimer stand-in for tests that walk the dispatch path.

    _start_next_refresh arms a per-provider watchdog, and a real QTimer needs
    a constructed QObject parent - which App.__new__(App) deliberately is not.
    """

    def __init__(self, parent=None):
        self.timeout = self

    def connect(self, callback):
        pass

    def setSingleShot(self, value):
        pass

    def start(self, ms=None):
        pass

    def stop(self):
        pass

    @staticmethod
    def singleShot(ms, callback):
        callback()


def test_a_provider_that_raises_out_of_refresh_is_redacted_too(qapp, monkeypatch):
    """refresh() raising is turned into an ERROR snapshot here, and that
    string reaches the tile, the tray tooltip and the dialog header - none of
    which redact. The other exit from this method already redacts."""
    from types import SimpleNamespace as _NS

    import aigauge.app as app_module

    monkeypatch.setattr(app_module, "QTimer", _RecordingTimer)
    app = App.__new__(App)
    app._inflight = set()  # noqa: SLF001
    app._watchdogs = {}  # noqa: SLF001
    app._dispatch_times = {}  # noqa: SLF001
    app._dispatch_epoch = {}  # noqa: SLF001
    app._abandoned = {}  # noqa: SLF001
    app._pool_wait_budgets = {}  # noqa: SLF001
    app._pending_profile_purges = []  # noqa: SLF001
    app._cycle_started_at = None  # noqa: SLF001
    sub = "11111111-2222-3333-4444-555555555555"
    captured: list = []
    app._signals = _NS(  # noqa: SLF001
        snapshot_ready=_NS(emit=captured.append)
    )

    def _raise(_on_done):
        raise RuntimeError(
            "Max retries exceeded with url: /subscriptions/"
            f"{sub}/providers/Microsoft.CostManagement/query"
        )

    app._providers = {"azure": _NS(refresh=_raise)}  # noqa: SLF001
    app._refresh_queue = ["azure"]  # noqa: SLF001

    app._start_next_refresh()  # noqa: SLF001

    assert captured, "the exception was not turned into a snapshot"
    snapshot, epoch = captured[0]
    assert epoch == 1, "the answer must name the dispatch it answers"
    assert sub not in (snapshot.error or "")
    assert "<guid>" in (snapshot.error or "")


class _TileOnlyWidget:
    """Just enough widget for `_build_providers`."""

    def __init__(self):
        self._tiles: dict[str, str] = {}

    def ensure_tile(self, tile_id, display_name):
        self._tiles[tile_id] = display_name

    def remove_tile(self, tile_id):
        self._tiles.pop(tile_id, None)


def _provider_app(config: Config) -> App:
    app = App.__new__(App)
    app._config = config  # noqa: SLF001
    app._widget = _TileOnlyWidget()  # noqa: SLF001
    app._providers = {}  # noqa: SLF001
    app._snapshots = {}  # noqa: SLF001
    app._inflight = set()  # noqa: SLF001
    app._abandoned = {}  # noqa: SLF001
    app._error_retry = {}  # noqa: SLF001
    app._dispatch_times = {}  # noqa: SLF001
    app._dispatch_epoch = {}  # noqa: SLF001
    app._watchdogs = {}  # noqa: SLF001
    app._build_providers()  # noqa: SLF001
    return app


def test_a_settings_save_keeps_the_provider_objects_it_did_not_change():
    """`_build_providers` runs on every settings save, colour-only included.

    Rebuilding a browser provider threw away the `ScrapeRunner` that knows a
    scrape of that account is still loading a page - the one guard left once
    the App's park expires at twice the watchdog budget. Every provider reads
    `self._config` live, so a rebuilt instance differs from the one it
    replaced only in the state it just discarded.
    """
    config = Config()
    config.providers.openrouter = True
    config.providers.azure = True
    config.providers.opencode_go = True
    app = _provider_app(config)
    before = dict(app._providers)  # noqa: SLF001
    assert set(before) == {
        "claude",
        "codex",
        "copilot",
        "openrouter",
        "azure",
        "opencode_go",
    }

    app._build_providers()  # noqa: SLF001

    for name, provider in before.items():
        assert app._providers[name] is provider, (  # noqa: SLF001
            f"{name} was rebuilt although nothing about it changed"
        )


def test_a_park_does_not_outlive_the_provider_the_user_removed():
    """Nothing else clears `_abandoned` for a name the user turned off.

    `_dispatch_refusal` answers `not_configured` before it ever reaches
    `_is_abandoned`, so the entry - and the `_dispatch_times` /
    `_dispatch_epoch` rows the rebuild holds open for anything still parked -
    would live for the process. A name still in flight keeps its park: that
    dispatch is what it bounds.
    """
    config = Config()
    config.providers.openrouter = True
    app = _provider_app(config)
    far_future = 10.0**18
    app._abandoned["copilot"] = (3, far_future)  # noqa: SLF001
    app._dispatch_epoch["copilot"] = 3  # noqa: SLF001
    app._abandoned["openrouter"] = (1, far_future)  # noqa: SLF001
    app._dispatch_epoch["openrouter"] = 1  # noqa: SLF001
    app._inflight.add("openrouter")  # noqa: SLF001

    config.providers.copilot = False
    config.providers.openrouter = False
    app._build_providers()  # noqa: SLF001

    assert "copilot" not in app._abandoned, "a park outlived its provider"  # noqa: SLF001
    assert "copilot" not in app._dispatch_epoch  # noqa: SLF001
    assert "openrouter" in app._abandoned, (  # noqa: SLF001
        "a dispatch that is still out lost the park that bounds it"
    )
    assert "openrouter" in app._dispatch_epoch  # noqa: SLF001


def test_a_provider_the_user_switched_off_and_on_again_is_a_new_object():
    config = Config()
    app = _provider_app(config)
    first = app._providers["copilot"]  # noqa: SLF001

    config.providers.copilot = False
    app._build_providers()  # noqa: SLF001
    assert "copilot" not in app._providers  # noqa: SLF001

    config.providers.copilot = True
    app._build_providers()  # noqa: SLF001
    assert app._providers["copilot"] is not first  # noqa: SLF001


def test_an_accounts_kind_change_is_a_new_provider_object():
    """The reuse key is the account id *and* the exact class.

    Keeping the previous object for an id whose kind changed would point a
    Codex account at a `ClaudeProvider` - a scrape of the wrong page against
    the right profile. Not reachable through the UI, where ids are
    kind-prefixed; it is the check that makes the reuse safe, which is
    exactly why it is worth pinning.
    """
    from aigauge.config import BrowserAccount
    from aigauge.providers.claude import ClaudeProvider
    from aigauge.providers.codex import CodexProvider

    config = Config()
    config.browser_accounts = [BrowserAccount(id="x-1", kind="claude")]
    app = _provider_app(config)
    assert isinstance(app._providers["x-1"], ClaudeProvider)  # noqa: SLF001

    config.browser_accounts = [BrowserAccount(id="x-1", kind="codex")]
    app._build_providers()  # noqa: SLF001

    assert isinstance(app._providers["x-1"], CodexProvider), (  # noqa: SLF001
        "an account that changed kind kept the provider for the old one"
    )


def test_a_rebuilt_browser_provider_still_refuses_a_live_scrape():
    """End to end: the account is scraping, the settings save rebuilds the
    provider, and the fresh object still refuses. Nine concurrent
    `QWebEngineView`s on one cached `QWebEngineProfile` were measured over
    six hours of settings saves against a wedged scrape."""
    from aigauge.providers import _scrape_runner as runner_module

    config = Config()
    app = _provider_app(config)
    runner_module._mark_account_busy("claude", 240.0)  # noqa: SLF001 - live scrape
    try:
        app._build_providers()  # noqa: SLF001
        answers: list[UsageSnapshot] = []
        app._providers["claude"].refresh(answers.append)  # noqa: SLF001
    finally:
        runner_module._release_account("claude")  # noqa: SLF001

    assert len(answers) == 1
    assert answers[0].status == SnapshotStatus.ERROR
    assert answers[0].error == "A refresh is already running."
    assert answers[0].error_class == "throttled"


def _nested(fanout: int, depth: int):
    if depth == 0:
        return 1
    return {f"k{index}": _nested(fanout, depth - 1) for index in range(fanout)}


def test_raw_summary_caps_key_names_and_the_whole_record():
    """`raw_summary=` is the other half of the line `raw_keys=` was capped on.

    `_summarize_for_log` bounded the *number* of keys, the length of values
    and the depth, and never the length of a key name or the total size -
    and `snapshot.raw` on a browser provider is the extractor's own dict, so
    a page chooses both. Measured before this cap: one 100 000-character key
    name printed verbatim; a fan-out of 20 at depth 4 produced 2.2 MB; an
    api-capture-shaped payload that stays inside `api_capture.js`'s own caps
    produced 4.77 MB, which is 9x the whole 512 KiB rotation - one ERROR
    scrape erasing the diagnostic history the line exists to build.

    `error_dialog._sanitize_raw`, the clipboard path, has capped dict keys
    for exactly this reason since the 1.0.0+cfa.1 audit addendum.
    """
    assert len(_raw_summary({"A" * 100_000: 1})) < 2_000
    assert len(_raw_summary(_nested(20, 4))) < 6_000
    assert len(_raw_summary(_nested(50, 4))) < 6_000

    # api_capture.js caps URLs (12), keys per URL (200) and total bytes, but
    # neither a key name nor a URL path.
    hostile = {
        "api": {
            f"https://provider.example/{'p' * 40_000}/{index}": {
                f"{'K' * 4_000}{key}": 1 for key in range(200)
            }
            for index in range(12)
        },
        "body_text": "b" * 8_000,
    }
    line = _raw_summary(hostile)
    assert len(line) < 6_000, f"one raw_summary field was {len(line)} bytes"
    assert "more keys" in line or "..." in line, "truncation must be visible"


def test_raw_summary_still_says_what_it_dropped():
    """A bound that hides the fact that it bit is a bound that makes the log
    lie. The truncation marker is the diagnostic bit."""
    line = _raw_summary({f"k{index}": "v" * 400 for index in range(200)})

    assert "more keys" in line
    assert len(line) < 6_000
