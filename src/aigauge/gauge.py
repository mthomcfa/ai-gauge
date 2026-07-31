"""Severity-band resolution for usage gauges.

Single source of truth for "what colour is this percentage" across the
expanded bars, the compact summary chips, and the tray / menu-bar indicator.
Bands are per-account so two accounts can use different cutoffs.

Ported from upstream v0.7.0. The colours themselves are validated in
``config.ColorThresholds`` before they reach here, because they are
interpolated into Qt stylesheets.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import ColorThresholds, Config, browser_account
from .models import SnapshotStatus, UsageSnapshot

GaugeBand = Literal["green", "yellow", "orange", "red"]

NEUTRAL_COLOR = "#6b7280"
_BAND_RANK: dict[GaugeBand, int] = {
    "green": 0,
    "yellow": 1,
    "orange": 2,
    "red": 3,
}


def band_for_percent(
    percent: float | None,
    colors: ColorThresholds | None = None,
) -> GaugeBand | None:
    """Return the configured band for a usage percentage.

    Cutoffs are presented as inclusive integer ranges in Settings. Comparing
    against ``max + 1`` preserves the original exact boundaries for fractional
    readings: 59.9% remains green while 60.0% starts yellow.
    """
    if percent is None:
        return None
    colors = colors or ColorThresholds()
    if percent < colors.green_max + 1:
        return "green"
    if percent < colors.yellow_max + 1:
        return "yellow"
    if percent < colors.orange_max + 1:
        return "orange"
    return "red"


def color_for_band(band: GaugeBand, colors: ColorThresholds) -> str:
    return {
        "green": colors.green_color,
        "yellow": colors.yellow_color,
        "orange": colors.orange_color,
        "red": colors.red_color,
    }[band]


def color_for_percent(
    percent: float | None,
    colors: ColorThresholds | None = None,
    *,
    neutral_color: str = NEUTRAL_COLOR,
) -> str:
    colors = colors or ColorThresholds()
    band = band_for_percent(percent, colors)
    return neutral_color if band is None else color_for_band(band, colors)


def thresholds_for_provider(
    config: Config | None,
    provider: str,
) -> ColorThresholds:
    """Resolve the band configuration for a provider/account id.

    Falls back to defaults for unknown ids so a caller can never end up
    without a usable band definition.

    Returns a *copy*: callers cache the result and compare by value to decide
    whether to repaint, so handing back the live config object would make an
    in-place edit invisible to that check.
    """
    if config is None:
        return ColorThresholds()
    account = browser_account(config, provider)
    if account is not None:
        return account.colors.model_copy(deep=True)
    if provider == "copilot":
        return config.copilot.colors.model_copy(deep=True)
    if provider == "openrouter":
        return config.openrouter.colors.model_copy(deep=True)
    if provider == "opencode_go":
        return config.opencode_go.colors.model_copy(deep=True)
    return ColorThresholds()


def provider_max_percent(snapshot: UsageSnapshot | None) -> float | None:
    if snapshot is None or snapshot.status != SnapshotStatus.OK:
        return None
    values = [
        metric.percent_used
        for metric in snapshot.metrics
        if metric.percent_used is not None
    ]
    return max(values) if values else None


@dataclass(frozen=True)
class GaugeIndicator:
    provider: str
    percent: float
    band: GaugeBand
    color: str

    @property
    def rank(self) -> int:
        return _BAND_RANK[self.band]


def provider_indicator(
    config: Config | None,
    provider: str,
    snapshot: UsageSnapshot | None,
) -> GaugeIndicator | None:
    percent = provider_max_percent(snapshot)
    if percent is None:
        return None
    colors = thresholds_for_provider(config, provider)
    band = band_for_percent(percent, colors)
    if band is None:  # unreachable: percent is not None here
        return None
    return GaugeIndicator(
        provider=provider,
        percent=percent,
        band=band,
        color=color_for_band(band, colors),
    )


def highest_indicator(
    config: Config,
    snapshots: dict[str, UsageSnapshot],
    enabled_providers: tuple[str, ...],
) -> GaugeIndicator | None:
    """Return the most severe enabled provider for a single tray indicator.

    Severity band takes priority over raw percentage because accounts may use
    different cutoffs. Equal bands prefer the higher percentage, while provider
    order keeps exact ties deterministic.
    """
    best: GaugeIndicator | None = None
    for provider in enabled_providers:
        candidate = provider_indicator(config, provider, snapshots.get(provider))
        if candidate is None:
            continue
        if best is None or (candidate.rank, candidate.percent) > (
            best.rank,
            best.percent,
        ):
            best = candidate
    return best
