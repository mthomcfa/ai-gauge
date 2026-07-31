import pytest

from aigauge.config import BrowserAccount, ColorThresholds, Config
from aigauge.gauge import (
    band_for_percent,
    color_for_percent,
    highest_indicator,
    provider_indicator,
    thresholds_for_provider,
)
from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot


def _snapshot(provider: str, *percents: float) -> UsageSnapshot:
    return UsageSnapshot(
        provider=provider,
        status=SnapshotStatus.OK,
        metrics=[UsageMetric(label=f"m{i}", percent_used=p) for i, p in enumerate(percents)],
    )


@pytest.mark.parametrize(
    "percent,expected",
    [
        (None, None),
        (0.0, "green"),
        (59.9, "green"),  # original boundary: 59.9 is still green
        (60.0, "yellow"),
        (79.9, "yellow"),
        (80.0, "orange"),
        (94.9, "orange"),
        (95.0, "red"),
        (110.0, "red"),  # Copilot overage stays red
    ],
)
def test_default_bands_match_original_fixed_behavior(percent, expected):
    assert band_for_percent(percent) == expected


def test_custom_cutoffs_are_honored():
    colors = ColorThresholds(green_max=10, yellow_max=20, orange_max=30)
    assert band_for_percent(5, colors) == "green"
    assert band_for_percent(15, colors) == "yellow"
    assert band_for_percent(25, colors) == "orange"
    assert band_for_percent(50, colors) == "red"


def test_color_for_percent_uses_configured_colors_and_neutral():
    colors = ColorThresholds(green_color="#001122")
    assert color_for_percent(10, colors) == "#001122"
    assert color_for_percent(None, colors) == "#6b7280"


def test_thresholds_resolve_per_account_and_provider():
    config = Config()
    config.browser_accounts.append(
        BrowserAccount(
            id="claude-work",
            kind="claude",
            colors=ColorThresholds(green_max=5),
        )
    )
    config.copilot.colors = ColorThresholds(green_max=7)

    assert thresholds_for_provider(config, "claude-work").green_max == 5
    assert thresholds_for_provider(config, "copilot").green_max == 7
    # Unknown ids and a missing config both fall back to defaults.
    assert thresholds_for_provider(config, "nope").green_max == 59
    assert thresholds_for_provider(None, "claude").green_max == 59


def test_provider_indicator_uses_worst_metric_and_ignores_non_ok():
    config = Config()
    indicator = provider_indicator(config, "claude", _snapshot("claude", 10.0, 96.0))
    assert indicator is not None
    assert indicator.percent == 96.0
    assert indicator.band == "red"

    errored = UsageSnapshot(provider="claude", status=SnapshotStatus.ERROR)
    assert provider_indicator(config, "claude", errored) is None
    assert provider_indicator(config, "claude", None) is None


def test_highest_indicator_prefers_band_then_percent():
    config = Config()
    # codex has the higher raw percent, but claude's custom cutoffs put it in a
    # worse band - band severity must win.
    config.browser_accounts[0].colors = ColorThresholds(
        green_max=1, yellow_max=2, orange_max=3
    )
    snapshots = {
        "claude": _snapshot("claude", 50.0),
        "codex": _snapshot("codex", 70.0),
    }
    best = highest_indicator(config, snapshots, ("claude", "codex"))
    assert best is not None
    assert best.provider == "claude"
    assert best.band == "red"


def test_highest_indicator_returns_none_without_usable_snapshots():
    config = Config()
    assert highest_indicator(config, {}, ("claude", "codex")) is None
