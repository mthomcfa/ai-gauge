"""Renderer tests for the macOS menu-bar pixmap.

Runs on every platform because Qt rendering is portable. Validates that the
pixmap geometry is sane and that the dot color matches the threshold band
for the supplied percent.
"""
from __future__ import annotations

from PyQt6.QtGui import QColor, QPixmap

from aigauge.menubar import (
    DOT_DIAMETER,
    OK_COLORS,
    NEUTRAL_COLOR,
    PIXMAP_HEIGHT,
    PIXMAP_WIDTH,
    SIDE_PADDING,
    SETUP_COLOR,
    measure_pixmap_width,
    render_menubar_pixmap,
    status_items,
)
from aigauge.models import (
    SnapshotStatus,
    UsageMetric,
    UsageSnapshot,
)


def _ok_snap(provider: str, percent: float) -> UsageSnapshot:
    return UsageSnapshot(
        provider=provider,
        status=SnapshotStatus.OK,
        metrics=[UsageMetric(label="usage", percent_used=percent)],
    )


def _color_at(pix: QPixmap, x: int, y: int) -> QColor:
    # The pixmap may be rendered at 2x device-pixel-ratio. toImage() returns
    # the underlying buffer at full resolution, so multiply the logical
    # coordinates by DPR before sampling.
    image = pix.toImage()
    dpr = pix.devicePixelRatio()
    return QColor(image.pixel(int(x * dpr), int(y * dpr)))


def test_empty_renders_a_neutral_dot(qtbot):  # qtbot ensures QApplication exists
    pix = render_menubar_pixmap({}, ())
    assert pix.height() / pix.devicePixelRatio() == PIXMAP_HEIGHT
    # A single neutral dot near the left edge.
    color = _color_at(pix, SIDE_PADDING + DOT_DIAMETER // 2, PIXMAP_HEIGHT // 2)
    assert color.name().lower() == NEUTRAL_COLOR.lower()


def test_provider_dot_color_matches_threshold_band(qtbot):
    # The dot now shares the configurable gauge bands with the expanded bars
    # and compact chips. This also unified the cutoffs: the dot previously had
    # its own fixed 75/90 thresholds, so 80% was amber and 90% was red; it now
    # follows 60/80/95 like everything else.
    from aigauge.config import ColorThresholds

    defaults = ColorThresholds()
    cases = [
        (10.0, defaults.green_color),
        (65.0, defaults.yellow_color),
        (80.0, defaults.orange_color),
        (95.0, defaults.red_color),
    ]
    for percent, expected in cases:
        pix = render_menubar_pixmap({"claude": _ok_snap("claude", percent)}, ("claude",))
        color = _color_at(pix, SIDE_PADDING + DOT_DIAMETER // 2, PIXMAP_HEIGHT // 2)
        assert color.name().lower() == expected.lower(), (
            f"percent={percent} expected {expected} got {color.name()}"
        )


def test_provider_dot_honors_configured_colors(qtbot):
    from aigauge.config import ColorThresholds, Config

    config = Config()
    config.browser_accounts[0].colors = ColorThresholds(
        green_max=5, green_color="#010203"
    )
    pix = render_menubar_pixmap(
        {"claude": _ok_snap("claude", 1.0)}, ("claude",), config=config
    )
    color = _color_at(pix, SIDE_PADDING + DOT_DIAMETER // 2, PIXMAP_HEIGHT // 2)
    assert color.name().lower() == "#010203"


def test_unknown_provider_renders_neutral(qtbot):
    pix = render_menubar_pixmap({}, ("claude",))
    color = _color_at(pix, SIDE_PADDING + DOT_DIAMETER // 2, PIXMAP_HEIGHT // 2)
    assert color.name().lower() == NEUTRAL_COLOR.lower()


def test_width_grows_with_provider_count(qtbot):
    snaps = {
        "claude": _ok_snap("claude", 40),
        "codex": _ok_snap("codex", 50),
        "copilot": _ok_snap("copilot", 60),
    }
    one = measure_pixmap_width(snaps, ("claude",))
    three = measure_pixmap_width(snaps, ("claude", "codex", "copilot"))
    assert one == PIXMAP_WIDTH
    assert three == PIXMAP_WIDTH


def test_pixmap_uses_device_pixel_ratio_for_retina(qtbot):
    pix = render_menubar_pixmap({"claude": _ok_snap("claude", 40)}, ("claude",))
    assert pix.devicePixelRatio() == 2.0
    # Logical height is fixed; raw buffer is height * dpr.
    assert pix.height() == PIXMAP_HEIGHT * 2
    assert pix.width() == PIXMAP_WIDTH * 2


def test_auth_required_snapshot_shows_setup_state(qtbot):
    snap = UsageSnapshot(provider="claude", status=SnapshotStatus.AUTH_REQUIRED)
    pix = render_menubar_pixmap({"claude": snap}, ("claude",))
    color = _color_at(pix, SIDE_PADDING + DOT_DIAMETER // 2, PIXMAP_HEIGHT // 2)
    assert color.name().lower() == SETUP_COLOR.lower()
    assert measure_pixmap_width({"claude": snap}, ("claude",)) == PIXMAP_WIDTH


def test_all_missing_snapshots_render_as_loading_summary(qtbot):
    one = measure_pixmap_width({}, ("claude",))
    three = measure_pixmap_width({}, ("claude", "codex", "copilot"))
    assert one == three
    pix = render_menubar_pixmap({}, ("claude", "codex", "copilot"))
    color = _color_at(pix, SIDE_PADDING + DOT_DIAMETER // 2, PIXMAP_HEIGHT // 2)
    assert color.name().lower() == NEUTRAL_COLOR.lower()


def test_status_items_use_compact_provider_labels():
    items = status_items(
        {
            "claude": UsageSnapshot(
                provider="claude",
                status=SnapshotStatus.AUTH_REQUIRED,
            ),
            "codex": _ok_snap("codex", 57),
        },
        ("claude", "codex", "copilot"),
    )

    assert items == [
        ("Cl", "!", SETUP_COLOR),
        ("Cx", "57%", OK_COLORS["low"]),
        ("Cp", "...", NEUTRAL_COLOR),
    ]


def test_native_mac_status_item_forwards_config_to_status_items(monkeypatch):
    """The macOS status item is the primary UI on Mac, not a fallback.

    It previously called status_items() without config, so configured gauge
    colours silently never reached the menu bar - and status_items' `config`
    default of None meant no TypeError to catch it.
    """
    from aigauge import macos_status_item
    from aigauge.config import Config

    captured = {}

    def fake_status_items(snapshots, providers, config=None):
        captured["config"] = config
        return []

    monkeypatch.setattr(macos_status_item, "status_items", fake_status_items)

    class _Button:
        def setAttributedTitle_(self, _title):
            pass

    class _Item:
        def button(self):
            return _Button()

    item = macos_status_item.NativeMacStatusItem.__new__(
        macos_status_item.NativeMacStatusItem
    )
    item._status_item = _Item()
    monkeypatch.setattr(macos_status_item, "_attributed_title", lambda items: None)

    config = Config()
    item.update({}, ("claude",), config)

    assert captured["config"] is config
