from __future__ import annotations

import atexit
import json
import logging
import math
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta

from PyQt6.QtCore import QObject, QLockFile, QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from . import __version__
from .config import (
    Config,
    account_kind,
    app_data_dir,
    browser_accounts,
    display_name_for_account,
    qt_scale_factor_env,
)
from .cookie_dialog import CookieDialog
from .error_dialog import ErrorDetailsDialog, _redact_azure_ids
from .history import HistoryStore
from .logging_setup import setup_logging
from .gauge import highest_indicator
from .menubar import render_menubar_pixmap
from .models import SnapshotStatus, UsageSnapshot
from .platforms import autostart_command, get_platform
from .providers.base import Provider, ProviderSignals
from .providers.claude import ClaudeProvider
from .providers.codex import CodexProvider
from .providers.azure import AzureProvider
from .providers.copilot import CopilotProvider
from .providers.openrouter import OpenRouterProvider
from .providers.opencode_go import OpenCodeGoProvider, usage_url as opencode_go_usage_url
from .ratio import RatioStore, sessions_per_week
from .ratio_dialog import RatioHistoryDialog
from .settings_dialog import SettingsDialog
from .webview.cookies import hydrate_all_from_keyring
from .webview.login_window import LoginWindow
from .webview.profile import purge_profile
from .widget import UsageWidget

log = logging.getLogger("aigauge.app")

LOGIN_URLS = {
    "claude": ("https://claude.ai/login", "Sign in to Claude"),
    "codex": ("https://chatgpt.com/auth/login", "Sign in to ChatGPT"),
}

_ACTIVE_MODE_MINUTES = 30
_ERROR_RETRY_MINUTES = 1
# A provider that is simply broken must not be retried every minute forever.
# After this many consecutive failures the fast retry stops for that provider
# and the normal cadence takes over. The wait doubles between attempts, so
# the three are at 1, 2 and 4 minutes.
_ERROR_FAST_RETRY_ATTEMPTS = 3
# Failures that are not the provider failing, and must not earn a fast retry:
#   throttled       - the provider is deliberately not fetching yet (Azure's
#                     fail-closed hourly gate, or a fetch already in flight)
#   resume_artifact - a scrape whose clock ran across a machine suspend
_NO_FAST_RETRY_ERROR_CLASSES = ("throttled", "resume_artifact")
# A Qt::CoarseTimer rounds its expiry and may fire early. Without a tolerance
# the wake a provider's retry bought can find nothing due and fall through.
_RETRY_WAKE_TOLERANCE_SECONDS = 2
_HEARTBEAT_INTERVAL_MS = 5 * 60 * 1000
_LOG_VALUE_LIMIT = 300
# How long a provider may hold a refresh before the App declares it lost.
# Browser providers name their own bound (the scraper timeout times the
# attempts it may make); a REST provider is a handful of HTTPS calls with
# request timeouts of their own, so it gets a flat ceiling.
_REST_REFRESH_BUDGET_SECONDS = 60.0
# Enough slack that a provider finishing right at its own bound reports
# normally rather than racing the watchdog.
_WATCHDOG_SLACK_SECONDS = 20.0
# How long after the watchdog gave up a provider stays parked, as a multiple
# of the budget that expired. Until then its worker is presumed still out
# there - a browser scrape holding the one cached QWebEngineProfile for that
# account - and re-dispatching would put a second one on it. Past it the
# worker is assumed dead, because parking a provider forever is its own
# failure mode.
_ABANDONED_CEILING_FACTOR = 2.0


def _pool_capacity() -> int:
    """How many REST refreshes can actually run at once.

    `QThreadPool.globalInstance().maxThreadCount()` is the ideal thread count,
    which is 1 on a single-core host and 2 on plenty of laptops.
    """
    try:
        from PyQt6.QtCore import QThreadPool

        return max(1, int(QThreadPool.globalInstance().maxThreadCount()))
    except Exception:  # noqa: BLE001 - a budget is not worth crashing over
        return 1


def _refresh_budget_seconds(provider) -> float:
    """The watchdog budget for one provider, taken from the provider itself.

    Reading it off the provider keeps the App's deadline from drifting away
    from the bound the provider actually enforces - a watchdog that fires
    inside a refresh that is still legitimately running would manufacture
    failures rather than catch them.
    """
    try:
        budget = float(getattr(provider, "refresh_budget_seconds", 0) or 0)
    except (TypeError, ValueError):
        budget = 0.0
    # isfinite, not just > 0: int(inf * 1000) raises OverflowError, and it
    # would raise inside _arm_watchdog - which runs *before* _dispatch's
    # try/except around provider.refresh, so the name would be left in
    # _inflight with neither a dispatch nor a watchdog.
    if not math.isfinite(budget) or budget <= 0:
        return _REST_REFRESH_BUDGET_SECONDS
    return budget


def _make_dot_tray_icon(color: str | None = None) -> QIcon:
    """Tray dot in the given band colour (grey when there is nothing to show).

    The colour is resolved by the shared gauge bands, so the tray agrees with
    the expanded bars and compact chips. This unified the cutoffs: the dot used
    to have its own fixed 75/90 thresholds.
    """
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color or "#6b7280"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, 24, 24)
    painter.end()
    return QIcon(pix)


def _enabled_providers(config: Config) -> tuple[str, ...]:
    # `BrowserAccount.enabled` is deliberately NOT consulted here. See
    # config.BrowserAccount: nothing in the app ever writes the field, and the
    # migration can stamp it false permanently, so reading it turned the
    # Settings provider checkbox into a no-op the user could never undo. The
    # `providers.<kind>` toggle is the only switch.
    accounts = browser_accounts(config)
    out: list[str] = [
        account.id
        for account in accounts
        if getattr(config.providers, account.kind, False)
    ]
    # The fallback is for a legacy config that has no browser_accounts list at
    # all.
    if not accounts:
        providers = getattr(config, "providers", None)
        if getattr(providers, "claude", False):
            out.append("claude")
        if getattr(providers, "codex", False):
            out.append("codex")
    if config.providers.copilot:
        out.append("copilot")
    if getattr(config.providers, "azure", False):
        out.append("azure")
    if config.providers.openrouter:
        out.append("openrouter")
    if getattr(config.providers, "opencode_go", False):
        out.append("opencode_go")
    return tuple(out)


# Cheap REST providers, refreshed before the browser-driven ones so their tiles
# fill while a Claude/Codex scrape is still loading a page. Azure is cheap in
# the same sense - a handful of JSON calls - and it self-throttles to one live
# fetch per hour internally, so putting it early costs the API nothing even
# when an error fast-retry is running every minute.
#
# Copilot is five plain HTTPS calls and it used to sit *behind* the browser
# scrapes, because _build_providers inserts browser accounts first. In 4.5
# days of a real desktop log that cost it the whole browser queue: cycles ran
# a median of 48 s (p90 79 s) and Copilot's payload line landed near the end
# of every one of them, for a provider that answers in about a second.
_REFRESH_FIRST = ("openrouter", "azure", "copilot")


def _refresh_provider_order(providers: dict[str, Provider]) -> list[str]:
    names = list(providers)
    positions = {name: index for index, name in enumerate(names)}

    def rank(name: str) -> tuple[int, int]:
        if name in _REFRESH_FIRST:
            return (0, _REFRESH_FIRST.index(name))
        return (1, positions[name])

    return sorted(names, key=rank)


def _adaptive_refresh_minutes(
    *,
    active: bool,
    active_minutes: int,
    unchanged_cycles: int,
    max_minutes: int,
) -> int:
    active_minutes = max(1, min(active_minutes, max_minutes))
    if active:
        return active_minutes
    backoff = active_minutes * (2 ** max(0, unchanged_cycles))
    return min(max_minutes, backoff)


def _snapshot_signature(snapshot: UsageSnapshot) -> tuple:
    """What counts as "this provider changed" for the adaptive refresh cadence.

    Untagged metrics only. Tagged ones are informational — per-model rows,
    every breakdown meter the catalog knows — and a provider that renders a
    dozen of them offers a dozen numbers that can twitch, each one resetting
    the backoff. The cadence should follow the meters the tile is actually
    about.

    ``reset_label`` is deliberately absent for every provider. It is a
    caption, not a meter: a countdown that ticks, or Azure's spend to the
    cent. Either one counted as "this provider changed", which reset
    ``_unchanged_cycles`` and pushed the whole app back into active-cadence
    polling — for one cent, or for the clock. The label and the rounded
    percentage are what the cadence is about.

    ``snapshot.error`` is absent for exactly the same reason, and it leaked
    the same defect back in: Azure's fail-closed message counts a minute down
    ("Waiting for the next Azure fetch window (43 min)"), so it differed on
    every cycle and re-armed the 30-minute active window for every provider,
    on a clock. The *status* is what the cadence is about — a provider that
    starts failing, or stops, is a change; a failure whose wording moved is
    not. The message itself still reaches the tile, the tooltip and the log.
    """
    return (
        snapshot.status.value,
        tuple(
            (
                metric.label,
                (
                    round(metric.percent_used or 0, 1)
                    if metric.percent_used is not None
                    else None
                ),
            )
            for metric in snapshot.metrics
            if metric.tag is None
        ),
    )


_LOG_DICT_KEY_LIMIT = 50
# A key *name* is a field name, not a value. Clipping it keeps a bounded list
# of bounded strings even when the names themselves came off a provider page.
_LOG_KEY_LEN_LIMIT = 60


def _summarize_for_log(value, *, depth: int = 0):
    if depth > 3:
        return "..."
    if isinstance(value, str):
        return (
            value
            if len(value) <= _LOG_VALUE_LIMIT
            else value[:_LOG_VALUE_LIMIT] + "..."
        )
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        # Lists were already bounded; dictionaries were not. A page-controlled
        # payload with tens of thousands of keys produced a 1 MB log line
        # against a 512 KiB rotation, which discards the user's existing
        # diagnostics - the log is the one artifact that makes a provider
        # failure explainable, so losing it is the expensive part.
        items = sorted(value.items(), key=lambda item: str(item[0]))
        summarized = {
            str(k): _summarize_for_log(v, depth=depth + 1)
            for k, v in items[:_LOG_DICT_KEY_LIMIT]
        }
        if len(items) > _LOG_DICT_KEY_LIMIT:
            summarized["..."] = f"{len(items) - _LOG_DICT_KEY_LIMIT} more keys"
        return summarized
    if isinstance(value, (list, tuple)):
        summarized = [_summarize_for_log(v, depth=depth + 1) for v in value[:5]]
        if len(value) > 5:
            summarized.append(f"... {len(value) - 5} more")
        return summarized
    return repr(value)


def _raw_keys_for_log(raw: dict | None) -> str:
    """The key names of a provider payload, bounded.

    `_raw_summary` below already caps what it prints. This list did not, and
    `snapshot.raw` on the browser providers is the extractor's own dict - page
    data. One payload with tens of thousands of keys is a megabyte-long record
    against a 512 KiB x 3 rotation, which discards the diagnostic history the
    line exists to build.
    """
    if not raw:
        return "[]"
    keys = sorted(str(key) for key in raw)
    shown = [key[:_LOG_KEY_LEN_LIMIT] for key in keys[:_LOG_DICT_KEY_LIMIT]]
    if len(keys) > _LOG_DICT_KEY_LIMIT:
        shown.append(f"... {len(keys) - _LOG_DICT_KEY_LIMIT} more")
    return repr(shown)


def _raw_summary(raw: dict) -> str:
    try:
        return json.dumps(_summarize_for_log(raw), sort_keys=True, default=str)
    except TypeError:
        return repr(raw)


def _preserve_error_metrics(
    snapshot: UsageSnapshot,
    previous: UsageSnapshot | None,
) -> UsageSnapshot:
    if (
        snapshot.status == SnapshotStatus.ERROR
        and not snapshot.metrics
        and previous is not None
        and previous.status in (SnapshotStatus.OK, SnapshotStatus.ERROR)
        and previous.metrics
    ):
        return replace(snapshot, metrics=list(previous.metrics))
    return snapshot


def _acquire_instance_lock() -> QLockFile | None:
    lock_path = app_data_dir() / "ai-gauge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_path))
    if not lock.tryLock(100):
        return None
    return lock


def _flush_log_handlers() -> None:
    for handler in logging.getLogger("aigauge").handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass


class App(QObject):
    """Main application controller — owns the widget, providers, refresh timer, tray."""

    def __init__(self):
        super().__init__()
        setup_logging()
        log.info(
            "ai-gauge %s starting platform=%s frozen=%s executable=%s cwd=%s app_data=%s",
            __version__,
            get_platform().name,
            bool(getattr(sys, "frozen", False)),
            sys.executable,
            os.getcwd(),
            app_data_dir(),
        )
        self._started_at = datetime.now()
        self._instance_lock: QLockFile | None = None
        self._config = Config.load()
        self._snapshots: dict[str, UsageSnapshot] = {}
        self._history = HistoryStore()
        self._ratio = RatioStore()
        self._signals = ProviderSignals()
        self._signals.snapshot_ready.connect(self._on_snapshot)
        self._inflight: set[str] = set()
        self._refresh_queue: list[str] = []
        self._cycle_signatures: dict[str, tuple] = {}
        self._last_cycle_signatures: dict[str, tuple] | None = None
        self._unchanged_cycles = 0
        # provider -> (consecutive errors, when its own retry is due)
        self._error_retry: dict[str, tuple[int, datetime | None]] = {}
        self._active_until = datetime.now() + timedelta(minutes=_ACTIVE_MODE_MINUTES)
        self._current_refresh_manual = False
        self._pending_manual_refresh = False
        self._pending_manual_providers: list[str] = []
        self._watchdogs: dict[str, QTimer] = {}
        self._cycle_active = False
        self._cycle_started_at: float | None = None
        self._cycle_reason = "startup"
        self._cycle_statuses: dict[str, SnapshotStatus] = {}
        self._cycle_total = 0
        self._cycle_partial = False
        self._dispatch_times: dict[str, float] = {}
        # provider -> the number of the dispatch now outstanding. A snapshot
        # is matched against it, so an answer from a dispatch the App has
        # already given up on cannot be read as the current one.
        self._dispatch_epoch: dict[str, int] = {}
        # provider -> (the epoch the watchdog abandoned, when its worker may
        # be assumed dead). While an entry is live the provider is not
        # dispatched again by anything.
        self._abandoned: dict[str, tuple[int, float]] = {}
        # The REST providers this cycle handed to the thread pool, with their
        # budgets: what a dispatch may spend waiting for a pool thread.
        self._pool_wait_budgets: dict[str, float] = {}
        # Accounts the user removed whose on-disk profile is waiting for a
        # live scrape to let go of it. See _run_profile_purges.
        self._pending_profile_purges: list[str] = []
        # The names this cycle is accounting for. A snapshot from outside it
        # repaints its tile without joining its progress or its verdict.
        self._cycle_names: set[str] = set()
        self._dispatching = False
        self._next_refresh_reason = "startup"
        self._settings_dialog: SettingsDialog | None = None
        self._settings_old_copilot_quota: int | None = None
        self._install_lifecycle_logging()

        # Push any saved session cookies into the WebEngine profiles before any
        # scrape runs, so the headless page loads as signed-in.
        loaded = hydrate_all_from_keyring(self._config)
        log.info("hydrated cookies for: %s", loaded or "none")

        self._widget = UsageWidget(self._config)
        self._widget.refresh_requested.connect(lambda: self.refresh_now(manual=True))
        self._widget.settings_requested.connect(self.open_settings)
        self._widget.sign_in_requested.connect(self.open_login)
        self._widget.details_requested.connect(self.open_error_details)
        self._widget.ratio_history_requested.connect(self.open_ratio_history)
        self._widget.tile_expanded_changed.connect(self._on_tile_expanded_changed)
        self._widget.activated_requested.connect(self._on_widget_activated)
        self._widget.closed.connect(self._on_widget_closed)

        # Pre-populate provider tiles in stable order so they don't pop in.
        self._providers: dict[str, Provider] = {}
        self._build_providers()

        # System tray (or menu-bar item on macOS, or no-tray fallback on Linux)
        self._ui_mode = get_platform().default_ui_mode()
        tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if not tray_available:
            # Stock GNOME has no system tray. Force the floating widget to be
            # the only UI and serve the same menu via right-click on it.
            log.info("system tray not available; falling back to widget-only UI")
            self._ui_mode = "floating_widget"

        self._app_menu = self._build_app_menu()
        self._native_status = None

        if self._ui_mode == "menubar":
            try:
                from .macos_status_item import NativeMacStatusItem

                self._native_status = NativeMacStatusItem(
                    on_activate=self._toggle_widget,
                    on_context=self._show_tray_menu,
                )
                self._native_status.update(
                    self._snapshots,
                    _enabled_providers(self._config),
                    self._config,
                )
                self._native_status.set_tooltip(f"AI Gauge {__version__}")
                self._tray = None
                log.info("using native macOS status item")
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "native macOS status item unavailable; falling back to Qt tray: %s",
                    exc,
                )
                self._native_status = None

        if self._native_status is not None:
            pass
        elif tray_available:
            self._tray = QSystemTrayIcon(self._render_tray_icon())
            self._tray.setToolTip(f"AI Gauge {__version__}")
            if self._ui_mode != "menubar":
                self._tray.setContextMenu(self._app_menu)
            self._tray.activated.connect(self._on_tray_activated)
            self._tray.show()
        else:
            self._tray = None
            self._widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._widget.customContextMenuRequested.connect(
                lambda pos: self._app_menu.exec(self._widget.mapToGlobal(pos))
            )

        # Auto-refresh timer
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_refresh_timer)
        self._restart_timer()

        # Always start with a refresh — fresh installs see provider tiles in
        # their auth-required state with a Sign in button instead of a
        # surprise modal popup.
        QTimer.singleShot(500, lambda: self.refresh_now(manual=True))

        # On Windows/Linux the floating widget is the headline UI; on macOS
        # the menu-bar item is, and the widget appears as a popover only
        # when the user clicks the menu bar.
        if self._ui_mode == "floating_widget":
            self._widget.show()

    # ----- Lifecycle helpers -----

    def _install_lifecycle_logging(self) -> None:
        qt_app = QApplication.instance()
        if qt_app is not None:
            qt_app.aboutToQuit.connect(self._log_about_to_quit)
        atexit.register(self._log_atexit)

        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(_HEARTBEAT_INTERVAL_MS)
        self._heartbeat.timeout.connect(self._log_heartbeat)
        self._heartbeat.start()
        log.info("heartbeat enabled interval_s=%s", _HEARTBEAT_INTERVAL_MS // 1000)

    def _uptime_seconds(self) -> int:
        return max(0, int((datetime.now() - self._started_at).total_seconds()))

    def _next_refresh_seconds(self) -> int | None:
        try:
            if not hasattr(self, "_timer"):
                return None
            if not self._timer.isActive():
                return None
            return max(0, int(self._timer.remainingTime() / 1000))
        except RuntimeError:
            return None

    def _widget_visible_for_log(self) -> bool | None:
        try:
            if not hasattr(self, "_widget"):
                return None
            return self._widget.isVisible()
        except RuntimeError:
            return None

    def _lifecycle_context(self) -> dict[str, object]:
        return {
            "uptime_s": self._uptime_seconds(),
            "ui_mode": getattr(self, "_ui_mode", None),
            "widget_visible": self._widget_visible_for_log(),
            "providers": ",".join(_enabled_providers(self._config)),
            "inflight": ",".join(sorted(self._inflight)),
            "queue": ",".join(self._refresh_queue),
            "next_refresh_s": self._next_refresh_seconds(),
            "unchanged_cycles": self._unchanged_cycles,
            "error_cycles": self._error_retry_for_log(),
        }

    def _log_lifecycle_event(self, event: str) -> None:
        try:
            context = self._lifecycle_context()
            log.info(
                "%s uptime_s=%s ui_mode=%s widget_visible=%s providers=%s "
                "inflight=%s queue=%s next_refresh_s=%s unchanged_cycles=%s "
                "error_cycles=%s",
                event,
                context["uptime_s"],
                context["ui_mode"],
                context["widget_visible"],
                context["providers"],
                context["inflight"],
                context["queue"],
                context["next_refresh_s"],
                context["unchanged_cycles"],
                context["error_cycles"],
            )
        except Exception:  # noqa: BLE001
            log.exception("%s logging failed", event)
        finally:
            _flush_log_handlers()

    def _log_heartbeat(self) -> None:
        self._log_lifecycle_event("heartbeat")
        self._recover_dead_timer()
        # A profile whose scrape was abandoned is only released when that
        # worker reports back or its ceiling passes; the heartbeat is what
        # notices the second of those.
        if self._pending_profile_purges:
            self._run_profile_purges()

    def _recover_dead_timer(self) -> None:
        """Restart a scheduler that stopped scheduling.

        `_schedule_next_refresh` returns without starting the timer while
        anything is in flight, so any path that loses a cycle leaves the app
        with no timer at all: refreshes stop, the header freezes, and only a
        restart fixes it. The heartbeat is the one timer still running, so it
        is what notices.
        """
        if self._inflight or self._refresh_queue:
            return
        if self._cycle_active:
            if self._watchdogs:
                # A dispatch is still bounded; its watchdog will end it.
                return
            # Nothing in flight, nothing queued, no watchdog left, and a cycle
            # that still calls itself open. This is the one state that cannot
            # recover on its own - there is no timer, because
            # _schedule_next_refresh was never reached - and it was also the
            # one state this method declined to act in.
            log.warning(
                "refresh cycle was wedged (active with nothing in flight); ending it"
            )
            self._end_cycle()
            return
        if not self._providers:
            # With no provider configured at all, refresh_now returns before
            # it touches the timer, so rescheduling here only logs a warning
            # every five minutes forever.
            return
        try:
            if self._timer.isActive():
                return
        except RuntimeError:
            return
        log.warning("refresh timer was not running; rescheduling")
        self._schedule_next_refresh()

    def _log_about_to_quit(self) -> None:
        self._log_lifecycle_event("qt aboutToQuit")

    def _log_atexit(self) -> None:
        self._log_lifecycle_event("python atexit")

    def _build_providers(self) -> None:
        # Tear down any existing providers (no shared state to clean up beyond refs)
        self._providers.clear()
        desired_tiles: set[str] = set()
        for account in browser_accounts(self._config):
            if not getattr(self._config.providers, account.kind, False):
                continue
            desired_tiles.add(account.id)
            if account.kind == "claude":
                self._providers[account.id] = ClaudeProvider(
                    parent=self,
                    account_id=account.id,
                    config=self._config,
                )
            elif account.kind == "codex":
                self._providers[account.id] = CodexProvider(
                    parent=self,
                    account_id=account.id,
                    config=self._config,
                )
            self._widget.ensure_tile(account.id, display_name_for_account(self._config, account.id))
        if self._config.providers.copilot:
            self._providers["copilot"] = CopilotProvider(self._config)
            desired_tiles.add("copilot")
            self._widget.ensure_tile("copilot", "Copilot")
        if getattr(self._config.providers, "azure", False):
            self._providers["azure"] = AzureProvider(self._config)
            desired_tiles.add("azure")
            self._widget.ensure_tile("azure", "Microsoft · Azure")
        if self._config.providers.openrouter:
            self._providers["openrouter"] = OpenRouterProvider(self._config)
            desired_tiles.add("openrouter")
            self._widget.ensure_tile("openrouter", "OpenRouter")
        if self._config.providers.opencode_go:
            self._providers["opencode_go"] = OpenCodeGoProvider(self._config, parent=self)
            desired_tiles.add("opencode_go")
            self._widget.ensure_tile("opencode_go", "OpenCode")
        for tile_id in list(self._widget._tiles):  # noqa: SLF001
            if tile_id not in desired_tiles:
                self._widget.remove_tile(tile_id)
                self._snapshots.pop(tile_id, None)
        self._error_retry = {
            name: state
            for name, state in self._error_retry.items()
            if name in self._providers
        }
        # Same pruning for the other per-provider maps, so a removed provider
        # leaves nothing behind. A name still in flight or still parked keeps
        # its entries: its dispatch is what they bound.
        keep = set(self._providers) | self._inflight | set(self._abandoned)
        for mapping in (self._dispatch_times, self._dispatch_epoch):
            for name in [n for n in mapping if n not in keep]:
                mapping.pop(name, None)
        for name in [n for n in self._watchdogs if n not in keep]:
            self._cancel_watchdog(name)

    def _restart_timer(self) -> None:
        self._timer.stop()
        self._schedule_next_refresh()

    def _schedule_next_refresh(self) -> None:
        if self._inflight or self._refresh_queue:
            return
        now = datetime.now()
        max_minutes = max(1, self._config.refresh_interval_minutes)
        active = now < self._active_until
        minutes = _adaptive_refresh_minutes(
            active=active,
            active_minutes=self._config.active_refresh_interval_minutes,
            unchanged_cycles=self._unchanged_cycles,
            max_minutes=max_minutes,
        )
        next_refresh_at = now + timedelta(minutes=minutes)
        reason = "active" if active else "idle"
        # Don't let an idle backoff stretch past a known reset — otherwise the
        # panel keeps showing 100% for tens of minutes after the limit has
        # actually rolled over. Pull the refresh forward so we re-read shortly
        # after the predicted reset.
        soon_after_reset = self._earliest_reset_refresh_time()
        if soon_after_reset is not None and soon_after_reset < next_refresh_at:
            next_refresh_at = soon_after_reset
            reason = "reset_pull_forward"
            minutes = max(
                1,
                int((next_refresh_at - datetime.now()).total_seconds() // 60) or 1,
            )
        error_retry = self._error_retry_time(now)
        if error_retry is not None and error_retry < next_refresh_at:
            next_refresh_at = error_retry
            reason = "error_retry"
            minutes = max(
                1,
                int((next_refresh_at - datetime.now()).total_seconds() // 60) or 1,
            )
        delay_ms = max(
            1000,
            int((next_refresh_at - datetime.now()).total_seconds() * 1000),
        )
        self._next_refresh_reason = reason
        self._timer.start(delay_ms)
        # The one line that makes an unexplained refresh explainable: the log
        # showed 32% of cycles starting within two minutes of the previous one
        # and no way to tell a fast retry from a reset pull-forward.
        log.info(
            "refresh scheduled in_s=%s reason=%s active=%s unchanged_cycles=%s",
            delay_ms // 1000,
            reason,
            active,
            self._unchanged_cycles,
        )
        self._widget.set_refresh_state(
            active=active,
            minutes=minutes,
            next_at=next_refresh_at,
        )

    def _earliest_reset_refresh_time(self) -> datetime | None:
        """Earliest moment the next scheduled refresh should run because some
        provider's metric is predicted to reset soon.

        Returns ``None`` when there is nothing useful to anticipate.
        """
        now = datetime.now()
        # Give the upstream a minute to commit the reset before we re-read.
        grace = timedelta(minutes=1)
        earliest: datetime | None = None
        for snap in self._snapshots.values():
            if snap.status != SnapshotStatus.OK:
                continue
            for metric in snap.metrics:
                if metric.resets_at is None:
                    continue
                if (metric.percent_used or 0) <= 0:
                    # An unused metric resetting changes nothing visible.
                    continue
                target = metric.resets_at + grace
                if target <= now:
                    continue
                if earliest is None or target < earliest:
                    earliest = target
        return earliest

    def _record_provider_outcome(self, snapshot: UsageSnapshot) -> None:
        """Advance or clear one provider's error streak.

        Per provider, not per cycle. The old counter was cycle-wide: any
        ERROR snapshot anywhere meant the *next cycle* ran in a minute, for
        everyone. 124 of 137 cycles in 4.5 days of desktop log contained at
        least one ERROR or AUTH_REQUIRED - OpenCode alone never succeeded once
        - so a third of all cycles started within two minutes of the previous
        one and five healthy providers were re-scraped for one broken tile.
        The bound then bit the wrong way round: past three failing cycles
        nobody got a fast retry, and since no cycle was ever clean the counter
        never reset, so a genuinely transient failure on a *different*
        provider got nothing.

        AUTH_REQUIRED is deliberately not a failure: signing in is the user's
        move, and retrying it quickly only burns page loads. Nor is a failure
        the provider marked as something other than its own (see
        _NO_FAST_RETRY_ERROR_CLASSES).

        One consequence worth stating: "three retries and then the normal
        cadence" is a bound on a *run* of errors, not on a provider. Any
        non-ERROR status pops the entry, so a provider alternating
        AUTH_REQUIRED and ERROR - OpenCode's exact pattern in the desktop log,
        95 auth failures and 38 errors, never a success - refills the ladder
        each time. Measured, that is about one extra dispatch per cycle for
        that provider while the healthy ones drop from 17 an hour to 10, so it
        is not an amplification; it is just not the bound the sentence above
        sounds like.
        """
        name = snapshot.provider
        if snapshot.status != SnapshotStatus.ERROR:
            # OK clears the streak; so does AUTH_REQUIRED, which is a state,
            # not a fault to back off from.
            self._error_retry.pop(name, None)
            return
        if snapshot.error_class in _NO_FAST_RETRY_ERROR_CLASSES:
            log.info(
                "refresh retry skipped provider=%s error_class=%s",
                name,
                snapshot.error_class,
            )
            # A wait is not a pending retry. Leaving an already-owed entry in
            # place left its `due` in the past; `_error_retry_time` clamps a
            # past due to *now*, `_schedule_next_refresh` floors the delay at
            # 1 000 ms, and the wake produces the same answer - a 1 Hz cycle
            # loop for as long as the throttle lasts, measured at 3 543 cycles
            # in an hour against an Azure hourly gate.
            self._error_retry.pop(name, None)
            return
        errors = self._error_retry.get(name, (0, None))[0] + 1
        if errors <= _ERROR_FAST_RETRY_ATTEMPTS:
            # 1, 2, 4 minutes. A flat minute is what the log caught in the
            # act: an offline burst re-scraped every provider every minute
            # while Claude alone needs a median of 18.5 s of browser time.
            delay = timedelta(minutes=_ERROR_RETRY_MINUTES * (2 ** (errors - 1)))
            due: datetime | None = datetime.now() + delay
        else:
            due = None
        self._error_retry[name] = (errors, due)

    def _error_retry_for_log(self) -> str:
        if not self._error_retry:
            return "-"
        return ",".join(
            f"{name}:{errors}"
            for name, (errors, _due) in sorted(self._error_retry.items())
        )

    def _due_error_providers(self, now: datetime | None = None) -> list[str]:
        """Providers whose own fast retry has come due, in queue order."""
        moment = now or datetime.now()
        return self._ordered(
            name
            for name, (_errors, due) in self._error_retry.items()
            if due is not None and due <= moment
        )

    def _error_retry_time(self, now: datetime | None = None) -> datetime | None:
        """Soonest recovery refresh owed to any single provider.

        The fast retry used to require the errored snapshot to still carry
        stale metrics, which meant the *worse* case got the slower retry: a
        provider that had never succeeded this run showed nothing at all and
        then waited a full interval, while one showing a stale-but-plausible
        number was retried within the minute.

        That is exactly the shape of a cold start. Claude's settings page
        resolves eight endpoints before it requests usage, and on a fresh
        launch none of them are cached, so the first scrape can exceed its
        budget. The retry a minute later runs against a warm cache and
        succeeds - but until then every restart showed a broken tile for a
        full refresh interval, at precisely the moment a user is most likely
        to be looking at the app.
        """
        moment = now or datetime.now()
        due_times = [
            due
            for name, (_errors, due) in self._error_retry.items()
            if due is not None and name in self._providers
        ]
        if not due_times:
            return None
        return max(min(due_times), moment)

    # ----- Refresh -----

    def _display_names(self, names: list[str]) -> dict[str, str]:
        return {
            name: {
                "copilot": "Copilot",
                "openrouter": "OpenRouter",
                "azure": "Microsoft · Azure",
            }.get(name, display_name_for_account(self._config, name))
            for name in names
        }

    def _begin_cycle(self, names: list[str], *, manual: bool, reason: str) -> None:
        """Start one refresh cycle over ``names``, in queue order.

        The single entry point for every cycle - manual, scheduled or a
        per-provider retry - so the log line that opens a cycle cannot
        disagree with what actually ran.
        """
        # A provider the App has given up on but whose worker is still out
        # there is not dispatched again - by this cycle or any other. Filter
        # before the cycle's own totals are computed, so its progress and its
        # verdict are about what actually ran.
        wanted: list[str] = []
        for name in names:
            refusal = self._dispatch_refusal(name)
            if refusal is None:
                wanted.append(name)
            else:
                log.info(
                    "refresh provider skipped provider=%s reason=%s", name, refusal
                )
        names = wanted
        if manual:
            self._active_until = datetime.now() + timedelta(
                minutes=_ACTIVE_MODE_MINUTES
            )
            self._unchanged_cycles = 0
        self._timer.stop()
        self._current_refresh_manual = manual
        self._cycle_signatures = {}
        self._cycle_statuses = {}
        self._cycle_total = len(names)
        self._cycle_started_at = time.monotonic()
        self._cycle_reason = reason
        self._cycle_names = set(names)
        self._cycle_partial = len(names) < len(self._providers)
        self._cycle_active = True
        log.info(
            "refresh cycle start manual=%s reason=%s providers=%s",
            manual,
            reason,
            ",".join(names),
        )
        self._widget.set_refreshing(True, total=len(names))
        # Both kinds of cycle mark their tiles now. A scheduled one is marked
        # more lightly - nobody asked for it - but it is marked, because the
        # alternative was a 48 s median cycle with no visible sign at all.
        self._widget.mark_loading(self._display_names(names), subtle=not manual)
        # The browser providers keep the serial queue; everything else goes
        # out at once. A provider that answers from inside this loop would
        # otherwise find an empty queue and close the cycle before the rest of
        # the batch had even been dispatched, so the loop holds the cycle open
        # until it is done.
        concurrent = [name for name in names if not self._uses_browser(name)]
        self._refresh_queue = [name for name in names if self._uses_browser(name)]
        # What each of those may spend waiting for a pool thread, before its
        # own work even starts. See _pool_wait_slack.
        self._pool_wait_budgets = {
            name: _refresh_budget_seconds(self._providers.get(name))
            for name in concurrent
        }
        self._dispatching = True
        try:
            for name in concurrent:
                self._dispatch(name)
        finally:
            self._dispatching = False
        self._advance_cycle()

    def refresh_now(self, manual: bool = True) -> None:
        if not self._providers:
            return
        if self._inflight or self._refresh_queue:
            if manual:
                # The widget's Refresh button is disabled for the whole cycle,
                # so the reachable path is the tray menu's "Refresh now" - and
                # it silently did nothing, at exactly the moment a tile looks
                # stale, which is usually mid-cycle. Settings-save went the
                # same way: apply, then a refresh_now that no-opped.
                self._pending_manual_refresh = True
                log.info(
                    "refresh_now queued inflight=%s queue=%s",
                    ",".join(sorted(self._inflight)) or "-",
                    ",".join(self._refresh_queue) or "-",
                )
                return
            # A *scheduled* wake landing inside a cycle needs no queueing:
            # the cycle it landed in already covers every provider.
            log.info(
                "refresh_now ignored inflight=%s queue=%s",
                ",".join(sorted(self._inflight)) or "-",
                ",".join(self._refresh_queue) or "-",
            )
            return
        self._begin_cycle(
            _refresh_provider_order(self._providers),
            manual=manual,
            reason="manual" if manual else self._next_refresh_reason,
        )

    def refresh_provider(self, provider: str) -> None:
        if provider not in self._providers:
            return
        if self._inflight or self._refresh_queue:
            if provider not in self._pending_manual_providers:
                self._pending_manual_providers.append(provider)
            log.info(
                "refresh_provider queued provider=%s inflight=%s queue=%s",
                provider,
                ",".join(sorted(self._inflight)) or "-",
                ",".join(self._refresh_queue) or "-",
            )
            return
        self._begin_cycle([provider], manual=True, reason="manual")

    def _ordered(self, names) -> list[str]:
        """The configured providers in ``names``, in canonical queue order."""
        wanted = set(names)
        return [
            name
            for name in _refresh_provider_order(self._providers)
            if name in wanted
        ]

    def _run_pending_manual(self) -> None:
        full = self._pending_manual_refresh
        wanted = list(self._pending_manual_providers)
        names = self._ordered(wanted)
        self._pending_manual_refresh = False
        self._pending_manual_providers = []
        if full:
            log.info("refresh pending manual running scope=all")
            self.refresh_now(manual=True)
        elif names:
            log.info("refresh pending manual running scope=%s", ",".join(names))
            self._begin_cycle(names, manual=True, reason="manual")
        elif wanted:
            # _ordered() filters against _providers, so a settings save that
            # removed the provider the user had just asked to refresh left
            # neither branch running and no line at all - indistinguishable
            # from the request never having been made.
            log.info("refresh pending manual dropped scope=%s", ",".join(wanted))

    def _uses_browser(self, name: str) -> bool:
        return bool(getattr(self._providers.get(name), "uses_browser", False))

    def _browser_in_flight(self) -> bool:
        return any(self._uses_browser(name) for name in self._inflight)

    def _start_next_refresh(self) -> None:
        if not self._refresh_queue:
            return
        # Only the browser queue is serial. A REST provider still in flight
        # must not hold up the next scrape.
        if self._browser_in_flight():
            return
        name = self._refresh_queue.pop(0)
        refusal = self._dispatch_refusal(name)
        if refusal is not None:
            # Usually a settings save removed this one while it was queued.
            # Re-entering _start_next_refresh here returned on the empty-queue
            # guard when the dropped name was the last entry, leaving
            # _cycle_active true with nothing in flight: the timer stopped,
            # the heartbeat's recovery blocked on _cycle_active, the Refresh
            # button disabled, and any queued manual refresh stranded.
            # _advance_cycle does both jobs - next provider, or close the
            # cycle.
            log.info("refresh provider skipped provider=%s reason=%s", name, refusal)
            self._cycle_names.discard(name)
            self._cycle_total = max(len(self._cycle_statuses), self._cycle_total - 1)
            self._advance_cycle()
            return
        self._dispatch(name)

    def _dispatch_refusal(self, name: str) -> str | None:
        """Why this provider must not be dispatched now, or None.

        The single gate every path goes through - a scheduled cycle, a retry
        wake, a manual refresh, a settings save - so a provider cannot be sent
        out twice by one of them while another thinks it is idle.
        """
        if self._providers.get(name) is None:
            return "not_configured"
        if name in self._inflight:
            return "already_in_flight"
        if self._is_abandoned(name):
            return "abandoned"
        return None

    def _is_abandoned(self, name: str) -> bool:
        """Is a dispatch the watchdog gave up on still presumed to be running?

        The watchdog ends the App's *wait*; it does not cancel the provider's
        work. A browser provider is still loading a page on the one cached
        QWebEngineProfile for that account (webview/profile.py returns one per
        provider), and ClaudeProvider.refresh rebuilds its runner
        unconditionally - so a second dispatch means two QWebEngineViews
        writing one cookie store, which is how a spurious sign-out happens,
        and N times the load on the provider from one desktop app.

        The entry clears when the abandoned worker finally reports back, or
        when twice its budget has passed and it can fairly be called dead.
        """
        entry = self._abandoned.get(name)
        if entry is None:
            return False
        epoch, assumed_dead_at = entry
        if time.monotonic() >= assumed_dead_at:
            log.warning(
                "refresh provider abandoned worker assumed dead provider=%s epoch=%s",
                name,
                epoch,
            )
            self._abandoned.pop(name, None)
            return False
        return True

    def _purge_removed_profiles(self, account_ids) -> None:
        for account_id in account_ids:
            if account_id not in self._pending_profile_purges:
                self._pending_profile_purges.append(account_id)
        self._run_profile_purges()

    def _run_profile_purges(self) -> None:
        """Delete a removed account's profile, once nothing is still using it.

        `purge_profile` calls `deleteLater()` on the cached
        `QWebEngineProfile` and then rmtree's its directory. A settings save
        can remove an account while its refresh is still out - F14's own
        comment names that scenario - and Qt requires a profile to outlive its
        pages, so destroying it under a live `QuietWebEnginePage` is a
        use-after-free. The surviving page can also flush rotated session
        cookies back into the directory that was just deleted, which puts a
        removed account's live credential back on disk.

        The keyring secret is cleared immediately by the dialog either way;
        this is only the on-disk profile, and deferring it costs nothing.
        """
        waiting: list[str] = []
        for account_id in self._pending_profile_purges:
            if account_id in self._inflight or self._is_abandoned(account_id):
                log.info(
                    "profile purge deferred account=%s reason=refresh_in_flight",
                    account_id,
                )
                waiting.append(account_id)
                continue
            try:
                purge_profile(account_id)
            except Exception:  # noqa: BLE001 - cleanup must not crash the app
                log.exception("failed to purge profile for %s", account_id)
        self._pending_profile_purges = waiting

    def _pool_wait_slack(self, name: str) -> float:
        """How long this dispatch may sit in the thread pool before it starts.

        `_arm_watchdog` starts its clock at dispatch, but a REST provider's
        `work()` starts when a `QThreadPool` thread frees up - and the cycle
        now hands openrouter, copilot and azure to the pool in one burst. On a
        host whose ideal thread count is 1 or 2 the last runnable waits behind
        the others while its own budget is already running, so the watchdog
        would fire inside a refresh that has not exceeded its own bound. The
        providers are not asked to report when they start (three separate
        `_run_async` implementations, and the Provider API is one callback),
        so the allowance is explicit here instead.
        """
        budgets = self._pool_wait_budgets
        if name not in budgets:
            return 0.0
        ahead = sum(budget for other, budget in budgets.items() if other != name)
        return ahead / _pool_capacity()

    def _dispatch(self, name: str) -> None:
        provider = self._providers.get(name)
        if provider is None:
            return
        refusal = self._dispatch_refusal(name)
        if refusal is not None:
            # Belt and braces: every caller asks first, and this is what makes
            # "one dispatch per provider at a time" a property of the method
            # rather than of its callers.
            log.warning(
                "refresh provider not dispatched provider=%s reason=%s",
                name,
                refusal,
            )
            return
        epoch = self._dispatch_epoch.get(name, 0) + 1
        self._dispatch_epoch[name] = epoch
        self._inflight.add(name)
        now = time.monotonic()
        self._dispatch_times[name] = now
        log.info(
            "refresh provider start provider=%s epoch=%s queued_s=%.1f",
            name,
            epoch,
            max(0.0, now - (self._cycle_started_at or now)),
        )
        self._arm_watchdog(name, provider, epoch)

        # The epoch travels with the answer, so a snapshot can be matched to
        # the dispatch it answers rather than to whatever is in flight for
        # that name when it lands.
        def _emit(snap: UsageSnapshot, _epoch=epoch):
            self._signals.snapshot_ready.emit((snap, _epoch))

        try:
            provider.refresh(_emit)
        except Exception as exc:  # noqa: BLE001
            self._signals.snapshot_ready.emit(
                (
                    UsageSnapshot(
                        provider=name,
                        status=SnapshotStatus.ERROR,
                        # str(exc) on a transport failure carries the request
                        # URL, and this string reaches the tile, the tray
                        # tooltip and the error dialog. Same redaction the log
                        # line below uses.
                        error=_redact_azure_ids(str(exc)),
                    ),
                    epoch,
                )
            )

    def _arm_watchdog(self, name: str, provider: Provider, epoch: int = 0) -> None:
        """Bound one dispatch, so a provider that never answers cannot stall
        the cycle - and with it every future refresh."""
        self._cancel_watchdog(name)
        budget = (
            _refresh_budget_seconds(provider)
            + _WATCHDOG_SLACK_SECONDS
            + self._pool_wait_slack(name)
        )
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda n=name, b=budget, e=epoch: self._on_watchdog(n, b, e)
        )
        timer.start(int(budget * 1000))
        self._watchdogs[name] = timer

    def _cancel_watchdog(self, name: str) -> None:
        self._retire_watchdog(self._watchdogs.pop(name, None))

    @staticmethod
    def _retire_watchdog(timer) -> None:
        """Stop a watchdog *and* destroy it.

        `QTimer(self)` parents the C++ object to `App`, so dropping the Python
        reference frees nothing: 2 000 armed-and-stopped watchdogs left 2 000
        live QObjects. At ~180 dispatches a day in a tray app designed to run
        for weeks that is unbounded growth, and it slows every child-event
        walk on `App`.
        """
        if timer is None:
            return
        try:
            timer.stop()
            timer.deleteLater()
        except RuntimeError:
            pass

    def _on_watchdog(self, name: str, budget: float, epoch: int = 0) -> None:
        self._retire_watchdog(self._watchdogs.pop(name, None))
        if name not in self._inflight:
            return
        if epoch and self._dispatch_epoch.get(name) != epoch:
            # A watchdog outliving the dispatch it was armed for.
            return
        log.warning(
            "refresh provider watchdog provider=%s epoch=%s budget_s=%.0f - giving up",
            name,
            epoch,
            budget,
        )
        # The App stops waiting; the provider does not stop working. Park the
        # name until that worker reports back or can be assumed dead, so no
        # cycle, retry wake, manual refresh or settings save starts a second
        # one alongside it.
        assumed_dead_in = _ABANDONED_CEILING_FACTOR * budget
        self._abandoned[name] = (epoch, time.monotonic() + assumed_dead_in)
        log.warning(
            "refresh provider abandoned provider=%s epoch=%s eligible_again_in_s=%.0f",
            name,
            epoch,
            assumed_dead_in,
        )
        self._signals.snapshot_ready.emit(
            (
                UsageSnapshot(
                    provider=name,
                    status=SnapshotStatus.ERROR,
                    error="Refresh timed out.",
                ),
                epoch,
            )
        )

    def _on_refresh_timer(self) -> None:
        """The scheduled wake. A retry wake refreshes only what is owed one."""
        if self._next_refresh_reason != "error_retry":
            self.refresh_now(manual=False)
            return
        # A Qt::CoarseTimer rounds its expiry and may fire a few milliseconds
        # before the due it was armed for, so the due test gets a tolerance.
        due = self._due_error_providers(
            datetime.now() + timedelta(seconds=_RETRY_WAKE_TOLERANCE_SECONDS)
        )
        if not due:
            # The wake was bought by one provider's retry and that retry is no
            # longer owed - the provider recovered, or a settings save removed
            # it. Falling through to refresh_now() re-ran the whole queue,
            # which is the cycle-wide retry this release removed: one flapping
            # provider cost every other provider a full extra cycle.
            log.info("refresh retry wake found nothing due")
            self._schedule_next_refresh()
            return
        if self._inflight or self._refresh_queue:
            log.info(
                "refresh_now ignored inflight=%s queue=%s",
                ",".join(sorted(self._inflight)) or "-",
                ",".join(self._refresh_queue) or "-",
            )
            return
        # Spend the due here, at dispatch, rather than waiting for an answer
        # to clear it. An answer that does not clear the entry - a throttle, a
        # resume artifact, a provider removed between the wake and the
        # dispatch - would otherwise leave a past due in place, and a past due
        # pins every later wake at the timer's 1 000 ms floor. The streak
        # survives, because that is what bounds the 1/2/4-minute ladder.
        for name in due:
            errors, _due = self._error_retry.get(name, (0, None))
            self._error_retry[name] = (errors, None)
        self._begin_cycle(due, manual=False, reason="error_retry")

    def _on_snapshot(self, payload) -> None:
        if isinstance(payload, tuple):
            snapshot, epoch = payload
        else:
            # Not a dispatch answer: a settings save re-rendering a cached
            # snapshot with a new denominator. There is no epoch to match.
            snapshot, epoch = payload, None
        name = snapshot.provider
        if epoch is not None and not (
            name in self._inflight and self._dispatch_epoch.get(name) == epoch
        ):
            self._on_late_snapshot(snapshot, epoch)
            return
        was_inflight = name in self._inflight
        self._inflight.discard(name)
        self._cancel_watchdog(name)
        dispatched_at = self._dispatch_times.pop(name, None)
        if was_inflight:
            log.info(
                "refresh provider done provider=%s elapsed_s=%.1f status=%s",
                name,
                (time.monotonic() - dispatched_at) if dispatched_at else 0.0,
                snapshot.status.value,
            )
        if name not in self._providers:
            # A settings save can remove a provider while its refresh is still
            # out. Storing the late snapshot re-created the tile that
            # _build_providers had just deleted, so a provider the user had
            # turned off came back until the next cycle.
            log.info(
                "refresh provider dropped provider=%s status=%s reason=not_configured",
                name,
                snapshot.status.value,
            )
            self._advance_cycle()
            return
        snapshot = _preserve_error_metrics(
            snapshot,
            self._snapshots.get(snapshot.provider),
        )
        self._snapshots[snapshot.provider] = snapshot
        # A cycle accounts for what it dispatched. A snapshot from a provider
        # it never asked - a per-provider retry running while something else
        # reports - used to count toward its progress, its `errors=` line and
        # its `changed` verdict.
        in_cycle = self._cycle_active and name in self._cycle_names
        if in_cycle:
            self._cycle_signatures[snapshot.provider] = _snapshot_signature(snapshot)
            self._cycle_statuses[snapshot.provider] = snapshot.status
        self._record_provider_outcome(snapshot)
        if snapshot.status == SnapshotStatus.ERROR:
            log.warning(
                "snapshot error provider=%s error=%s raw_keys=%s raw_summary=%s",
                snapshot.provider,
                _redact_azure_ids(snapshot.error or ""),
                _raw_keys_for_log(snapshot.raw),
                _raw_summary(snapshot.raw) if snapshot.raw else "{}",
            )
        elif snapshot.status == SnapshotStatus.AUTH_REQUIRED:
            log.info(
                "snapshot auth_required provider=%s error=%s raw_keys=%s raw_summary=%s",
                snapshot.provider,
                _redact_azure_ids(snapshot.error or ""),
                _raw_keys_for_log(snapshot.raw),
                _raw_summary(snapshot.raw) if snapshot.raw else "{}",
            )
        try:
            self._history.record_snapshot(snapshot)
        except Exception:  # noqa: BLE001
            log.exception("history.record_snapshot failed")
        try:
            self._ratio.record_snapshot(snapshot)
        except Exception:  # noqa: BLE001
            log.exception("ratio.record_snapshot failed")
        display_name = display_name_for_account(self._config, snapshot.provider)
        self._widget.update_snapshot(snapshot, display_name)
        try:
            self._widget.set_ratio(
                snapshot.provider,
                self._ratio.display_estimate(snapshot.provider),
                self._ratio_recent(snapshot.provider),
                self._ratio.current_estimate(snapshot.provider),
            )
        except Exception:  # noqa: BLE001
            log.exception("widget.set_ratio failed")
        if in_cycle:
            # The cycle's own total, not one recomputed from the queue: while
            # the REST batch is going out, the providers not yet dispatched
            # are in neither set and the header would count down to a
            # denominator that moves.
            self._widget.set_refresh_progress(
                len(self._cycle_statuses), self._cycle_total
            )
        # Per snapshot, not per cycle: the tray dot and its tooltip used to be
        # a whole cycle behind the tiles, which is minutes on a failing cycle.
        self._update_tray()

        self._advance_cycle()

    def _on_late_snapshot(self, snapshot: UsageSnapshot, epoch: int) -> None:
        """An answer from a dispatch that is no longer the current one.

        Keying on the provider name alone had no dispatch identity: when a
        scrape the watchdog had abandoned finally answered, the *new*
        dispatch's `_inflight` entry made the stale answer look current, so it
        cancelled the new dispatch's watchdog and closed the cycle while that
        scrape was still running - live in neither `_inflight` nor
        `_watchdogs`.

        The answer is dropped rather than painted: it was produced by a
        dispatch the App gave up on, possibly against a configuration the user
        has since changed, and F14 already drops a snapshot whose provider is
        gone for the same reason. What it does tell us is that the worker
        finally let go, which is what un-parks the provider.
        """
        name = snapshot.provider
        log.info(
            "refresh provider late provider=%s epoch=%s current=%s status=%s - dropped",
            name,
            epoch,
            self._dispatch_epoch.get(name, "-"),
            snapshot.status.value,
        )
        abandoned = self._abandoned.get(name)
        if abandoned is not None and abandoned[0] == epoch:
            self._abandoned.pop(name, None)
            log.info(
                "refresh provider abandoned worker reported back provider=%s epoch=%s",
                name,
                epoch,
            )
            self._run_profile_purges()

    def _advance_cycle(self) -> None:
        """Dispatch the next provider, or close the cycle when none is left."""
        if not self._cycle_active:
            # A snapshot that arrived outside a cycle - a re-render from a
            # settings change, or a provider reporting late - repaints its
            # tile and nothing more. It must not close a cycle that is not
            # running or re-arm the timer behind the scheduler's back.
            return
        if self._dispatching:
            # Still handing out this cycle's work; a synchronous answer must
            # not be read as "everything is done".
            return
        if self._refresh_queue:
            QTimer.singleShot(0, self._start_next_refresh)
            return
        if self._inflight:
            return
        self._end_cycle()

    def _end_cycle(self) -> None:
        self._cycle_active = False
        changed = self._cycle_changed()
        if changed:
            self._active_until = datetime.now() + timedelta(
                minutes=_ACTIVE_MODE_MINUTES
            )
            self._unchanged_cycles = 0
        elif not self._current_refresh_manual and not self._cycle_partial:
            # A retry cycle polls one provider; "nothing changed" there says
            # nothing about whether the app is idle.
            #
            # The guard is deliberately asymmetric: a partial cycle cannot
            # *advance* the backoff but a partial cycle whose one tile moved
            # still zeroes it and re-arms the active window above, so a
            # provider that flaps ERROR to OK on its retry cadence -
            # OpenRouter had 22 such errors in 4.5 days - holds the whole app
            # in active mode. Symmetry would be worse: a real change is a real
            # change, whoever noticed it.
            self._unchanged_cycles += 1
        # Merge rather than replace: a partial cycle must not erase the
        # baseline for the providers it did not visit.
        self._last_cycle_signatures = {
            **(self._last_cycle_signatures or {}),
            **self._cycle_signatures,
        }
        started_at = self._cycle_started_at
        log.info(
            "refresh cycle end duration_s=%.1f changed=%s errors=%s auth_required=%s",
            (time.monotonic() - started_at) if started_at else 0.0,
            changed,
            sum(
                1
                for status in self._cycle_statuses.values()
                if status == SnapshotStatus.ERROR
            ),
            sum(
                1
                for status in self._cycle_statuses.values()
                if status == SnapshotStatus.AUTH_REQUIRED
            ),
        )
        self._cycle_started_at = None
        self._current_refresh_manual = False
        self._widget.set_refreshing(False)
        self._update_tray()
        self._run_profile_purges()
        self._schedule_next_refresh()
        if self._pending_manual_refresh or self._pending_manual_providers:
            QTimer.singleShot(0, self._run_pending_manual)

    def _cycle_changed(self) -> bool:
        """Did any provider this cycle visited report something new?

        Compared per provider, because a cycle no longer has to be the whole
        queue: a per-provider retry visits one tile, and comparing its single
        signature against the previous full cycle's map would read as "changed"
        every time.
        """
        if self._last_cycle_signatures is None:
            return True
        return any(
            self._last_cycle_signatures.get(name) != signature
            for name, signature in self._cycle_signatures.items()
        )

    def _update_tray(self) -> None:
        lines: list[str] = []
        for name in _enabled_providers(self._config):
            snap = self._snapshots.get(name)
            if not snap:
                continue
            display_name = display_name_for_account(self._config, name)
            if snap.status == SnapshotStatus.AUTH_REQUIRED:
                lines.append(f"{display_name}: setup needed")
                continue
            if snap.status == SnapshotStatus.ERROR:
                lines.append(f"{display_name}: error")
                continue
            for m in snap.metrics:
                if m.percent_used is None:
                    continue
                if m.tag:
                    continue
                lines.append(f"{display_name} {m.label}: {m.percent_used:.0f}%")
        tooltip = (
            f"AI Gauge {__version__}\n" + "\n".join(lines)
            if lines
            else f"AI Gauge {__version__}"
        )
        if self._native_status is not None:
            self._native_status.update(
                self._snapshots,
                _enabled_providers(self._config),
                self._config,
            )
            self._native_status.set_tooltip(tooltip)
            return
        if self._tray is None:
            return
        self._tray.setIcon(self._render_tray_icon())
        self._tray.setToolTip(tooltip)

    def _build_app_menu(self) -> QMenu:
        menu = QMenu()
        menu.addAction("Show / Hide", self._toggle_widget)
        refresh_act = menu.addAction("Refresh now")
        refresh_act.triggered.connect(lambda: self.refresh_now(manual=True))
        menu.addAction("Settings…", self.open_settings)
        menu.addSeparator()
        quit_act = QAction("Quit", menu)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_act)
        return menu

    def _render_tray_icon(self) -> QIcon:
        if self._ui_mode == "menubar":
            providers = _enabled_providers(self._config)
            pixmap = render_menubar_pixmap(
                self._snapshots, providers, config=self._config
            )
            return QIcon(pixmap)
        indicator = highest_indicator(
            self._config, self._snapshots, _enabled_providers(self._config)
        )
        return _make_dot_tray_icon(indicator.color if indicator else None)

    # ----- Login / cookie paste -----

    def open_login(self, provider: str) -> None:
        kind = account_kind(self._config, provider)
        if kind == "opencode_go":
            url = opencode_go_usage_url(self._config)
        elif kind in LOGIN_URLS:
            url, _title = LOGIN_URLS[kind]
        else:
            return
        display_name = display_name_for_account(self._config, provider)
        dlg = LoginWindow(
            kind,
            url,
            f"Sign in to {display_name}",
            account_id=provider,
            verify_url=url if kind == "opencode_go" else None,
        )
        self._widget.suspend_always_on_top()
        try:
            accepted = bool(dlg.exec())
        finally:
            self._widget.restore_always_on_top()
        if accepted:
            self.refresh_provider(provider)

    def open_cookie_paste(self, provider: str) -> None:
        kind = account_kind(self._config, provider)
        if kind is None:
            return
        try:
            dlg = CookieDialog(
                kind,
                account_id=provider,
                display_name=display_name_for_account(self._config, provider),
                verify_url=(
                    opencode_go_usage_url(self._config)
                    if kind == "opencode_go"
                    else None
                ),
            )
        except ValueError:
            return
        self._widget.suspend_always_on_top()
        try:
            accepted = bool(dlg.exec())
        finally:
            self._widget.restore_always_on_top()
        if accepted:
            # QWebEngineCookieStore commits asynchronously; give the freshly
            # injected cookie a short beat before loading the scrape page.
            QTimer.singleShot(1000, lambda: self.refresh_provider(provider))

    def open_error_details(self, provider: str) -> None:
        snapshot = self._snapshots.get(provider)
        if snapshot is None or snapshot.status != SnapshotStatus.ERROR:
            return
        display_name = display_name_for_account(self._config, provider)
        dlg = ErrorDetailsDialog(provider, display_name, snapshot, parent=self._widget)
        dlg.exec()

    def _ratio_recent(self, provider: str) -> list[float]:
        """Recent finalized sessions/week values (oldest -> newest) for tooltips."""
        out: list[float] = []
        for record in self._ratio.history(provider)[-4:]:
            value = sessions_per_week(
                record.sum_session_delta, record.sum_weekly_delta
            )
            if value is not None:
                out.append(value)
        return out

    def _current_weekly_pct(self, provider: str) -> float | None:
        snapshot = self._snapshots.get(provider)
        if snapshot is None or snapshot.status != SnapshotStatus.OK:
            return None
        for metric in snapshot.metrics:
            if metric.label.lower() == "weekly" and metric.percent_used is not None:
                return metric.percent_used
        return None

    def open_ratio_history(self, provider: str) -> None:
        display_name = display_name_for_account(self._config, provider)
        dlg = RatioHistoryDialog(
            provider,
            display_name,
            self._ratio.history(provider),
            self._ratio.current_estimate(provider),
            weekly_pct_used=self._current_weekly_pct(provider),
            parent=self._widget,
        )
        dlg.exec()

    # ----- Settings -----

    def set_instance_lock(self, lock: QLockFile | None) -> None:
        """Hold the single-instance lock so restart() can release it cleanly."""
        self._instance_lock = lock

    def restart(self) -> None:
        """Relaunch the app so a new UI scale (QT_SCALE_FACTOR) takes effect.

        QT_SCALE_FACTOR is latched when QApplication is constructed, so a scale
        change only applies on a fresh process.
        """
        log.info("restarting to apply new UI scale")
        # Release the single-instance lock first so the relaunched process can
        # acquire it without racing this one's shutdown.
        if self._instance_lock is not None:
            try:
                self._instance_lock.unlock()
            except Exception:  # noqa: BLE001
                pass
        _flush_log_handlers()
        # Drop any inherited QT_SCALE_FACTOR so the child recomputes it from the
        # freshly saved config rather than reusing this process's value.
        child_env = dict(os.environ)
        child_env.pop("QT_SCALE_FACTOR", None)
        try:
            subprocess.Popen(autostart_command(), env=child_env, close_fds=True)
        except Exception:  # noqa: BLE001
            log.exception("failed to relaunch for UI scale change")
        QApplication.instance().quit()

    def open_settings(self) -> None:
        if self._settings_dialog is not None:
            self._raise_settings_dialog()
            return
        old_copilot_quota = self._config.copilot.monthly_quota
        old_openrouter_budget = self._config.openrouter.daily_budget
        dlg = SettingsDialog(self._config, parent=self._widget)
        dlg.setModal(False)
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.sign_in_clicked.connect(self.open_login)
        dlg.paste_cookie_clicked.connect(self.open_cookie_paste)
        # The dialog has already cleared the scan timestamps; refresh so the
        # scan happens now rather than at the next scheduled cycle.
        dlg.rescan_meters_clicked.connect(lambda: self.refresh_now(manual=True))
        dlg.finished.connect(
            lambda result, dialog=dlg, old_quota=old_copilot_quota, old_budget=old_openrouter_budget: (
                self._on_settings_finished(dialog, result, old_quota, old_budget)
            )
        )
        self._settings_dialog = dlg
        self._settings_old_copilot_quota = old_copilot_quota
        self._widget.suspend_always_on_top()
        dlg.show()
        self._raise_settings_dialog()

    def _raise_settings_dialog(self) -> None:
        dlg = self._settings_dialog
        if dlg is None:
            return
        if dlg.isMinimized():
            dlg.showNormal()
        else:
            dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_settings_finished(
        self,
        dlg: SettingsDialog,
        result: int,
        old_copilot_quota: int,
        old_openrouter_budget: float | None,
    ) -> None:
        if self._settings_dialog is not dlg:
            return
        self._settings_dialog = None
        self._settings_old_copilot_quota = None
        self._widget.restore_always_on_top()
        accepted = result == QDialog.DialogCode.Accepted.value
        if accepted:
            dlg.apply_to(self._config)
            self._build_providers()
            self._purge_removed_profiles(
                getattr(dlg, "removed_profile_ids", None) or []
            )
            self._widget.apply_gauge_colors()
            # Colour-only changes must not wait for the next network refresh.
            self._update_tray()
            self._widget.apply_window_settings()
            self._widget.show()
            # Copilot's metric label bakes the quota into the displayed string.
            # If the user just changed it, re-render the cached snapshot now so
            # the new denominator shows immediately rather than after a refresh.
            new_copilot_quota = self._config.copilot.monthly_quota
            if old_copilot_quota != new_copilot_quota:
                self._rerender_copilot(new_copilot_quota)
            new_openrouter_budget = self._config.openrouter.daily_budget
            if old_openrouter_budget != new_openrouter_budget:
                self._rerender_openrouter(new_openrouter_budget)
            self._restart_timer()
            self.refresh_now(manual=True)
            if getattr(dlg, "start_at_login_error", False):
                QMessageBox.warning(
                    self._widget,
                    "Start at login",
                    "AI Gauge couldn't update the Windows start-at-login task. "
                    "Your other settings were saved.",
                )
            if getattr(dlg, "ui_scale_changed", False):
                answer = QMessageBox.question(
                    self._widget,
                    "Restart to apply scale",
                    "AI Gauge needs to restart to apply the new UI scale.\n\n"
                    "Restart now?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self.restart()
                    return
        dlg.deleteLater()

    def _on_widget_activated(self) -> None:
        if self._settings_dialog is not None:
            self._raise_settings_dialog()

    def _on_tile_expanded_changed(self, provider: str, expanded: bool) -> None:
        compact_collapsible = (
            provider == "opencode_go"
            or provider == "claude"
            or provider == "codex"
            or provider.startswith("claude-")
            or provider.startswith("codex-")
        )
        if compact_collapsible:
            current = list(getattr(self._config, "collapsed_tiles", []) or [])
            if not expanded and provider not in current:
                current.append(provider)
            elif expanded and provider in current:
                current.remove(provider)
            else:
                return
            self._config.collapsed_tiles = current
            log_name = "collapsed_tiles"
        else:
            current = list(self._config.expanded_tiles or [])
            if expanded and provider not in current:
                current.append(provider)
            elif not expanded and provider in current:
                current.remove(provider)
            else:
                return
            self._config.expanded_tiles = current
            log_name = "expanded_tiles"
        try:
            self._config.save()
        except Exception:  # noqa: BLE001
            log.exception("failed to persist %s", log_name)

    def _rerender_copilot(self, quota: int) -> None:
        cached = self._snapshots.get("copilot")
        if cached is None or cached.status != SnapshotStatus.OK or not cached.raw:
            return
        from .providers.copilot import _build_snapshot

        try:
            self._on_snapshot(_build_snapshot(cached.raw, quota))
        except Exception:  # noqa: BLE001
            log.exception("failed to re-render copilot snapshot with new quota")

    def _rerender_openrouter(self, daily_budget: float | None) -> None:
        cached = self._snapshots.get("openrouter")
        if cached is None or cached.status != SnapshotStatus.OK or not cached.raw:
            return
        from .providers.openrouter import _build_snapshot

        raw = cached.raw
        top_models = [
            (str(name), float(cost))
            for name, cost in (raw.get("top_models") or [])
        ]
        try:
            self._on_snapshot(
                _build_snapshot(
                    raw.get("credits"),
                    raw.get("key", {}) or {},
                    top_models,
                    daily_budget,
                    mgmt_key_configured=bool(raw.get("mgmt_key_configured")),
                    activity_error=raw.get("activity_error"),
                    activity_date=raw.get("activity_date"),
                )
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "failed to re-render openrouter snapshot with new daily budget"
            )

    # ----- Tray -----

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_widget()
        elif (
            self._ui_mode == "menubar"
            and reason == QSystemTrayIcon.ActivationReason.Context
        ):
            self._show_tray_menu()

    def _tray_anchor(self) -> QPoint:
        if self._native_status is not None:
            return self._native_status.anchor_point()
        if self._tray is not None:
            geo = self._tray.geometry()
            if not geo.isEmpty():
                return geo.bottomLeft()
        screen_geo = QApplication.primaryScreen().availableGeometry()
        return QPoint(screen_geo.right() - 220, screen_geo.top() + 22)

    def _show_tray_menu(self) -> None:
        anchor = self._tray_anchor()
        if anchor.isNull():
            anchor = QCursor.pos()
        self._app_menu.exec(anchor)

    def _toggle_widget(self) -> None:
        if self._widget.isVisible():
            self._widget.hide()
            return
        if self._ui_mode == "menubar":
            anchor = self._tray_anchor()
            anchor_x = anchor.x()
            anchor_y = anchor.y()
            self._widget.show_as_popover(anchor_x, anchor_y)
            return
        self._widget.show()
        self._widget.raise_()
        self._widget.activateWindow()

    def _on_widget_closed(self) -> None:
        # Closing the widget hides to tray rather than quitting
        pass


def _install_excepthook() -> None:
    """Log otherwise-fatal uncaught exceptions.

    PyQt aborts the process when an exception escapes a slot, and a windowed
    build has nowhere to print the traceback — so it vanishes silently. Routing
    it through the file log leaves a diagnosable trace.
    """
    previous = sys.excepthook

    def _hook(exc_type, exc, tb):
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = _hook


def _apply_ui_scale_env() -> None:
    """Translate the saved UI scale into QT_SCALE_FACTOR before QApplication.

    Qt latches the factor when QApplication is constructed, so a change only
    takes effect on the next launch — the Settings dialog tells the user as
    much. An explicit QT_SCALE_FACTOR in the environment always wins.
    """
    if "QT_SCALE_FACTOR" in os.environ:
        return
    try:
        factor = qt_scale_factor_env(Config.load())
    except Exception:  # noqa: BLE001 - never block startup over a scale read
        return
    if factor is not None:
        os.environ["QT_SCALE_FACTOR"] = factor


def main() -> int:
    instance_lock = _acquire_instance_lock()
    if instance_lock is None:
        return 0
    setup_logging()
    _install_excepthook()
    # QtWebEngine requires this attribute set before QApplication is constructed.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    # Importing QtWebEngineWidgets before the QApplication forces its OpenGL
    # initialisation to happen at the right time.
    from PyQt6 import QtWebEngineWidgets  # noqa: F401

    _apply_ui_scale_env()

    qt_app = QApplication(sys.argv)
    # Must be set on the live instance: a tray-resident app keeps running with
    # no visible windows, and closing a transient dialog/message box must not
    # quit it. (Called pre-construction this silently no-ops.)
    qt_app.setQuitOnLastWindowClosed(False)
    qt_app.setApplicationName("ai-gauge")
    qt_app.setOrganizationName("ai-gauge")
    qt_app.setApplicationVersion(__version__)
    _app = App()  # noqa: F841 - keeps refs alive
    _app.set_instance_lock(instance_lock)
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
