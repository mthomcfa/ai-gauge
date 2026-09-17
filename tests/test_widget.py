from datetime import datetime, timedelta

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QResizeEvent
from PyQt6.QtWidgets import QApplication

from aigauge.config import (
    BrowserAccount,
    Config,
    config_path,
    WINDOW_DEFAULT_HEIGHT,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_WIDTH,
)
from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot
from aigauge.ratio import RatioEstimate
from aigauge.ui_style import SCROLLBAR_WIDTH
from aigauge.widget import (
    NATIVE_GESTURE_COMMIT_MS,
    UsageWidget,
    _format_ratio_inline,
    _MetricRow,
    _SummaryChip,
)


def _ok_snapshot(provider: str) -> UsageSnapshot:
    fetched = datetime(2026, 4, 27, 12, 0)
    return UsageSnapshot(
        provider=provider,
        status=SnapshotStatus.OK,
        metrics=[
            UsageMetric("Session", 40.0, fetched + timedelta(hours=2)),
            UsageMetric("Weekly", 20.0, fetched + timedelta(days=5)),
        ],
        fetched_at=fetched,
    )


def _estimate(confident: bool, n: float | None, source: str = "current") -> RatioEstimate:
    return RatioEstimate(
        sessions_per_week=n if confident else None,
        weekly_pct_per_session=(100.0 / n) if (confident and n) else None,
        coverage_pct=40.0,
        sample_count=12,
        confident=confident,
        source=source,
    )


def _tile_order(widget: UsageWidget) -> list[str]:
    return [
        widget._tile_layout.itemAt(i).widget().provider
        for i in range(widget._tile_layout.count())
    ]


def _collapsed_chip_texts(widget: UsageWidget) -> list[str]:
    texts = []
    stack = [widget._collapsed_summary_layout]  # noqa: SLF001
    while stack:
        layout = stack.pop(0)
        for i in range(layout.count()):
            item = layout.itemAt(i)
            child = item.widget()
            if child is not None and child is not widget._collapsed_label:  # noqa: SLF001
                if hasattr(child, "text"):
                    texts.append(child.text())
                if child.layout() is not None:
                    stack.append(child.layout())
    return texts


def test_offscreen_saved_position_is_clamped_on_screen(qtbot):
    """A position saved at a lower display scale can land off the (smaller)
    logical desktop at 175%/200%; the widget must reappear fully on-screen."""
    geo = QApplication.primaryScreen().availableGeometry()
    config = Config()
    # Far past the bottom-right corner, as a high-DPI logical shrink would do
    # to coordinates captured at 100%.
    config.window.x = geo.right() + 5000
    config.window.y = geo.bottom() + 5000

    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    assert widget.x() >= geo.left()
    assert widget.y() >= geo.top()
    assert widget.x() + widget.width() <= geo.right() + 1
    assert widget.y() + widget.height() <= geo.bottom() + 1


def test_reenabled_provider_returns_to_canonical_order(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.ensure_tile("claude", "Claude")
    widget.ensure_tile("codex", "Codex")
    widget.ensure_tile("copilot", "Copilot")
    widget.remove_tile("codex")
    widget.ensure_tile("codex", "Codex")

    assert _tile_order(widget) == ["claude", "codex", "copilot"]


def test_microsoft_tiles_render_as_a_pair(qtbot):
    """Copilot and Azure are the two halves of the Microsoft section, so they
    must sit next to each other however the tiles happen to be created."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.ensure_tile("openrouter", "OpenRouter")
    widget.ensure_tile("azure", "Microsoft · Azure")
    widget.ensure_tile("claude", "Claude")
    widget.ensure_tile("copilot", "Copilot")

    order = _tile_order(widget)
    assert order == ["claude", "copilot", "azure", "openrouter"]


def test_azure_summary_chip_uses_the_short_name(qtbot):
    """The tile header has room for "Microsoft · Azure"; a chip does not, and a
    chip that wraps costs a whole row in the collapsed panel."""
    from aigauge.widget import UsageWidget as _UsageWidget

    widget = _UsageWidget(Config())
    qtbot.addWidget(widget)
    chip = widget._summary_chip("azure")  # noqa: SLF001
    qtbot.addWidget(chip)

    assert chip._text.startswith("Azure")  # noqa: SLF001
    assert "Microsoft" not in chip._text  # noqa: SLF001


def test_browser_account_tiles_group_by_provider_kind(qtbot):
    config = Config()
    config.browser_accounts.append(
        BrowserAccount(id="claude-team", kind="claude", name="Team")
    )
    config.browser_accounts.append(
        BrowserAccount(id="codex-work", kind="codex", name="Work")
    )
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    widget.ensure_tile("codex-work", "Codex (Work)")
    widget.ensure_tile("claude-team", "Claude (Team)")
    widget.ensure_tile("codex", "Codex")
    widget.ensure_tile("claude", "Claude")
    widget.ensure_tile("copilot", "Copilot")

    assert _tile_order(widget) == [
        "claude",
        "claude-team",
        "codex",
        "codex-work",
        "copilot",
    ]


def test_format_ratio_inline_states():
    assert _format_ratio_inline(None) is None
    assert _format_ratio_inline(_estimate(True, 9.24)) == "~9.2/wk"
    assert _format_ratio_inline(_estimate(False, None)) == "burn ~?"


def test_ratio_label_shows_when_ok_and_confident(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget.set_ratio("claude", _estimate(True, 9.2), recent=[10.0, 9.5, 9.2])

    label = widget._tiles["claude"].ratio_label  # noqa: SLF001
    assert not label.isHidden()
    assert "9.2/wk" in label.text()
    assert "sessions/week" in label.toolTip()


def test_ratio_label_calibrating_placeholder(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("codex"), "Codex")
    calibrating = RatioEstimate(
        sessions_per_week=None,
        weekly_pct_per_session=None,
        coverage_pct=1.2,
        sample_count=2,
        confident=False,
        source="current",
        session_delta=12.0,
    )
    widget.set_ratio("codex", calibrating, recent=[])

    label = widget._tiles["codex"].ratio_label  # noqa: SLF001
    assert not label.isHidden()
    assert "burn ~?" in label.text()
    tip = label.toolTip().lower()
    assert "calibrating" in tip
    # Calibration progress is visible so the wait is not a mystery.
    assert "12/30" in label.toolTip()
    assert "2/3" in label.toolTip()


def test_ratio_label_carry_over_dimmed_with_progress(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    last_week = _estimate(True, 9.2, source="history")
    this_week = RatioEstimate(
        sessions_per_week=None,
        weekly_pct_per_session=None,
        coverage_pct=1.0,
        sample_count=1,
        confident=False,
        source="current",
        session_delta=6.0,
    )
    widget.set_ratio("claude", last_week, recent=[9.5, 9.2], live=this_week)

    label = widget._tiles["claude"].ratio_label  # noqa: SLF001
    assert not label.isHidden()
    assert "9.2/wk°" in label.text()  # carry-over marker
    assert "#6b7280" in label.text()  # dimmed color
    tip = label.toolTip()
    assert "Last week" in tip
    assert "This week calibrating" in tip
    assert "6/30" in tip


def test_ratio_label_hidden_when_not_ok(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in.",
            fetched_at=datetime(2026, 4, 27, 12, 0),
        ),
        "Claude",
    )
    # Even a confident estimate must not show on a non-OK tile.
    widget.set_ratio("claude", _estimate(True, 9.2), recent=[9.2])

    label = widget._tiles["claude"].ratio_label  # noqa: SLF001
    assert label.isHidden()


def test_ratio_history_signal_emitted_on_link_click(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget.set_ratio("claude", _estimate(True, 9.2), recent=[9.2])

    seen: list[str] = []
    widget.ratio_history_requested.connect(seen.append)
    widget._tiles["claude"].ratio_label.linkActivated.emit("ratio-history")  # noqa: SLF001
    assert seen == ["claude"]


def test_mark_loading_preserves_existing_data_and_dims_tile(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    fetched = datetime(2026, 4, 27, 12, 0)

    widget.update_snapshot(
        UsageSnapshot(
            provider="codex",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric("Session", 47.0, fetched + timedelta(hours=2)),
            ],
            fetched_at=fetched,
        ),
        "Codex",
    )
    assert len(widget._tiles["codex"]._rows) == 1  # noqa: SLF001

    widget.mark_loading({"codex": "Codex"})

    tile = widget._tiles["codex"]  # noqa: SLF001
    # Prior data stays on screen — only the visual "refreshing" flag flips.
    assert len(tile._rows) == 1  # noqa: SLF001
    assert tile._rows[0].pct.text() == "47%"  # noqa: SLF001
    assert tile._refreshing is True  # noqa: SLF001
    # And the cached snapshot is intact so collapsed chips keep their values.
    assert widget._snapshots["codex"] is not None  # noqa: SLF001


def test_mark_loading_shows_skeleton_when_no_prior_data(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.mark_loading({"claude": "Claude", "codex": "Codex", "copilot": "Copilot"})
    widget._do_refit_height()  # noqa: SLF001

    for provider in ("claude", "codex", "copilot"):
        tile = widget._tiles[provider]  # noqa: SLF001
        assert tile._refreshing is True  # noqa: SLF001
        assert len(tile._rows) == 1  # noqa: SLF001
        # Indeterminate range == busy mode (animated stripe).
        assert tile._rows[0].bar.maximum() == 0  # noqa: SLF001

    assert widget._header_widget.height() <= widget._header_widget.sizeHint().height() + 2  # noqa: SLF001
    assert widget._tile_container.height() <= widget._tile_container.sizeHint().height() + 2  # noqa: SLF001


def test_update_snapshot_clears_refreshing_flag(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.mark_loading({"codex": "Codex"})
    assert widget._tiles["codex"]._refreshing is True  # noqa: SLF001

    widget.update_snapshot(
        UsageSnapshot(
            provider="codex",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric("Session", 50.0, None)],
        ),
        "Codex",
    )

    assert widget._tiles["codex"]._refreshing is False  # noqa: SLF001
    # The previously-skeleton row is now a real metric row.
    assert widget._tiles["codex"]._rows[0].bar.maximum() == 100  # noqa: SLF001


def test_a_parked_refresh_hint_is_cleared_by_the_next_paint(qtbot):
    """The hint is the whole of what a refused manual refresh does.

    The snapshot the tile is showing, its rows and its burn rate are
    untouched - nothing was measured - and the next answer that really
    arrives rewrites the status label, which is what clears it.
    """
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("codex"), "Codex")
    tile = widget._tiles["codex"]  # noqa: SLF001
    rows_before = len(tile._rows)  # noqa: SLF001
    assert tile.status.text() == ""

    hint = "Waiting for the previous refresh to finish."
    widget.set_status_hint("codex", hint)

    assert tile.status.toolTip() == hint
    assert tile.status.text() == hint
    assert tile._latest_snapshot is not None  # noqa: SLF001
    assert len(tile._rows) == rows_before, "a refusal changed the metric rows"  # noqa: SLF001

    widget.update_snapshot(_ok_snapshot("codex"), "Codex")

    assert tile.status.toolTip() == ""
    assert tile.status.text() == ""


def test_a_parked_refresh_hint_keeps_an_error_tiles_own_label(qtbot):
    """An ERROR tile's status label is the link to the error details, and an
    AUTH_REQUIRED one says "not signed in" - the one thing on the tile the
    user can act on. The hint goes to the tooltip there rather than over the
    top of it."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(
        UsageSnapshot(
            provider="copilot", status=SnapshotStatus.ERROR, error="Refresh timed out."
        ),
        "Copilot",
    )
    tile = widget._tiles["copilot"]  # noqa: SLF001
    label_before = tile.status.text()

    widget.set_status_hint("copilot", "Waiting for the previous refresh to finish.")

    assert tile.status.text() == label_before
    assert tile.status.toolTip() == "Waiting for the previous refresh to finish."


def test_auth_required_tile_uses_sign_in_button(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.update_snapshot(
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in.",
        ),
        "Claude",
    )

    tile = widget._tiles["claude"]  # noqa: SLF001
    assert tile.action_btn.text() == "Sign in"
    assert not tile.action_btn.isHidden()


def test_secondary_browser_account_auth_tile_uses_sign_in_button(qtbot):
    config = Config()
    config.browser_accounts.append(
        BrowserAccount(id="codex-work", kind="codex", name="Work")
    )
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    widget.update_snapshot(
        UsageSnapshot(
            provider="codex-work",
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in.",
        ),
        "Codex (Work)",
    )

    tile = widget._tiles["codex-work"]  # noqa: SLF001
    assert tile.action_btn.text() == "Sign in"
    assert not tile.action_btn.isHidden()




def test_opencode_go_auth_tile_uses_sign_in_button(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.update_snapshot(
        UsageSnapshot(
            provider="opencode_go",
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in.",
        ),
        "OpenCode",
    )

    tile = widget._tiles["opencode_go"]  # noqa: SLF001
    assert tile.action_btn.text() == "Sign in"
    assert not tile.action_btn.isHidden()

def test_sign_in_button_emits_sign_in_signal(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.update_snapshot(
        UsageSnapshot(
            provider="codex",
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in.",
        ),
        "Codex",
    )

    with qtbot.waitSignal(widget.sign_in_requested) as signal:
        widget._tiles["codex"].action_btn.click()  # noqa: SLF001

    assert signal.args == ["codex"]




def test_browser_tile_collapses_to_inline_metric_chips(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    reset_at = datetime.now() + timedelta(hours=2, minutes=30)

    widget.update_snapshot(
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric("Session", 85.0, reset_at),
                UsageMetric("Weekly", 9.0, reset_at + timedelta(days=4)),
            ],
        ),
        "Claude",
    )

    tile = widget._tiles["claude"]  # noqa: SLF001
    assert not tile.expand_btn.isHidden()
    assert [row.label.text() for row in tile._rows] == ["Session", "Weekly"]  # noqa: SLF001

    tile.set_expanded(False)

    assert tile._rows == []  # noqa: SLF001
    assert not tile._compact_summary.isHidden()  # noqa: SLF001
    assert [item.code.text() for item in tile._compact_metrics] == ["S", "W"]  # noqa: SLF001
    assert [item.pct.text() for item in tile._compact_metrics] == ["85%", "9%"]  # noqa: SLF001
    assert tile._compact_metrics[0].bar.value() == 85  # noqa: SLF001


def test_collapsed_browser_tile_hides_ratio_label(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("codex"), "Codex")
    tile = widget._tiles["codex"]  # noqa: SLF001

    widget.set_ratio("codex", _estimate(True, 6.7), [], _estimate(True, 6.7))
    assert not tile.ratio_label.isHidden()

    tile.set_expanded(False)
    widget.set_ratio("codex", _estimate(True, 6.7), [], _estimate(True, 6.7))

    assert tile.ratio_label.isHidden()


def test_ratio_label_returns_after_browser_tile_reexpanded(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    tile = widget._tiles["claude"]  # noqa: SLF001

    widget.set_ratio("claude", _estimate(True, 9.2), [9.2], _estimate(True, 9.2))
    assert "9.2/wk" in tile.ratio_label.text()

    tile.set_expanded(False)
    assert tile.ratio_label.isHidden()

    tile.set_expanded(True)
    assert not tile.ratio_label.isHidden()
    assert "9.2/wk" in tile.ratio_label.text()


def test_configured_collapsed_browser_tile_starts_compact(qtbot):
    config = Config()
    config.collapsed_tiles = ["claude"]
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    widget.update_snapshot(_ok_snapshot("claude"), "Claude")

    tile = widget._tiles["claude"]  # noqa: SLF001
    assert tile._rows == []  # noqa: SLF001
    assert not tile._compact_summary.isHidden()  # noqa: SLF001


def test_opencode_go_collapsed_tile_shows_rolling_and_monthly_inline_metrics(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    reset_at = datetime.now() + timedelta(hours=4)

    widget.update_snapshot(
        UsageSnapshot(
            provider="opencode_go",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric("Rolling", 13.0, reset_at),
                UsageMetric("Weekly", 14.0, reset_at + timedelta(days=3)),
                UsageMetric("Monthly", 7.0, reset_at + timedelta(days=30)),
            ],
        ),
        "OpenCode",
    )

    tile = widget._tiles["opencode_go"]  # noqa: SLF001
    tile.set_expanded(False)

    assert [item.code.text() for item in tile._compact_metrics] == ["R", "M"]  # noqa: SLF001
    assert [item.pct.text() for item in tile._compact_metrics] == ["13%", "7%"]  # noqa: SLF001

def test_refresh_state_shows_next_refresh_countdown(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.set_refresh_state(
        active=True,
        minutes=5,
        next_at=datetime.now() + timedelta(minutes=3, seconds=5),
    )

    assert widget.cadence_label.text() == "· active next 4m"
    assert "5 min cadence" in widget.cadence_label.toolTip()


def test_refresh_state_shows_now_when_next_refresh_is_due(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.set_refresh_state(
        active=False,
        minutes=60,
        next_at=datetime.now() - timedelta(seconds=1),
    )

    assert widget.cadence_label.text() == "· idle next now"


def test_the_refreshing_header_survives_the_one_second_tick(qtbot):
    """The refreshing state used to last at most one second.

    set_refreshing wrote "refreshing…" into the header, and the 1 Hz tick that
    keeps the age and countdown live rewrote both labels from _last_fetch_at
    and _next_refresh_at with no regard for it. Because _schedule_next_refresh
    early-returns while a cycle is in flight, _next_refresh_at was stale and
    in the past - so the header read "· active next now" for the whole cycle,
    which the log says ran for a median of 48 s and a p90 of 79 s.
    """
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.set_refresh_state(
        active=True, minutes=5, next_at=datetime.now() - timedelta(seconds=1)
    )

    widget.set_refreshing(True, total=6)
    widget._refresh_header_labels()  # noqa: SLF001 - what the 1 Hz tick calls

    assert widget.age_label.text() == "refreshing…"
    assert "refreshing" in widget.cadence_label.text()
    assert "next now" not in widget.cadence_label.text()


def test_the_header_counts_the_cycle_off_as_it_goes(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.set_refreshing(True, total=6)
    widget.set_refresh_progress(2, 6)

    assert widget.cadence_label.text() == "· refreshing 2/6"


def test_the_countdown_comes_back_when_the_cycle_ends(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.set_refresh_state(
        active=True,
        minutes=5,
        next_at=datetime.now() + timedelta(minutes=3, seconds=5),
    )

    widget.set_refreshing(True, total=2)
    widget.set_refreshing(False)

    assert widget.cadence_label.text() == "· active next 4m"


def test_a_scheduled_cycle_marks_its_tiles_without_blanking_them(qtbot):
    """Scheduled cycles showed nothing at all: mark_loading was manual-only,
    so the only evidence of a refresh was numbers changing one at a time."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")

    widget.mark_loading({"claude": "Claude"}, subtle=True)

    tile = widget._tiles["claude"]  # noqa: SLF001
    assert tile._refreshing is True  # noqa: SLF001
    # Still populated: a scheduled refresh must not blank a tile that has data.
    assert tile._latest_snapshot is not None  # noqa: SLF001
    # And more legible than the manual dim, which is a deliberate 0.55.
    assert tile._opacity_anim.endValue() > 0.55  # noqa: SLF001


def test_an_extreme_saved_size_is_shrunk_to_the_screen(qtbot):
    """Replaces test_widget_uses_fixed_width_despite_extreme_saved_size.

    The width was pinned to 340 from 1.0 to 1.3.2; from 1.4.0+cfa.8 it is the
    user's, so the answer to a 5000-px saved width is no longer "ignore it"
    but "fit it to the monitor". WindowState bounds it to 4096 on load and the
    widget shrinks it to the work area at construction.
    """
    config = Config()
    config.window.width = 5000
    config.window.height = 2

    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    geo = (widget.screen() or QApplication.primaryScreen()).availableGeometry()
    assert widget.width() <= geo.width()
    assert widget.height() <= geo.height()
    assert widget.width() >= WINDOW_MIN_WIDTH
    assert widget.height() >= WINDOW_MIN_HEIGHT
    assert widget.x() >= geo.left() and widget.y() >= geo.top()


def test_refit_leaves_a_user_chosen_width_alone(qtbot):
    """Replaces test_refit_restores_fixed_width_after_dpi_resize_glitch.

    Re-fitting used to restore the fixed width on every tile change, which is
    precisely what would undo a drag now. It only ever touches the height, and
    only while the window is still the app's to size.
    """
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.resize(480, 120)

    widget._do_refit_height()  # noqa: SLF001

    assert widget.width() == 480


def test_collapsed_mode_shows_session_summary(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.update_snapshot(
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric("Session", 50.0, None),
                UsageMetric("Weekly", 12.0, None),
            ],
        ),
        "Claude",
    )
    widget.update_snapshot(
        UsageSnapshot(
            provider="codex",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric("Session", 0.0, None),
                UsageMetric("Weekly", 15.0, None),
            ],
        ),
        "Codex",
    )
    widget.update_snapshot(
        UsageSnapshot(
            provider="copilot",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric("Premium (1434/1500)", 96.0, None)],
        ),
        "Copilot",
    )

    widget.set_collapsed(True)

    assert not widget._collapsed_widget.isHidden()  # noqa: SLF001
    assert widget._tile_container.isHidden()  # noqa: SLF001
    assert _collapsed_chip_texts(widget) == ["Claude 50%", "Codex 0%", "Copilot 96%"]
    assert all("Weekly" not in text for text in _collapsed_chip_texts(widget))


def test_error_snapshot_can_show_stale_metrics(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.update_snapshot(
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.ERROR,
            error="extractor retry limit exceeded",
            metrics=[
                UsageMetric("Session", 50.0, None),
                UsageMetric("Weekly", 12.0, None),
            ],
        ),
        "Claude",
    )

    tile = widget._tiles["claude"]  # noqa: SLF001
    assert "error · stale" in tile.status.text()
    assert [row.label.text() for row in tile._rows] == ["Session", "Weekly"]  # noqa: SLF001
    assert tile._rows[0].pct.text() == "50%"  # noqa: SLF001

    widget.set_collapsed(True)

    assert _collapsed_chip_texts(widget) == ["Claude 50% stale"]


def test_collapsed_mode_shows_openrouter_balance(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.update_snapshot(
        UsageSnapshot(
            provider="openrouter",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric("Balance $11.50 left · Today $1.31", None)],
        ),
        "OpenRouter",
    )

    widget.set_collapsed(True)

    assert _collapsed_chip_texts(widget) == ["OpenRouter $11.50"]


def test_collapsed_mode_shows_openrouter_today_without_balance(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)

    widget.update_snapshot(
        UsageSnapshot(
            provider="openrouter",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric(
                    "Spend today $1.31 / month $21.90",
                    None,
                )
            ],
        ),
        "OpenRouter",
    )

    widget.set_collapsed(True)

    assert _collapsed_chip_texts(widget) == ["OpenRouter today $1.31"]


def test_collapsed_mode_resizes_immediately(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.resize(340, 260)

    widget.set_collapsed(True)

    assert widget.height() == 58

def test_openrouter_model_expand_resizes_immediately(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(
        UsageSnapshot(
            provider="openrouter",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric("Balance $11.50 left · Spend today $0.00", None),
                UsageMetric("Models: last 30 completed UTC days", None, tag="models"),
                UsageMetric("claude-sonnet-4", 42.0, tag="models"),
                UsageMetric("gpt-4.1", 21.0, tag="models"),
                UsageMetric("gemini-pro", 18.0, tag="models"),
            ],
        ),
        "OpenRouter",
    )
    widget._do_refit_height()  # noqa: SLF001
    collapsed_height = widget.height()

    widget._tiles["openrouter"].set_expanded(True)  # noqa: SLF001
    qtbot.waitUntil(lambda: widget.height() > collapsed_height)
    expanded_height = widget.height()

    widget._tiles["openrouter"].set_expanded(False)  # noqa: SLF001
    qtbot.waitUntil(lambda: widget.height() < expanded_height)


def test_collapsed_mode_wraps_all_account_chips_without_overflow(qtbot):
    config = Config()
    for i in range(2, 6):
        config.browser_accounts.append(
            BrowserAccount(id=f"codex-{i}", kind="codex", name=f"Account {i}")
        )
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    for account in config.browser_accounts:
        widget.update_snapshot(
            UsageSnapshot(
                provider=account.id,
                status=SnapshotStatus.OK,
                metrics=[UsageMetric("Session", 25.0, None)],
            ),
            f"Codex ({account.name})" if account.name else "Codex",
        )

    widget.set_collapsed(True)
    widget._do_refit_height()  # noqa: SLF001

    texts = _collapsed_chip_texts(widget)
    assert "+4" not in texts
    assert len(texts) == len(config.browser_accounts)
    assert "Account 2 25%" in texts
    assert all("Codex (Account" not in text for text in texts)
    assert widget.height() > 58


def test_collapsed_mode_persists_and_expands(qtbot):
    config = Config()
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    widget.set_collapsed(True)
    assert config.window.collapsed is True

    widget.set_collapsed(False)
    assert config.window.collapsed is False
    assert widget._collapsed_widget.isHidden()  # noqa: SLF001
    assert not widget._tile_container.isHidden()  # noqa: SLF001


def test_always_on_top_suspension_is_reference_counted(qtbot):
    config = Config()
    config.window.always_on_top = True
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    assert widget.windowFlags() & Qt.WindowType.WindowStaysOnTopHint

    widget.suspend_always_on_top()
    widget.suspend_always_on_top()
    assert not widget.windowFlags() & Qt.WindowType.WindowStaysOnTopHint

    widget.restore_always_on_top()
    assert not widget.windowFlags() & Qt.WindowType.WindowStaysOnTopHint

    widget.restore_always_on_top()
    assert widget.windowFlags() & Qt.WindowType.WindowStaysOnTopHint

def test_widget_is_solid_when_fade_disabled(qtbot):
    config = Config()
    config.window.fade_when_inactive = False
    config.window.opacity = 0.4
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    assert widget._target_window_opacity() == 1.0  # noqa: SLF001
    assert widget.windowOpacity() == 1.0


def test_widget_fades_when_inactive_and_restores_on_hover(qtbot):
    config = Config()
    config.window.fade_when_inactive = True
    config.window.opacity = 0.45
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    assert widget._target_window_opacity() == 0.45  # noqa: SLF001
    assert widget.windowOpacity() == pytest.approx(0.45, abs=1 / 255)

    widget._mouse_inside = True  # noqa: SLF001
    widget._apply_window_opacity()  # noqa: SLF001

    assert widget._target_window_opacity() == 1.0  # noqa: SLF001
    assert widget.windowOpacity() == 1.0

def test_metric_row_sets_pace_from_window(qtbot):
    row = _MetricRow()
    qtbot.addWidget(row)

    row.set_metric(
        "Session",
        47.0,
        datetime.now() + timedelta(hours=1),
        window=timedelta(hours=5),
    )

    assert row.bar._pace_pct == pytest.approx(80, abs=1)  # noqa: SLF001
    assert "Time elapsed:" in row.bar.toolTip()


def test_metric_row_renders_note_only_metric_without_empty_gauge(qtbot):
    row = _MetricRow()
    qtbot.addWidget(row)

    row.set_metric("Models: none", None, None, note="No activity.")

    assert row.label.text() == "Models: none"
    assert row.bar.isHidden()
    assert row.pct.isHidden()
    assert row.reset.isHidden()


def test_metric_row_right_aligns_split_note_metric(qtbot):
    row = _MetricRow()
    qtbot.addWidget(row)

    row.set_metric(
        "Balance $11.50 left · Spend today $0.00 / month $0.00",
        None,
        None,
        note="OpenRouter summary.",
    )

    assert row.label.text() == "Balance $11.50 left"
    assert row.reset.text() == "Spend today $0.00 / month $0.00"
    assert row.reset.width() > 92
    assert row.reset.toolTip() == ""
    assert "#d1d5db" in row.reset.styleSheet()
    assert row.bar.isHidden()
    assert row.pct.isHidden()
    assert not row.reset.isHidden()


def test_metric_row_keeps_timeline_bar_without_missing_percent(qtbot):
    row = _MetricRow()
    qtbot.addWidget(row)

    row.set_metric(
        "Today ($0.00/$5.00)",
        None,
        datetime.now() + timedelta(hours=8),
        window=timedelta(days=1),
    )

    assert not row.bar.isHidden()
    assert row.pct.isHidden()
    assert not row.reset.isHidden()


def test_summary_chip_stores_pace(qtbot):
    chip = _SummaryChip()
    qtbot.addWidget(chip)

    chip.set_state("Claude 37%", 37.0, "ok", pace=37)

    assert chip._pace_pct == 37  # noqa: SLF001


def test_metric_row_bar_clamps_overage_but_label_keeps_real_value(qtbot):
    # Copilot can exceed 100% of the included allowance. QProgressBar leaves an
    # above-maximum value unpainted, so the bar must clamp while the percentage
    # label still reports the true overage.
    from aigauge.widget import _MetricRow

    row = _MetricRow()
    qtbot.addWidget(row)
    row.set_metric("Premium (1650/1500)", 110.0, None)

    assert row.bar._bar.value() == 100
    assert row.pct.text() == "110%"


def test_tile_bar_uses_configured_account_colors(qtbot):
    from aigauge.config import ColorThresholds, Config
    from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot
    from aigauge.widget import UsageWidget

    config = Config()
    config.browser_accounts[0].colors = ColorThresholds(
        green_max=5, yellow_max=6, orange_max=7, red_color="#123456"
    )
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.OK,
        metrics=[UsageMetric(label="Session", percent_used=50.0)],
    )
    widget.update_snapshot(snapshot, "Claude")

    tile = widget._tiles["claude"]  # noqa: SLF001
    # 50% is green under the defaults but red under these cutoffs.
    assert "#123456" in tile._rows[0].bar._bar.styleSheet()  # noqa: SLF001


def test_apply_gauge_colors_repaints_existing_tiles(qtbot):
    from aigauge.config import ColorThresholds, Config
    from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot
    from aigauge.widget import UsageWidget

    config = Config()
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric(label="Session", percent_used=50.0)],
        ),
        "Claude",
    )
    tile = widget._tiles["claude"]  # noqa: SLF001
    assert "#22c55e" in tile._rows[0].bar._bar.styleSheet()  # noqa: SLF001

    config.browser_accounts[0].colors = ColorThresholds(green_color="#abcdef")
    widget.apply_gauge_colors()

    assert "#abcdef" in tile._rows[0].bar._bar.styleSheet()  # noqa: SLF001


def _colored_config(**kwargs):
    from aigauge.config import ColorThresholds, Config

    config = Config()
    config.browser_accounts[0].colors = ColorThresholds(**kwargs)
    return config


def test_compact_metrics_receive_account_colors(qtbot):
    """Guards the mutation where _set_compact_metrics stops propagating colours."""
    from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot
    from aigauge.widget import UsageWidget

    config = _colored_config(green_max=1, yellow_max=2, orange_max=3, red_color="#654321")
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric(label="Session", percent_used=50.0)],
        ),
        "Claude",
    )
    tile = widget._tiles["claude"]  # noqa: SLF001
    tile.set_expanded(False, emit=False)

    compact = tile._compact_metrics  # noqa: SLF001
    assert compact, "expected a compact metric row"
    assert "#654321" in compact[0].bar.styleSheet()


def test_summary_chip_receives_account_colors(qtbot):
    """Guards the mutation where _summary_chip stops passing thresholds."""
    from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot
    from aigauge.widget import UsageWidget

    config = _colored_config(green_max=1, yellow_max=2, orange_max=3, red_color="#654321")
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric(label="Session", percent_used=50.0)],
        ),
        "Claude",
    )
    chip = widget._summary_chip("claude")  # noqa: SLF001
    qtbot.addWidget(chip)

    from PyQt6.QtGui import QColor

    expected = QColor("#654321").darker(135)
    assert chip._fill_color.name() == expected.name()  # noqa: SLF001


def test_metric_row_sizes_a_long_reset_label_to_fit(qtbot):
    """A reset_label is normally a countdown, but Azure puts the money there
    so the amounts stay on the row when the tile is collapsed. The 58 px
    countdown column would clip it."""
    row = _MetricRow()
    qtbot.addWidget(row)

    row.set_metric(
        "Spend this month",
        24.0,
        datetime.now() + timedelta(days=20),
        "CAD 36.10 of 150.00 · resets 1 Oct",
        note="…",
        window=timedelta(days=30),
    )

    # Whether the whole string fits depends on the platform's font metrics -
    # Windows draws this wider than Linux and elides it - so the contract is
    # what holds everywhere: the column widened past the countdown width, what
    # is drawn fits inside it and starts with the amounts, and the full text
    # is reachable from the tooltip.
    label = "CAD 36.10 of 150.00 · resets 1 Oct"
    drawn = row.reset.text()
    assert not row.reset.isHidden()
    assert row.reset.width() > 58
    assert row.reset.fontMetrics().horizontalAdvance(drawn) <= row.reset.width()
    assert drawn == label or (drawn.startswith("CAD 36.10 of 150.00") and drawn != label)
    assert label in row.reset.toolTip()


def test_a_long_reset_label_is_elided_and_kept_in_the_tooltip(qtbot):
    """A currency with no two-digit magnitude, or an allowance above ~1e6,
    ran past the column and was clipped mid-string with the amounts nowhere
    else in the UI."""
    row = _MetricRow()
    qtbot.addWidget(row)
    label = "JPY 12,345,678.00 of 50,000,000.00 · resets 28 Feb"

    row.set_metric(
        "Spend this month",
        24.0,
        datetime.now() + timedelta(days=20),
        label,
        note="Data as of 2026-09-08.",
        window=timedelta(days=30),
    )

    drawn = row.reset.text()
    assert row.reset.fontMetrics().horizontalAdvance(drawn) <= row.reset.width()
    assert drawn != label, "the label fitted, so this test proves nothing"
    assert drawn.startswith("JPY 12,345,678.00"), "elided from the wrong end"
    tooltip = row.reset.toolTip()
    assert label in tooltip
    assert "Data as of 2026-09-08." in tooltip


def test_metric_row_keeps_the_narrow_countdown_column_for_a_countdown(qtbot):
    row = _MetricRow()
    qtbot.addWidget(row)
    row.set_metric("Weekly", 20.0, datetime.now() + timedelta(days=2), "idle")
    assert row.reset.width() == 58


# --- Resizing ---------------------------------------------------------------
#
# Offscreen geometry is a single 800x800 screen with a 14 px font, and
# QWindow.startSystemResize()/startSystemMove() both return False there, which
# is exactly the platform the pure-Qt fallback exists for. Every resize below
# therefore goes through the fallback path.


def _press(widget, point, button=Qt.MouseButton.LeftButton):
    widget.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(point),
            QPointF(widget.mapToGlobal(point)),
            button,
            button,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def _move_to(widget, global_point):
    widget.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(widget.mapFromGlobal(global_point)),
            QPointF(global_point),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def _release(widget, point):
    widget.mouseReleaseEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(point),
            QPointF(widget.mapToGlobal(point)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def _drag_corner(widget, dx, dy):
    """Drag the bottom-right corner by (dx, dy) through the fallback path."""
    grab = QPoint(widget.width() - 2, widget.height() - 2)
    _press(widget, grab)
    _move_to(widget, widget.mapToGlobal(grab) + QPoint(dx, dy))
    _release(widget, QPoint(widget.width() - 2, widget.height() - 2))


@pytest.mark.parametrize(
    "point,expected",
    [
        ((3, 150), Qt.Edge.LeftEdge),
        ((337, 150), Qt.Edge.RightEdge),
        ((170, 3), Qt.Edge.TopEdge),
        ((170, 297), Qt.Edge.BottomEdge),
        ((2, 2), Qt.Edge.LeftEdge | Qt.Edge.TopEdge),
        ((338, 2), Qt.Edge.RightEdge | Qt.Edge.TopEdge),
        ((2, 298), Qt.Edge.LeftEdge | Qt.Edge.BottomEdge),
        ((338, 298), Qt.Edge.RightEdge | Qt.Edge.BottomEdge),
        ((170, 150), Qt.Edge(0)),
    ],
    ids=["left", "right", "top", "bottom", "tl", "tr", "bl", "br", "middle"],
)
def test_edge_hit_testing_names_all_eight_zones(point, expected):
    from aigauge.widget import _edges_for_point

    assert _edges_for_point(QPoint(*point), 340, 300) == expected


def test_a_resize_is_saved_to_config_on_release(qtbot):
    config = Config()
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget.move(50, 50)
    before = widget.size()

    _drag_corner(widget, 120, 60)

    assert widget.width() == before.width() + 120
    assert widget.height() == before.height() + 60
    assert config.window.width == widget.width()
    assert config.window.height == widget.height()


def test_the_saved_size_is_restored_on_the_next_construction(qtbot):
    """And is still there once the deferred re-fit has run.

    One `qtbot.wait(0)` is not enough: `_refit_height` posts through
    `QTimer.singleShot(0, ...)` and the tile add posts its own layout work, so
    the size an auto-fit would overwrite was asserted before auto-fit had had
    a turn - the assertion held with `_user_sized` broken.
    """
    config = Config()
    config.window.width = 512
    config.window.height = 333
    config.window.user_sized = True

    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    qtbot.wait(0)
    qtbot.wait(0)

    assert (widget.width(), widget.height()) == (512, 333), (
        "auto-fit overrode a size the user had chosen"
    )

    with qtbot.waitExposed(widget):
        widget.show()
    qtbot.wait(0)
    qtbot.wait(0)
    assert (widget.width(), widget.height()) == (512, 333)


def test_a_saved_size_larger_than_the_screen_is_shrunk_and_moved_on(qtbot):
    config = Config()
    geo = QApplication.primaryScreen().availableGeometry()
    config.window.width = geo.width() + 500
    config.window.height = geo.height() + 500
    config.window.x = geo.right() - 10
    config.window.y = geo.bottom() - 10

    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    with qtbot.waitExposed(widget):
        widget.show()

    assert widget.width() <= geo.width()
    assert widget.height() <= geo.height()
    assert widget.x() >= geo.left()
    assert widget.y() >= geo.top()
    assert widget.x() + widget.width() <= geo.right() + 1
    assert widget.y() + widget.height() <= geo.bottom() + 1


def test_the_minimum_width_and_height_hold_against_a_drag(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget.move(50, 50)

    _drag_corner(widget, -4000, -4000)

    assert widget.width() == WINDOW_MIN_WIDTH
    assert widget.height() == WINDOW_MIN_HEIGHT


def test_auto_fit_runs_on_a_fresh_config_and_stops_after_a_resize(qtbot):
    """The whole point of the conditional: re-fitting after a drag would undo
    it on the next refresh."""
    config = Config()
    assert (config.window.width, config.window.height) == (
        WINDOW_WIDTH,
        WINDOW_DEFAULT_HEIGHT,
    )
    widget = UsageWidget(config)
    qtbot.addWidget(widget)

    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget._do_refit_height()  # noqa: SLF001
    fitted = widget.height()
    widget.update_snapshot(_ok_snapshot("codex"), "Codex")
    widget._do_refit_height()  # noqa: SLF001
    assert widget.height() > fitted, "auto-fit did not grow with a second tile"

    widget.move(50, 50)
    _drag_corner(widget, 0, 90)
    chosen = widget.height()

    widget.update_snapshot(_ok_snapshot("copilot"), "Copilot")
    widget._do_refit_height()  # noqa: SLF001
    assert widget.height() == chosen, "a refresh resized a window the user had sized"


def test_moving_a_fresh_window_does_not_end_auto_fit(qtbot):
    """Only a resize writes a size back; a move must leave the first-run
    values in place or the first drag would silently pin the height."""
    config = Config()
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget.move(50, 50)

    middle = QPoint(widget.width() // 2, widget.height() // 2)
    _press(widget, middle)
    _move_to(widget, widget.mapToGlobal(middle) + QPoint(60, 40))
    _release(widget, middle)

    assert widget._user_sized is False  # noqa: SLF001
    assert (config.window.width, config.window.height) == (
        WINDOW_WIDTH,
        WINDOW_DEFAULT_HEIGHT,
    )
    assert (config.window.x, config.window.y) == (widget.x(), widget.y())


def test_the_geometry_survives_a_drag_the_window_manager_ended(qtbot):
    """No press, no release - the path every real desktop takes.

    ``startSystemMove``/``startSystemResize`` hand the pointer to the window
    manager and the release comes back to it, not to us; offscreen they both
    return False, which is why the suite only ever saw the pure-Qt fallback.
    ``hideEvent`` wrote the model without saving it and ``App.shutdown()``
    hides and quits, so the size and position were lost on every such drag.
    """
    config = Config()
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    with qtbot.waitExposed(widget):
        widget.show()
    widget._mark_user_sized()  # noqa: SLF001
    geo = QApplication.primaryScreen().availableGeometry()
    widget.setGeometry(
        geo.left() + 30,
        geo.top() + 40,
        min(520, geo.width() - 60),
        min(360, geo.height() - 80),
    )
    qtbot.wait(0)

    widget.hide()

    loaded = Config.load()
    assert (loaded.window.x, loaded.window.y) == (widget.x(), widget.y())
    assert (loaded.window.width, loaded.window.height) == (
        widget.width(),
        widget.height(),
    )


def test_a_window_manager_drag_is_written_once_when_its_events_stop(qtbot, monkeypatch):
    """One atomic write per gesture, and none during it.

    The only sign a WM-owned drag has ended is that its events stop, so the
    save is a single-shot debounce armed by move and resize while the gesture
    is live - not one write per event, which at 200 events a drag would be
    200 fsyncs.
    """
    config = Config()
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget._mark_user_sized()  # noqa: SLF001
    assert widget._geometry_commit.isSingleShot()  # noqa: SLF001
    assert widget._geometry_commit.interval() == NATIVE_GESTURE_COMMIT_MS  # noqa: SLF001
    # The same timer, wound down so the test does not sit out a second.
    widget._geometry_commit.setInterval(1)  # noqa: SLF001

    saves: list[int] = []
    monkeypatch.setattr(Config, "save", lambda self: saves.append(1))

    widget._native_gesture = True  # startSystemResize returned True  # noqa: SLF001
    before = widget.size()
    for step in range(20):
        widget.resize(360 + step, 240 + step)
        widget.resizeEvent(QResizeEvent(widget.size(), before))
    assert saves == [], "the drag itself wrote to the file"

    qtbot.waitUntil(lambda: bool(saves), timeout=3000)
    qtbot.wait(20)
    assert saves == [1], "one gesture, more than one write"
    assert (config.window.width, config.window.height) == (
        widget.width(),
        widget.height(),
    )
    assert widget._native_gesture is False  # noqa: SLF001


def test_a_hide_that_changed_nothing_writes_nothing(qtbot, monkeypatch):
    """The dirty check. Re-applying the always-on-top flag hides and shows the
    window, and the App hides it on quit, so the commit seam is reached far
    more often than the geometry changes."""
    config = Config()
    config.window.x, config.window.y = 60, 70
    config.window.width, config.window.height = 420, 280
    config.window.user_sized = True
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    with qtbot.waitExposed(widget):
        widget.show()
    qtbot.wait(0)

    saves: list[int] = []
    monkeypatch.setattr(Config, "save", lambda self: saves.append(1))
    widget.hide()
    widget.show()
    widget.hide()

    assert saves == []


def test_a_click_on_the_edge_does_not_end_auto_fit(qtbot):
    """`_mark_user_sized()` used to run on the press, before any movement was
    known, so one stray click within 8 px of an edge switched auto-fit off
    for good - and the release then wrote that size to disk, so it survived
    every restart."""
    config = Config()
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget.move(50, 50)
    widget._do_refit_height()  # noqa: SLF001
    fitted = widget.height()

    edge = QPoint(2, widget.height() // 2)
    _press(widget, edge)
    # And a move that travels nowhere: the pointer jitters inside one pixel
    # and the geometry the fallback computes is the geometry it started from.
    _move_to(widget, widget.mapToGlobal(edge))
    _release(widget, edge)

    assert widget._user_sized is False  # noqa: SLF001
    assert config.window.user_sized is False
    assert (config.window.width, config.window.height) == (
        WINDOW_WIDTH,
        WINDOW_DEFAULT_HEIGHT,
    )
    widget.update_snapshot(_ok_snapshot("codex"), "Codex")
    widget._do_refit_height()  # noqa: SLF001
    assert widget.height() > fitted, "a click in the band stopped auto-fit"

    _drag_corner(widget, 0, 20)

    assert widget._user_sized is True  # noqa: SLF001
    assert config.window.user_sized is True


def test_a_1_3_x_config_still_auto_fits(qtbot):
    """1.3.x saved its auto-fitted height on every release, hide and close, so
    "the size is not 340x220" meant "user-sized" for practically every
    installed config. Every existing user would have lost auto-fit on the
    first launch of 1.4.0 without touching an edge."""
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"window": {"width": 340, "height": 268}}', encoding="utf-8"
    )
    config = Config.load()

    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget._do_refit_height()  # noqa: SLF001
    fitted = widget.height()

    assert widget._user_sized is False  # noqa: SLF001
    widget.update_snapshot(_ok_snapshot("codex"), "Codex")
    widget._do_refit_height()  # noqa: SLF001
    assert widget.height() > fitted, "the upgraded config arrived user-sized"


def test_a_config_that_says_user_sized_keeps_its_size(qtbot):
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"window": {"width": 500, "height": 300, "user_sized": true}}',
        encoding="utf-8",
    )
    config = Config.load()

    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    qtbot.wait(0)
    qtbot.wait(0)

    assert widget._user_sized is True  # noqa: SLF001
    assert (widget.width(), widget.height()) == (500, 300)


def test_a_click_raises_settings_but_a_drag_does_not(qtbot):
    """The drag defect: activated_requested used to fire on every press, and
    the App answers it by activating the Settings window - which takes the
    focus, and with it the implicit mouse grab, away mid-drag."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget.move(50, 50)
    raised: list[int] = []
    widget.activated_requested.connect(lambda: raised.append(1))

    middle = QPoint(widget.width() // 2, widget.height() // 2)
    _press(widget, middle)
    _move_to(widget, widget.mapToGlobal(middle) + QPoint(80, 0))
    _release(widget, middle)
    assert raised == [], "a drag raised Settings"

    _press(widget, middle)
    _release(widget, middle)
    assert raised == [1], "a click did not raise Settings"


def test_the_vertical_bar_appears_with_twelve_tiles_and_not_with_one(qtbot):
    one = UsageWidget(Config())
    qtbot.addWidget(one)
    one.update_snapshot(_ok_snapshot("claude"), "Claude")
    with qtbot.waitExposed(one):
        one.show()
    one._do_refit_height()  # noqa: SLF001
    qtbot.wait(0)
    assert one._tile_scroll.verticalScrollBar().maximum() == 0  # noqa: SLF001

    many = UsageWidget(Config())
    qtbot.addWidget(many)
    for i in range(12):
        many.update_snapshot(_ok_snapshot(f"claude-{i}"), f"Claude {i}")
    with qtbot.waitExposed(many):
        many.show()
    many._do_refit_height()  # noqa: SLF001
    qtbot.wait(0)
    assert many._tile_scroll.verticalScrollBar().maximum() > 0  # noqa: SLF001


def test_the_horizontal_bar_appears_only_when_a_row_is_wider_than_the_window(qtbot):
    """A plain metric row's minimum is 196 px, well inside the 260 px window
    minimum, so ordinary content never needs one. An Azure row that carries a
    spend and an allowance in its reset column does: measured 304 px."""
    plain = UsageWidget(Config())
    qtbot.addWidget(plain)
    plain.update_snapshot(_ok_snapshot("claude"), "Claude")
    with qtbot.waitExposed(plain):
        plain.show()
    plain._mark_user_sized()  # noqa: SLF001
    plain.resize(WINDOW_MIN_WIDTH, 300)
    qtbot.wait(0)
    assert plain._tile_scroll.horizontalScrollBar().maximum() == 0  # noqa: SLF001

    wide = UsageWidget(Config())
    qtbot.addWidget(wide)
    fetched = datetime(2026, 4, 27, 12, 0)
    wide.update_snapshot(
        UsageSnapshot(
            provider="azure",
            status=SnapshotStatus.OK,
            metrics=[
                UsageMetric(
                    "Spend",
                    40.0,
                    fetched + timedelta(days=5),
                    reset_label="$1,234,567.89 of $9,999,999.00",
                )
            ],
            fetched_at=fetched,
        ),
        "Microsoft · Azure",
    )
    with qtbot.waitExposed(wide):
        wide.show()
    wide._mark_user_sized()  # noqa: SLF001
    wide.resize(WINDOW_MIN_WIDTH, 300)
    qtbot.wait(0)
    assert wide._tile_container.minimumSizeHint().width() > WINDOW_MIN_WIDTH  # noqa: SLF001
    assert wide._tile_scroll.horizontalScrollBar().maximum() > 0  # noqa: SLF001


def test_the_scroll_bar_is_painted_in_the_shared_colours(qtbot):
    """A 6 px handle on a track the colour of the panel read as a floating
    sliver. Measured here: a 10 px bar, a #1f2937 track against the #111827
    panel, a #4b5563 handle - and nothing at all when the content fits."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    for i in range(12):
        widget.update_snapshot(_ok_snapshot(f"claude-{i}"), f"Claude {i}")
    with qtbot.waitExposed(widget):
        widget.show()
    widget._do_refit_height()  # noqa: SLF001
    qtbot.wait(0)

    bar = widget._tile_scroll.verticalScrollBar()  # noqa: SLF001
    assert bar.maximum() > 0
    assert bar.width() == SCROLLBAR_WIDTH
    # Three lines of text per notch, and a page is the viewport.
    assert bar.singleStep() == 3 * widget.fontMetrics().height()
    assert bar.pageStep() == widget._tile_scroll.viewport().height()  # noqa: SLF001

    image = widget.grab().toImage()
    top_left = bar.mapTo(widget, bar.rect().topLeft())
    column = top_left.x() + bar.width() // 2
    assert image.pixelColor(column, top_left.y() + 12).name() == "#4b5563"
    assert (
        image.pixelColor(column, top_left.y() + bar.height() - 4).name() == "#1f2937"
    ), "the track is not distinguishable from the panel"
    assert image.pixelColor(top_left.x() - 6, top_left.y() + 12).name() == "#111827"

    fitted = UsageWidget(Config())
    qtbot.addWidget(fitted)
    fitted.update_snapshot(_ok_snapshot("claude"), "Claude")
    with qtbot.waitExposed(fitted):
        fitted.show()
    fitted._do_refit_height()  # noqa: SLF001
    qtbot.wait(0)
    fitted_image = fitted.grab().toImage()
    assert fitted_image.pixelColor(column, 60).name() == "#111827", (
        "a bar was painted for content that fits"
    )


def test_the_collapsed_strip_keeps_the_width_the_user_chose(qtbot):
    """And gives the height back on the way out.

    ``set_collapsed`` used to record the size after ``_collapsed`` had flipped
    to False and before the expanded geometry was back, so one click on the
    chevron and one back wrote the 58 px strip's height over the size the user
    had dragged to - in the config, i.e. on disk, i.e. for every later launch.
    """
    config = Config()
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    widget.update_snapshot(_ok_snapshot("claude"), "Claude")
    widget.move(50, 50)
    _drag_corner(widget, 140, 60)
    chosen = widget.size()

    widget.set_collapsed(True)
    qtbot.wait(0)

    assert widget.width() == chosen.width()
    assert widget.height() < chosen.height(), "the strip is not a strip"
    # The guard in _remember_size: a collapsed window's height is the app's
    # answer, not the user's, so it is never what gets written back.
    assert (config.window.width, config.window.height) == (
        chosen.width(),
        chosen.height(),
    ), "the collapsed height reached the config"

    # And a click on the strip, which runs the same save path on release:
    # the guard in _remember_size is the only thing between the strip's own
    # height and the file.
    strip_middle = QPoint(widget.width() // 2, widget.height() // 2)
    _press(widget, strip_middle)
    _release(widget, strip_middle)
    assert (config.window.width, config.window.height) == (
        chosen.width(),
        chosen.height(),
    ), "a click on the collapsed strip wrote its height back"

    widget.set_collapsed(False)
    qtbot.wait(0)

    assert widget.size() == chosen, "expanding did not give the height back"
    assert (config.window.width, config.window.height) == (
        chosen.width(),
        chosen.height(),
    )


def test_a_collapse_and_an_expand_survive_a_relaunch(qtbot):
    """The same round trip, through the file and a second construction.

    Twelve tiles, because that is the case where the strip wraps to three
    rows and the height it would have written back (106) is far enough from
    the user's to be unmistakable.
    """
    config = Config()
    widget = UsageWidget(config)
    qtbot.addWidget(widget)
    for i in range(12):
        widget.update_snapshot(_ok_snapshot(f"claude-{i}"), f"Claude {i}")
    widget.move(50, 50)
    _drag_corner(widget, 140, 60)
    chosen = widget.size()

    widget.set_collapsed(True)
    qtbot.wait(0)
    widget.set_collapsed(False)
    qtbot.wait(0)
    assert widget.size() == chosen

    reloaded = Config.load()
    assert (reloaded.window.width, reloaded.window.height) == (
        chosen.width(),
        chosen.height(),
    ), "the next launch would open at the strip's height"

    relaunched = UsageWidget(reloaded)
    qtbot.addWidget(relaunched)
    for i in range(12):
        relaunched.update_snapshot(_ok_snapshot(f"claude-{i}"), f"Claude {i}")
    qtbot.wait(0)
    qtbot.wait(0)
    assert relaunched.size() == chosen


# --- An error tile with no rows -------------------------------------------


def _error_snapshot(provider, error, metrics=(), status=SnapshotStatus.ERROR):
    return UsageSnapshot(
        provider=provider,
        status=status,
        metrics=list(metrics),
        error=error,
        fetched_at=datetime(2026, 4, 27, 12, 0),
    )


def test_an_error_tile_with_no_rows_says_what_happened(qtbot):
    """The reported symptom - "the title and nothing else".

    The status line was there all along: 26 px of "error" in the far corner of
    a 340 px header, on a tile 22 px tall. It is now a full-width line under
    the header carrying the message itself, and the corner tag names the
    failure mode.
    """
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    message = "Cost Management is rate limiting this tenant; next attempt at 12:16."
    widget.update_snapshot(
        _error_snapshot("azure", message), "Microsoft · Azure"
    )
    with qtbot.waitExposed(widget):
        widget.show()
    widget._do_refit_height()  # noqa: SLF001
    qtbot.wait(0)
    tile = widget._tiles["azure"]  # noqa: SLF001

    assert tile.detail.isVisible()
    assert tile.detail.width() > tile.header.width()
    assert tile.detail.toolTip().startswith(message)
    assert message.startswith(tile.detail.text().rstrip("…")), (
        "the line does not show the beginning of the message"
    )
    assert "rate limited" in tile.status.text()
    assert tile.height() > tile.header.sizeHint().height() + tile.detail.height() - 1


def test_the_error_line_is_the_clickable_details_affordance(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_error_snapshot("azure", "boom"), "Microsoft · Azure")
    tile = widget._tiles["azure"]  # noqa: SLF001

    with qtbot.waitSignal(widget.details_requested) as signal:
        _press(tile.detail, QPoint(4, 4))
        _release(tile.detail, QPoint(4, 4))
    assert signal.args == ["azure"]


def test_the_error_line_raises_details_on_a_click_not_a_press(qtbot):
    """The line used to emit on the press, so the details dialog came up with
    the mouse still down - which is exactly the focus-and-grab steal this
    release removed from the panel, re-introduced over most of the width of
    every error tile that has no rows."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_error_snapshot("azure", "boom"), "Microsoft · Azure")
    detail = widget._tiles["azure"].detail  # noqa: SLF001
    seen: list[str] = []
    widget.details_requested.connect(seen.append)

    start = QPoint(4, 4)
    _press(detail, start)
    assert seen == [], "the dialog opened while the button was still down"

    far = QPoint(start.x() + QApplication.startDragDistance() + 40, start.y())
    _move_to(detail, detail.mapToGlobal(far))
    _release(detail, far)
    assert seen == [], "a drag across the line opened the dialog"

    _press(detail, start)
    _release(detail, start)
    assert seen == ["azure"], "a click did not open the dialog"


def test_a_long_error_is_elided_not_clipped(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    message = "x" * 60
    widget.update_snapshot(_error_snapshot("azure", message), "Microsoft · Azure")
    with qtbot.waitExposed(widget):
        widget.show()
    qtbot.wait(0)
    detail = widget._tiles["azure"].detail  # noqa: SLF001

    drawn = detail.text()
    assert drawn.endswith("…")
    assert detail.fontMetrics().horizontalAdvance(drawn) <= detail.width()
    assert detail.toolTip().startswith(message), "the full text is unreachable"


def test_the_detail_line_shows_markup_literally(qtbot):
    """The label had no text format, so it defaulted to AutoText and a
    provider string that looks like markup was *interpreted*: measured, a
    hint of 67 px against the 287 px the same string needs as plain text,
    i.e. `<span style="color:#111827">` would have painted half an error
    message in the panel's own background colour. It also took the elide
    guarantee with it - `_elide` measures the raw string and hands the result
    to the renderer, and a 38 kB `<table>` came out 44 px tall."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    markup = "<b>bold</b>"
    widget.update_snapshot(_error_snapshot("azure", markup), "Microsoft · Azure")
    with qtbot.waitExposed(widget):
        widget.show()
    qtbot.wait(0)
    detail = widget._tiles["azure"].detail  # noqa: SLF001

    assert detail.textFormat() == Qt.TextFormat.PlainText
    assert detail.text() == markup, "the markup was not drawn as itself"
    # The measurement rule: a plain-text label needs at least what its own
    # font metrics say the string costs. A rich-text one needs a quarter of it.
    assert detail.sizeHint().width() >= detail.fontMetrics().horizontalAdvance(
        markup
    )


def test_an_error_tooltip_is_clipped_and_shows_markup_literally(qtbot):
    """A tooltip has no text-format setter - Qt renders anything markup-shaped
    as rich text - and the string it carries is a provider's, i.e. unbounded:
    a 10 240-character error made a 10 260-character tooltip. Both copies of
    it, the detail line's and the status label's, are clipped and escaped."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(
        _error_snapshot("azure", "<b>x</b>"), "Microsoft · Azure"
    )
    tile = widget._tiles["azure"]  # noqa: SLF001

    for tooltip in (tile.detail.toolTip(), tile.status.toolTip()):
        assert "&lt;b&gt;" in tooltip
        assert "<b>" not in tooltip
    assert tile.detail.text() == "<b>x</b>", "the label itself is the literal"

    widget.update_snapshot(
        _error_snapshot("azure", "x" * 10240), "Microsoft · Azure"
    )
    assert len(tile.detail.toolTip()) <= 320
    assert len(tile.status.toolTip()) <= 400
    assert tile.detail.toolTip().startswith("x" * 100)
    assert "…" in tile.detail.toolTip()


def test_an_error_tile_that_still_has_rows_does_not_repeat_itself(qtbot):
    """With numbers on the tile the corner tag reads "error · stale" beside
    them and a second red line would be noise."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(
        _error_snapshot(
            "azure",
            "boom",
            metrics=[UsageMetric("Spend", 40.0, datetime(2026, 4, 28, 12, 0))],
        ),
        "Microsoft · Azure",
    )
    tile = widget._tiles["azure"]  # noqa: SLF001

    assert tile.detail.isVisibleTo(tile) is False
    assert "stale" in tile.status.text()


def test_a_provider_with_no_sign_in_button_says_why_it_is_unauthenticated(qtbot):
    """Azure, Copilot and OpenRouter have no Sign in button, so
    "not signed in" in the corner was the whole message."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(
        _error_snapshot(
            "azure",
            "The client secret has expired.",
            status=SnapshotStatus.AUTH_REQUIRED,
        ),
        "Microsoft · Azure",
    )
    tile = widget._tiles["azure"]  # noqa: SLF001
    assert tile.detail.isVisibleTo(tile)
    assert tile.detail.toolTip() == "The client secret has expired."
    assert tile.detail.cursor().shape() == Qt.CursorShape.ArrowCursor

    widget.update_snapshot(
        _error_snapshot(
            "claude", "Session expired.", status=SnapshotStatus.AUTH_REQUIRED
        ),
        "Claude",
    )
    claude = widget._tiles["claude"]  # noqa: SLF001
    assert claude.action_btn.isVisibleTo(claude)
    assert claude.detail.isVisibleTo(claude) is False, (
        "a tile with a Sign in button does not also need a sentence"
    )


def test_a_recovered_tile_drops_the_error_line(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_error_snapshot("azure", "boom"), "Microsoft · Azure")
    tile = widget._tiles["azure"]  # noqa: SLF001
    assert tile.detail.isVisibleTo(tile)

    widget.update_snapshot(_ok_snapshot("azure"), "Microsoft · Azure")
    assert tile.detail.isVisibleTo(tile) is False
    assert tile.detail.text() == ""


@pytest.mark.parametrize(
    "error,expected",
    [
        ("Cost Management is rate limiting this tenant", "error · rate limited"),
        ("the request was throttled", "error · rate limited"),
        ("something else entirely", "error"),
    ],
    ids=["rate-limiting", "throttled", "unmatched"],
)
def test_a_rate_limit_is_named_in_the_corner_tag(error, expected):
    from aigauge.widget import _short_error_reason

    assert _short_error_reason(error) == expected


# --- The note is reachable by hovering any part of a row --------------------


def _azure_component_snapshot(note="CA$1,234.56"):
    return UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.OK,
        metrics=[
            UsageMetric(label="Spend", percent_used=42.0, note="CA$4,000.00"),
            UsageMetric(
                label="Azure App Service",
                percent_used=13.0,
                note=note,
                tag="meter_breakdown",
            ),
        ],
        fetched_at=datetime(2026, 4, 27, 12, 0),
    )


def _component_row(qtbot):
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_azure_component_snapshot(), "Microsoft · Azure")
    tile = widget._tiles["azure"]  # noqa: SLF001
    tile.set_expanded(True, emit=False)
    with qtbot.waitExposed(widget):
        widget.show()
    widget._do_refit_height()  # noqa: SLF001
    qtbot.wait(0)
    assert [row.label.text() for row in tile._rows][-1] == "Azure App Service"  # noqa: SLF001
    return widget, tile._rows[-1]  # noqa: SLF001


def test_every_part_of_a_component_row_carries_the_amount(qtbot):
    """A Windows desktop showed no tooltip on an Azure component row. The row
    has always carried the note and Qt propagates an unanswered ToolTip event
    up from a child, but that rests on every child answering with nothing -
    so each of them now carries it outright."""
    _widget, row = _component_row(qtbot)

    assert row.toolTip() == "CA$1,234.56"
    for name in ("label", "bar", "pct"):
        assert getattr(row, name).toolTip() == "CA$1,234.56", name


@pytest.mark.parametrize(
    "target", ["row", "label", "bar", "pct"], ids=["row", "label", "bar", "pct"]
)
def test_a_tooltip_event_anywhere_on_the_row_shows_the_amount(qtbot, target):
    from PyQt6.QtGui import QHelpEvent
    from PyQt6.QtWidgets import QToolTip

    _widget, row = _component_row(qtbot)
    widget = row if target == "row" else getattr(row, target)

    QToolTip.hideText()
    point = QPoint(2, 2)
    event = QHelpEvent(QEvent.Type.ToolTip, point, widget.mapToGlobal(point))
    QApplication.sendEvent(widget, event)
    qtbot.wait(0)

    assert event.isAccepted()
    assert QToolTip.text() == "CA$1,234.56"
    QToolTip.hideText()


def test_a_row_with_no_note_leaves_no_stale_tooltip_behind(qtbot):
    """The children are written on every set_metric, so a row reused for a
    metric that has no note must not keep the previous one's."""
    widget = UsageWidget(Config())
    qtbot.addWidget(widget)
    widget.update_snapshot(_azure_component_snapshot(), "Microsoft · Azure")
    tile = widget._tiles["azure"]  # noqa: SLF001
    tile.set_expanded(True, emit=False)
    row = tile._rows[-1]  # noqa: SLF001
    assert row.label.toolTip() == "CA$1,234.56"

    row.set_metric("Azure App Service", 13.0, None, None, None, None)

    assert row.toolTip() == ""
    for name in ("label", "bar", "pct"):
        assert getattr(row, name).toolTip() == "", name
