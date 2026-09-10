"""Azure month-to-date spend, decomposed by service, with a Foundry roll-up.

One tile: spend so far this allowance period against a monthly allowance,
broken into component rows. Microsoft Foundry (formerly Azure AI Foundry) is
one of those rows rather than a tile of its own, because Foundry bills per
token to the same Azure subscription - its spend is a strict subset of the
Azure total. Giving it a second gauge would double-count it; giving it a row
makes the double-count structurally impossible.

Three things about this data are easy to get wrong, so they are stated once
here and repeated where they bite:

* **Cost Management is gross of credits.** It reports consumption, never a
  credit balance. There is no ChargeType row for credits to subtract. The
  gauge therefore measures spend against an allowance *you* state, and it is
  not a live read of a Visual Studio or MCA credit balance.
* **The data lags.** 8-24 h for EA/MCA, up to 72 h for pay-as-you-go, and it
  refreshes about six times a day. Every snapshot carries the latest usage
  date it actually saw, so the tile can say how old the number is instead of
  implying it is live.
* **Rate limits are shared tenant-wide.** The app's adaptive refresh can fire
  every five minutes, and its error fast-retry every minute. Cost Management
  quotas are per *tenant*, not per app, so an unthrottled tile here would eat
  the budget of whatever else the owner runs against the same tenant. This
  module self-throttles to one fetch per hour and serves the cached snapshot
  in between. Microsoft's own guidance is no more than once per day.

Grouping is capped at two dimensions by the Query API, and the two we spend
them on are ResourceId and ServiceName. Classification is by *resource id*,
never by ResourceType or a ServiceName string: Azure OpenAI, Speech, Vision,
Language and Foundry all share ``Microsoft.CognitiveServices/accounts``, and
the display names shifted when "Azure AI Services" became "Foundry Tools".
Group on names, classify on ids.
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

import requests

from ..config import Config, get_azure_client_secret
from ..models import SnapshotStatus, UsageMetric, UsageSnapshot
from ._azure_auth import AzureAuthError, get_token
from .base import Provider

log = logging.getLogger("aigauge.providers.azure")

MANAGEMENT_HOST = "https://management.azure.com"
COST_MANAGEMENT_API_VERSION = "2025-03-01"
CONSUMPTION_BUDGETS_API_VERSION = "2024-08-01"
COGNITIVE_SERVICES_API_VERSION = "2024-10-01"
SUBSCRIPTIONS_API_VERSION = "2022-12-01"

# Cost Management gives a ClientType its own rate-limit bucket. Without one we
# would share a single bucket with every other caller in the world that also
# omits it.
CLIENT_TYPE = "ai-gauge"
REQUEST_TIMEOUT = 15

# The floor between two live fetches. See the module docstring: the refresh
# scheduler above us is far more eager than this API tolerates, and the quota
# is shared with everything else in the tenant.
MIN_FETCH_INTERVAL = timedelta(hours=1)
# Ceiling for the internal error backoff, so a subscription that is permanently
# misconfigured stops being retried hourly forever.
MAX_ERROR_BACKOFF = timedelta(hours=6)
# Discovery calls (offer type, Foundry resource list) describe things that
# change on the order of months, not minutes.
DISCOVERY_TTL = timedelta(hours=24)

# Same value as OpenRouter's MODEL_BREAKDOWN_TAG, and it must stay that way:
# history.py, gauge.provider_max_percent and the menu-bar dot all filter on the
# literal string, so a second tag value would silently opt these rows back into
# driving the tray colour. Asserted in tests/test_azure.py.
BREAKDOWN_TAG = "model_breakdown"
MAX_BREAKDOWN_ROWS = 6
LABEL_MAX_LEN = 20

FOUNDRY_BUCKET = "Foundry"
MARKETPLACE_BUCKET = "Marketplace models"
# Foundry resources are Cognitive Services accounts of this kind. Projects are
# child resources (accounts/projects) and bill to the parent account, so the
# parent's id is the one that appears in cost rows.
FOUNDRY_ACCOUNT_KIND = "aiservices"

# Cost Management names the cost metric differently across billing systems:
# "Cost" on Microsoft Customer Agreement, "PreTaxCost" on EA and pay-as-you-go
# (every example in the REST spec uses PreTaxCost). Asking for the wrong one is
# a 400, so we try the first and fall back once, then remember which worked.
COST_METRIC_PRIMARY = "Cost"
COST_METRIC_FALLBACK = "PreTaxCost"
_COST_COLUMN_CANDIDATES = ("Cost", "PreTaxCost", "CostUSD", "PreTaxCostUSD", "totalCost")
# The Query API pages at ~1000 rows, and Daily granularity grouped on ResourceId
# *and* ServiceName reaches that at roughly 33 resources over a month. Following
# nextLink is therefore the normal case on a busy subscription, not an edge one.
# The cap is a ceiling on one refresh's request count, not an expected limit.
MAX_QUERY_PAGES = 20

_PERMISSION_HINT = (
    "Grant the app registration Cost Management Reader (for cost queries, "
    "forecasts, and budgets) and Reader (to list resources and read the "
    "subscription offer) on the subscription."
)


# --------------------------------------------------------------------------
# Period arithmetic
# --------------------------------------------------------------------------


def _add_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _sub_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def period_bounds(today: date, reset_day: int) -> tuple[date, date]:
    """Return (period_start, next_reset) for the allowance period holding today.

    ``next_reset`` is exclusive - it is the first day of the *next* period.

    The reset day is a setting rather than the calendar first of the month
    because credit-backed subscriptions reset on an anniversary Microsoft does
    not publish, and querying MonthToDate for one of those measures the wrong
    window. ``reset_day`` is capped at 28 upstream in config.py so this
    arithmetic never has to invent a 31st of February.
    """
    reset_day = max(1, min(28, reset_day))
    if today.day >= reset_day:
        start = date(today.year, today.month, reset_day)
        end_year, end_month = _add_month(today.year, today.month)
    else:
        start_year, start_month = _sub_month(today.year, today.month)
        start = date(start_year, start_month, reset_day)
        end_year, end_month = today.year, today.month
    return start, date(end_year, end_month, reset_day)


def _local_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


def _column_index(columns: list[Any]) -> dict[str, int]:
    """Map lower-cased column name -> position.

    The Query API returns ``properties.columns`` as [{name, type}] and
    ``properties.rows`` as positional arrays, so every read is by this map.
    Column order is not contractual and has changed between api-versions.
    """
    index: dict[str, int] = {}
    for position, column in enumerate(columns or []):
        if isinstance(column, dict):
            name = column.get("name")
            if isinstance(name, str) and name:
                index.setdefault(name.strip().lower(), position)
    return index


def _cost_column(columns: list[Any], index: dict[str, int]) -> int | None:
    """Locate the cost column without trusting a single hard-coded name."""
    for candidate in _COST_COLUMN_CANDIDATES:
        position = index.get(candidate.lower())
        if position is not None:
            return position
    # Last resort: the first numeric column that is not the usage date.
    for position, column in enumerate(columns or []):
        if not isinstance(column, dict):
            continue
        name = str(column.get("name") or "").strip().lower()
        if name in ("usagedate", "billingmonth"):
            continue
        if str(column.get("type") or "").strip().lower() == "number":
            return position
    return None


def _to_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    # json.loads accepts Infinity/NaN, and these numbers are summed and divided.
    return number if number == number and abs(number) != float("inf") else 0.0


def _parse_usage_date(value: Any) -> date | None:
    """Accept both shapes the API uses for UsageDate.

    Daily granularity returns an integer ``20260908`` on the legacy shape and
    an ISO timestamp on others; neither is documented as the only one.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = str(int(value))
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass
class QueryRows:
    """The parts of a Query API response this provider actually uses."""

    total: float = 0.0
    currency: str = ""
    by_resource: dict[str, float] = field(default_factory=dict)
    by_service: dict[str, float] = field(default_factory=dict)
    # (resource_id_lower, service_name) -> cost, so a bucket can be chosen per
    # row without re-querying.
    rows: list[tuple[str, str, float]] = field(default_factory=list)
    latest_usage_date: date | None = None
    row_count: int = 0
    # Set when a page of results was left unread - a refused nextLink or the
    # page cap. The total is then a subtotal, and must not be shown as a gauge.
    truncated: bool = False


def parse_query_response(payload: Any) -> QueryRows:
    """Turn a Query API payload into totals keyed by resource and service."""
    out = QueryRows()
    properties = payload.get("properties") if isinstance(payload, dict) else None
    if not isinstance(properties, dict):
        return out
    columns = properties.get("columns") or []
    rows = properties.get("rows") or []
    if not isinstance(columns, list) or not isinstance(rows, list):
        return out
    index = _column_index(columns)
    cost_at = _cost_column(columns, index)
    if cost_at is None:
        return out
    resource_at = index.get("resourceid")
    service_at = index.get("servicename")
    currency_at = index.get("currency")
    if currency_at is None:
        currency_at = index.get("billingcurrency")
    date_at = index.get("usagedate")

    for row in rows:
        if not isinstance(row, list) or cost_at >= len(row):
            continue
        out.row_count += 1
        cost = _to_float(row[cost_at])
        out.total += cost
        resource_id = ""
        if resource_at is not None and resource_at < len(row):
            resource_id = str(row[resource_at] or "").strip()
        service = ""
        if service_at is not None and service_at < len(row):
            service = str(row[service_at] or "").strip()
        if currency_at is not None and currency_at < len(row) and not out.currency:
            out.currency = str(row[currency_at] or "").strip()
        if date_at is not None and date_at < len(row) and cost:
            # "Data as of" is the latest day that actually carries cost. A
            # trailing zero-cost day is the API padding the range, not evidence
            # that the day has been processed.
            day = _parse_usage_date(row[date_at])
            if day is not None and (
                out.latest_usage_date is None or day > out.latest_usage_date
            ):
                out.latest_usage_date = day
        if resource_id:
            out.by_resource[resource_id.lower()] = (
                out.by_resource.get(resource_id.lower(), 0.0) + cost
            )
        if service:
            out.by_service[service] = out.by_service.get(service, 0.0) + cost
        out.rows.append((resource_id.lower(), service, cost))
    return out


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


@dataclass
class AzureAggregate:
    """Everything one live fetch produced, with no ids or names that identify
    the account. The snapshot is rebuilt from this on every render, so changing
    the allowance in Settings re-colours the gauge without another API call."""

    total: float = 0.0
    currency: str = ""
    period_start: date | None = None
    period_end: date | None = None
    data_as_of: date | None = None
    buckets: list[tuple[str, float]] = field(default_factory=list)
    foundry_cost: float | None = None
    foundry_resource_count: int = 0
    marketplace_cost: float | None = None
    budget_amount: float | None = None
    budget_time_grain: str | None = None
    forecast_total: float | None = None
    quota_id: str | None = None
    sponsorship: bool = False
    service_count: int = 0
    row_count: int = 0
    cost_metric: str = COST_METRIC_PRIMARY
    # A page of the cost query was left unread, so ``total`` is a subtotal.
    partial: bool = False
    notes: list[str] = field(default_factory=list)


def bucket_costs(
    parsed: QueryRows,
    foundry_ids: set[str],
    marketplace_costs: dict[tuple[str, str], float] | None = None,
    *,
    top_rows: int = MAX_BREAKDOWN_ROWS,
) -> tuple[list[tuple[str, float]], float | None, float | None, int]:
    """Partition rows into display buckets that sum to the total.

    Each cost row lands in exactly one bucket - Foundry, Marketplace, or its
    ServiceName - so the rows can never double-count. That is the whole reason
    Foundry is a row rather than a tile: a Foundry *tile* would show spend that
    the Azure tile is also showing, and nothing in the layout would say so.
    The buckets sum to the query total to within float rounding: they are
    accumulated in a different order from ``QueryRows.total``, so bit-exact
    equality is not something to assert.

    ``foundry_ids`` are matched on resource id, because a Foundry resource's
    whole spend belongs to the Foundry row. ``marketplace_costs`` is keyed on
    the *(resource id, service)* pair and carries an amount, because a
    Marketplace charge is one charge on a resource that also bills ordinary
    Azure usage - keying it on the resource id alone moved that resource's
    entire spend into the Marketplace row.

    ``resource_id`` keys are compared lower-cased; the ids are lowered here so
    a caller that did not do it gets the same answer rather than a silent zero.
    """
    marketplace_costs = {
        (str(rid).lower(), service): cost
        for (rid, service), cost in (marketplace_costs or {}).items()
    }
    remaining = dict(marketplace_costs)
    foundry_ids = {rid.lower() for rid in foundry_ids}
    totals: dict[str, float] = {}
    foundry_cost: float | None = 0.0 if foundry_ids else None
    marketplace_cost: float | None = 0.0 if marketplace_costs else None
    services: set[str] = set()
    for resource_id, service, cost in parsed.rows:
        if foundry_ids and resource_id and resource_id in foundry_ids:
            foundry_cost = (foundry_cost or 0.0) + cost
            totals[FOUNDRY_BUCKET] = totals.get(FOUNDRY_BUCKET, 0.0) + cost
            continue
        # Move at most what the marketplace query said this exact row spent,
        # and never more than the row holds.
        moved = 0.0
        available = remaining.get((resource_id, service), 0.0)
        if available > 0 and cost > 0:
            moved = min(cost, available)
            remaining[(resource_id, service)] = available - moved
            marketplace_cost = (marketplace_cost or 0.0) + moved
            totals[MARKETPLACE_BUCKET] = totals.get(MARKETPLACE_BUCKET, 0.0) + moved
        cost -= moved
        if moved and not cost:
            continue
        bucket = service or "Unattributed"
        services.add(bucket)
        totals[bucket] = totals.get(bucket, 0.0) + cost

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    # Foundry is never folded into "Other": it is the row the tile exists to
    # show, and a quiet month would otherwise hide it.
    pinned = [pair for pair in ranked if pair[0] == FOUNDRY_BUCKET and pair[1]]
    rest = [pair for pair in ranked if pair not in pinned]
    top_rows = max(1, min(MAX_BREAKDOWN_ROWS, top_rows))
    keep = pinned + rest[: max(0, top_rows - len(pinned))]
    remainder = [pair for pair in rest if pair not in keep]
    if remainder:
        other_total = sum(cost for _, cost in remainder)
        keep.append((f"Other ({len(remainder)} services)", other_total))
    return keep, foundry_cost, marketplace_cost, len(services)


# --------------------------------------------------------------------------
# Snapshot construction
# --------------------------------------------------------------------------


def _money(amount: float, currency: str) -> str:
    """Format an amount in the currency the API reported.

    Never a bare ``$``. The subscription's billing currency is whatever the
    Currency column says - CAD here, and something else for the next reader -
    so the code is always shown rather than assumed away.
    """
    return f"{currency} {amount:,.2f}" if currency else f"{amount:,.2f}"


def _truncate(label: str) -> str:
    if len(label) <= LABEL_MAX_LEN:
        return label
    return label[: LABEL_MAX_LEN - 1] + "…"


def _allowance(aggregate: AzureAggregate, azure_cfg) -> tuple[float | None, str]:
    """Resolve the denominator: a real Azure Budget wins over the setting."""
    if aggregate.budget_amount and aggregate.budget_amount > 0:
        return aggregate.budget_amount, "budget"
    allowance = getattr(azure_cfg, "monthly_allowance", None)
    if allowance and allowance > 0:
        return float(allowance), "setting"
    return None, "none"


def _as_of_note(aggregate: AzureAggregate) -> str:
    if aggregate.data_as_of is None:
        return (
            "No usage has been processed for this period yet. Cost Management "
            "lags 8-24 h (up to 72 h on pay-as-you-go)."
        )
    return (
        f"Data as of {aggregate.data_as_of.isoformat()}. Cost Management lags "
        "8-24 h (up to 72 h on pay-as-you-go) and refreshes about six times a "
        "day."
    )


def build_snapshot(
    aggregate: AzureAggregate,
    azure_cfg,
    *,
    fetched_at: datetime | None = None,
) -> UsageSnapshot:
    """Render an aggregate as tile rows.

    Pure: called both after a live fetch and when serving the cached aggregate
    inside the throttle window, so an allowance edited in Settings takes effect
    on the next render rather than on the next API call.
    """
    metrics: list[UsageMetric] = []
    currency = aggregate.currency
    allowance, allowance_source = _allowance(aggregate, azure_cfg)
    resets_at = (
        _local_midnight(aggregate.period_end) if aggregate.period_end else None
    )
    window = (
        _local_midnight(aggregate.period_end) - _local_midnight(aggregate.period_start)
        if aggregate.period_end and aggregate.period_start
        else None
    )

    if aggregate.sponsorship:
        # An Azure Sponsorship offer is not supported by Cost Management: it
        # reports 0 while the credit actually drains. A gauge reading 0% here
        # would be worse than no gauge, so say what is wrong instead.
        metrics.append(
            UsageMetric(
                label="Sponsorship offer — spend not reported",
                percent_used=None,
                note=(
                    "Cost Management does not support Azure Sponsorship offers; "
                    "it reports zero cost while the sponsorship credit is "
                    "consumed. Track this credit in the Azure Sponsorships "
                    "portal instead."
                ),
            )
        )

    spend_text = _money(aggregate.total, currency)
    if allowance and not aggregate.partial:
        percent = max(0.0, min(100.0, aggregate.total / allowance * 100.0))
        label = f"Spend this month ({spend_text} of {allowance:,.2f})"
    elif allowance:
        percent = None
        label = f"Spend this month ({spend_text} of {allowance:,.2f})"
    else:
        percent = None
        label = f"Spend this month ({spend_text})"

    note_parts = [_as_of_note(aggregate)]
    note_parts.append(
        "Costs are gross: Cost Management excludes free and prepaid credits, so "
        "this measures consumption against your stated allowance, not a live "
        "credit balance."
    )
    if aggregate.partial:
        note_parts.append(
            "Cost Management returned more results than were read, so these "
            "results are truncated and the total is incomplete; no gauge is "
            "shown for a subtotal."
        )
    if allowance_source == "budget":
        note_parts.append("Allowance read from an Azure Budget on this subscription.")
    elif allowance_source == "none":
        note_parts.append("Set a monthly allowance in Settings to show a gauge.")
    if aggregate.period_start and aggregate.period_end:
        note_parts.append(
            f"Period {aggregate.period_start.isoformat()} → "
            f"{aggregate.period_end.isoformat()}."
        )
    note_parts.extend(aggregate.notes)

    metrics.append(
        UsageMetric(
            label=label,
            percent_used=None if aggregate.sponsorship else percent,
            resets_at=resets_at,
            note=" ".join(note_parts),
            window=window,
        )
    )

    total = aggregate.total
    for name, cost in aggregate.buckets:
        share = (cost / total * 100.0) if total > 0 else None
        if name == FOUNDRY_BUCKET:
            note = (
                f"{_money(cost, currency)} across "
                f"{aggregate.foundry_resource_count} Foundry resource"
                f"{'' if aggregate.foundry_resource_count == 1 else 's'}. "
                "Foundry bills per token to this subscription, so this is part "
                "of the total above, not a separate credit."
            )
        elif name == MARKETPLACE_BUCKET:
            note = (
                f"{_money(cost, currency)}. Marketplace model charges bill "
                "outside the Foundry resource, at resource-group level."
            )
        else:
            note = _money(cost, currency)
            if len(name) > LABEL_MAX_LEN:
                note = f"{name}\n{note}"
        metrics.append(
            UsageMetric(
                label=_truncate(name),
                percent_used=share,
                note=note,
                tag=BREAKDOWN_TAG,
            )
        )

    if aggregate.forecast_total is not None:
        forecast_share = (
            max(0.0, min(100.0, aggregate.forecast_total / allowance * 100.0))
            if allowance
            else None
        )
        metrics.append(
            UsageMetric(
                label="Forecast end of month",
                percent_used=forecast_share,
                note=(
                    f"~{_money(aggregate.forecast_total, currency)} projected by "
                    "Cost Management for the full period"
                    + (f" ({forecast_share:.0f}% of allowance)." if forecast_share is not None else ".")
                ),
                tag=BREAKDOWN_TAG,
            )
        )

    return UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.OK,
        metrics=metrics,
        fetched_at=fetched_at or datetime.now(),
        raw=_diagnostic_raw(aggregate),
    )


def _diagnostic_raw(aggregate: AzureAggregate) -> dict[str, Any]:
    """What Copy-diagnostics may see.

    Built as an allowlist rather than a redaction pass: no resource id,
    resource-group name, subscription id, or tenant id is put in here at all,
    so error_dialog's redaction is a second line of defence rather than the
    only one.
    """
    return {
        "currency": aggregate.currency,
        "total": round(aggregate.total, 6),
        "period_start": aggregate.period_start.isoformat()
        if aggregate.period_start
        else None,
        "period_end": aggregate.period_end.isoformat()
        if aggregate.period_end
        else None,
        "data_as_of": aggregate.data_as_of.isoformat()
        if aggregate.data_as_of
        else None,
        "buckets": [[name, round(cost, 6)] for name, cost in aggregate.buckets],
        "foundry_cost": aggregate.foundry_cost,
        "foundry_resource_count": aggregate.foundry_resource_count,
        "marketplace_cost": aggregate.marketplace_cost,
        "budget_amount": aggregate.budget_amount,
        "budget_time_grain": aggregate.budget_time_grain,
        "forecast_total": aggregate.forecast_total,
        "quota_id": aggregate.quota_id,
        "sponsorship": aggregate.sponsorship,
        "service_count": aggregate.service_count,
        "row_count": aggregate.row_count,
        "cost_metric": aggregate.cost_metric,
        "notes": list(aggregate.notes),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        # Gives this app its own Cost Management rate-limit bucket instead of
        # sharing the anonymous one with every other client that omits it.
        "ClientType": CLIENT_TYPE,
    }


class AzureThrottled(Exception):
    """A 429 from Cost Management, carrying the server's own back-off."""

    def __init__(self, retry_after: int):
        super().__init__(f"Cost Management throttled the request ({retry_after}s)")
        self.retry_after = retry_after


class AzurePermissionError(Exception):
    """401/403 from ARM: the app registration is missing a role."""


def retry_after_seconds(headers: Any) -> int:
    """Read the back-off Cost Management asked for.

    Cost Management does not use one header: it emits a family of
    ``x-ms-ratelimit-microsoft.costmanagement-<bucket>-retry-after`` headers
    (qpu, entity, tenant, client, clienttype), and which one appears depends on
    which quota was exhausted. Take the largest of whatever is present, and
    fall back to the standard Retry-After.
    """
    values: list[int] = []
    try:
        items = headers.items()
    except AttributeError:
        return int(MIN_FETCH_INTERVAL.total_seconds())
    for name, value in items:
        lowered = str(name).lower()
        if lowered == "retry-after" or (
            lowered.startswith("x-ms-ratelimit-microsoft.costmanagement-")
            and lowered.endswith("-retry-after")
        ):
            try:
                number = float(str(value).strip())
            except (TypeError, ValueError, OverflowError):
                continue
            # int(float("inf")) raises OverflowError, and this call sits inside
            # the `raise AzureThrottled(...)` expression: letting it escape
            # discarded the server's own back-off along with the 429 handling.
            if not math.isfinite(number):
                continue
            values.append(int(number))
    positive = [v for v in values if v > 0]
    if not positive:
        return int(MIN_FETCH_INTERVAL.total_seconds())
    # Cap so a pathological header cannot park the tile for a week.
    return min(max(positive), int(MAX_ERROR_BACKOFF.total_seconds()))


def _check(response: requests.Response, what: str) -> None:
    if response.status_code == 429:
        raise AzureThrottled(retry_after_seconds(response.headers))
    if response.status_code in (401, 403):
        raise AzurePermissionError(
            f"Azure rejected the {what} request ({response.status_code}). "
            + _PERMISSION_HINT
        )


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def arm_post(token: str, url: str, body: dict, what: str) -> requests.Response:
    # allow_redirects=False on every ARM call: the only host this module
    # speaks to is fixed, so a redirect is a failure, never something to follow.
    response = requests.post(
        url,
        json=body,
        headers=_headers(token),
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
    )
    _check(response, what)
    return response


def arm_get(token: str, url: str, what: str) -> requests.Response:
    response = requests.get(
        url, headers=_headers(token), timeout=REQUEST_TIMEOUT, allow_redirects=False
    )
    _check(response, what)
    return response


def _scope(subscription_id: str) -> str:
    return f"{MANAGEMENT_HOST}/subscriptions/{subscription_id}"


def query_body(
    period_start: date,
    period_end: date,
    *,
    cost_metric: str,
    resource_group: str | None = None,
    marketplace_only: bool = False,
) -> dict:
    """Build the Query API request.

    ``timeframe: Custom`` rather than ``MonthToDate`` on purpose: MonthToDate
    is the calendar month, and a credit-backed subscription resets on an
    anniversary that is usually not the 1st. Daily granularity is what makes
    "data as of" derivable at all - without it there is no UsageDate column and
    no way to tell a fully-processed period from a half-processed one.
    """
    filters: list[dict] = [
        {
            "dimensions": {
                "name": "ChargeType",
                "operator": "In",
                "values": ["Usage"],
            }
        }
    ]
    if marketplace_only:
        filters.append(
            {
                "dimensions": {
                    "name": "PublisherType",
                    "operator": "In",
                    "values": ["Marketplace"],
                }
            }
        )
    if resource_group:
        filters.append(
            {
                "dimensions": {
                    "name": "ResourceGroupName",
                    "operator": "In",
                    "values": [resource_group],
                }
            }
        )
    # QueryFilter's "and" requires at least two clauses; a lone clause goes in
    # unwrapped.
    query_filter = filters[0] if len(filters) == 1 else {"and": filters}

    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{period_start.isoformat()}T00:00:00+00:00",
            "to": f"{(period_end - timedelta(days=1)).isoformat()}T23:59:59+00:00",
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": cost_metric, "function": "Sum"}},
            # The Query API caps grouping at two dimensions (maxItems: 2 in the
            # REST spec). ResourceId is what classification needs; ServiceName
            # is what the component rows are named from.
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "ServiceName"},
            ],
            "filter": query_filter,
        },
    }


def _next_query_link(payload: Any) -> tuple[str, bool]:
    """Return (url_to_follow, refused).

    ``nextLink`` is server-supplied, so it gets the same host pin the Cognitive
    Services page loop uses: only a link back to ARM is followed, and a link
    anywhere else stops the loop rather than being requested.
    """
    properties = payload.get("properties") if isinstance(payload, dict) else None
    link = properties.get("nextLink") if isinstance(properties, dict) else None
    if not isinstance(link, str) or not link:
        return "", False
    if not link.startswith(f"{MANAGEMENT_HOST}/"):
        log.warning(
            "provider api diagnosis provider=azure "
            "classification=query_nextlink_refused"
        )
        return "", True
    return link, False


def _merge_query_rows(into: QueryRows, page: QueryRows) -> None:
    """Fold one page of results into the running total."""
    into.total += page.total
    if not into.currency:
        into.currency = page.currency
    for resource_id, cost in page.by_resource.items():
        into.by_resource[resource_id] = into.by_resource.get(resource_id, 0.0) + cost
    for service, cost in page.by_service.items():
        into.by_service[service] = into.by_service.get(service, 0.0) + cost
    into.rows.extend(page.rows)
    into.row_count += page.row_count
    if page.latest_usage_date is not None and (
        into.latest_usage_date is None
        or page.latest_usage_date > into.latest_usage_date
    ):
        into.latest_usage_date = page.latest_usage_date
    into.truncated = into.truncated or page.truncated


def _follow_query_pages(
    token: str, body: dict, payload: Any, parsed: QueryRows
) -> None:
    """Read the remaining pages of a query into ``parsed``.

    Cost Management continues a POST query by re-POSTing the same body to the
    ``nextLink`` it returned.
    """
    pages = 1
    while True:
        url, refused = _next_query_link(payload)
        if refused:
            parsed.truncated = True
            return
        if not url:
            return
        if pages >= MAX_QUERY_PAGES:
            parsed.truncated = True
            return
        response = arm_post(token, url, body, "cost query")
        if response.status_code != 200:
            parsed.truncated = True
            return
        payload = _json(response)
        _merge_query_rows(parsed, parse_query_response(payload))
        pages += 1


def fetch_query(
    token: str,
    subscription_id: str,
    period_start: date,
    period_end: date,
    *,
    cost_metric: str = COST_METRIC_PRIMARY,
    resource_group: str | None = None,
    marketplace_only: bool = False,
) -> tuple[QueryRows, str]:
    """Run one cost query, retrying once with the other cost metric name.

    Follows ``nextLink`` until the results run out, the link points somewhere
    other than ARM, or the page cap is reached; the last two mark the result
    truncated. Returns (parsed, metric_name_that_worked).
    """
    url = (
        f"{_scope(subscription_id)}/providers/Microsoft.CostManagement/query"
        f"?api-version={COST_MANAGEMENT_API_VERSION}"
    )
    attempts = [cost_metric]
    if cost_metric == COST_METRIC_PRIMARY:
        attempts.append(COST_METRIC_FALLBACK)
    last_status = 0
    for metric in attempts:
        body = query_body(
            period_start,
            period_end,
            cost_metric=metric,
            resource_group=resource_group,
            marketplace_only=marketplace_only,
        )
        response = arm_post(token, url, body, "cost query")
        if response.status_code == 200:
            payload = _json(response)
            parsed = parse_query_response(payload)
            _follow_query_pages(token, body, payload, parsed)
            log.debug(
                "provider api diagnosis provider=azure classification=query_ok "
                "metric=%s marketplace=%s rows=%s truncated=%s",
                metric,
                marketplace_only,
                parsed.row_count,
                parsed.truncated,
            )
            return parsed, metric
        last_status = response.status_code
        if response.status_code != 400:
            break
        log.info(
            "provider api diagnosis provider=azure classification=query_metric_rejected "
            "metric=%s status=400",
            metric,
        )
    raise requests.HTTPError(f"Cost Management query returned HTTP {last_status}")


def fetch_forecast(
    token: str,
    subscription_id: str,
    period_start: date,
    period_end: date,
    *,
    cost_metric: str,
    resource_group: str | None = None,
) -> float | None:
    """Projected spend for the whole period, or None when unavailable.

    A forecast needs enough history to extrapolate from; a new subscription or
    a short period gets a 4xx or an empty result. That is expected, not an
    error - the row is simply omitted.
    """
    url = (
        f"{_scope(subscription_id)}/providers/Microsoft.CostManagement/forecast"
        f"?api-version={COST_MANAGEMENT_API_VERSION}"
    )
    body: dict[str, Any] = {
        "type": "ActualCost",
        # ForecastTimeframe's only legal value is Custom.
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{period_start.isoformat()}T00:00:00+00:00",
            "to": f"{(period_end - timedelta(days=1)).isoformat()}T23:59:59+00:00",
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": cost_metric, "function": "Sum"}},
        },
        # Actual-to-date plus projected-remainder, so the row is a full-period
        # number rather than only the unspent tail.
        "includeActualCost": True,
        "includeFreshPartialCost": False,
    }
    if resource_group:
        body["dataset"]["filter"] = {
            "dimensions": {
                "name": "ResourceGroupName",
                "operator": "In",
                "values": [resource_group],
            }
        }
    response = arm_post(token, url, body, "forecast")
    if response.status_code != 200:
        log.info(
            "provider api diagnosis provider=azure classification=forecast_unavailable "
            "status=%s",
            response.status_code,
        )
        return None
    parsed = parse_query_response(_json(response))
    if parsed.row_count == 0:
        return None
    return parsed.total


def fetch_budget(token: str, subscription_id: str) -> tuple[float | None, str | None]:
    """A real monthly cost Budget on the subscription, if one exists.

    Preferred over the settings allowance: if the owner already told Azure what
    the monthly number is, restating it in this app is a second copy to keep in
    sync. Returns (amount, timeGrain).
    """
    url = (
        f"{_scope(subscription_id)}/providers/Microsoft.Consumption/budgets"
        f"?api-version={CONSUMPTION_BUDGETS_API_VERSION}"
    )
    response = arm_get(token, url, "budgets")
    if response.status_code != 200:
        log.info(
            "provider api diagnosis provider=azure classification=budgets_unavailable "
            "status=%s",
            response.status_code,
        )
        return None, None
    payload = _json(response)
    values = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return None, None
    for item in values:
        if not isinstance(item, dict):
            continue
        properties = item.get("properties")
        if not isinstance(properties, dict):
            continue
        if str(properties.get("category") or "").strip().lower() != "cost":
            continue
        if str(properties.get("timeGrain") or "").strip().lower() != "monthly":
            continue
        amount = properties.get("amount")
        try:
            value = float(amount)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value, str(properties.get("timeGrain"))
    return None, None


def fetch_quota_id(token: str, subscription_id: str) -> str | None:
    """The subscription's offer, as ``subscriptionPolicies.quotaId``.

    Worth one call because Azure Sponsorship offers are not supported by Cost
    Management at all: they report zero while the credit drains, and a gauge
    reading 0% is a lie the user has no way to detect.
    """
    url = f"{_scope(subscription_id)}?api-version={SUBSCRIPTIONS_API_VERSION}"
    response = arm_get(token, url, "subscription")
    if response.status_code != 200:
        return None
    payload = _json(response)
    if not isinstance(payload, dict):
        return None
    policies = payload.get("subscriptionPolicies")
    if not isinstance(policies, dict):
        return None
    quota_id = policies.get("quotaId")
    return str(quota_id) if quota_id else None


def is_sponsorship(quota_id: str | None) -> bool:
    """Match on the family, not one literal.

    ``Sponsored_2016-01-01`` is the documented pay-as-you-go sponsorship quota
    id, and EA Azure Sponsorship (MS-AZR-0136P) is listed as unsupported with
    no quota id published at all. A substring test covers both and any future
    sibling, and a false positive costs a warning row rather than a wrong gauge.
    """
    return "sponsor" in (quota_id or "").lower()


def fetch_foundry_resource_ids(token: str, subscription_id: str) -> set[str]:
    """Foundry resource ids, discovered by resource *kind*.

    Not by ResourceType: Azure OpenAI, Speech, Vision, Language and Foundry all
    live under ``Microsoft.CognitiveServices/accounts``, so a type filter would
    sweep every one of them into the Foundry row. ``kind == "AIServices"`` is
    what distinguishes a Foundry resource. Projects are child resources and
    bill to the parent account, whose id is the one that appears in cost rows.
    """
    url = (
        f"{_scope(subscription_id)}/providers/Microsoft.CognitiveServices/accounts"
        f"?api-version={COGNITIVE_SERVICES_API_VERSION}"
    )
    found: set[str] = set()
    pages = 0
    while url and pages < 10:
        response = arm_get(token, url, "Cognitive Services accounts")
        if response.status_code != 200:
            log.info(
                "provider api diagnosis provider=azure "
                "classification=foundry_discovery_unavailable status=%s",
                response.status_code,
            )
            break
        payload = _json(response)
        if not isinstance(payload, dict):
            break
        items = payload.get("value")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "").strip().lower() != FOUNDRY_ACCOUNT_KIND:
                continue
            resource_id = item.get("id")
            if isinstance(resource_id, str) and resource_id:
                found.add(resource_id.lower())
        # nextLink is server-supplied and is not guaranteed to be a string:
        # a non-string one used to raise AttributeError out of the whole fetch.
        next_link = payload.get("nextLink")
        url = next_link if isinstance(next_link, str) else ""
        if url and not url.startswith(f"{MANAGEMENT_HOST}/"):
            # nextLink is server-supplied; only follow it back to ARM.
            break
        pages += 1
    return found


# --------------------------------------------------------------------------
# Throttle / cache
# --------------------------------------------------------------------------


@dataclass
class _State:
    # Which credentials the cached data belongs to. State is keyed by
    # subscription, so pointing the same subscription at a different app
    # registration would otherwise keep serving the old tenant's numbers - and,
    # worse, a user who has just fixed a wrong client secret would keep seeing
    # the auth error for the rest of the backoff window. See _identity().
    identity: tuple | None = None
    last_fetch_at: datetime | None = None
    # Set before a work item is dispatched and cleared when it finishes. Two
    # refreshes arriving before the first completes would otherwise both pass
    # the gate, because the gate only short-circuits on state that a *finished*
    # fetch leaves behind.
    in_flight: bool = False
    blocked_until: datetime | None = None
    consecutive_errors: int = 0
    aggregate: AzureAggregate | None = None
    fetched_at: datetime | None = None
    last_error: UsageSnapshot | None = None
    discovery_at: datetime | None = None
    foundry_ids: set[str] = field(default_factory=set)
    quota_id: str | None = None
    cost_metric: str = COST_METRIC_PRIMARY


# Module-level, keyed by subscription id: App rebuilds every provider on each
# settings save, so per-instance state would reset the throttle every time the
# user pressed OK - exactly the hammering this is here to prevent.
_STATES: dict[str, _State] = {}
_STATES_LOCK = threading.Lock()


def _identity(tenant_id: str, client_id: str, client_secret: str) -> tuple[str, str, str]:
    """A change detector for the credential set, not a place to keep a secret.

    The secret is reduced to a truncated digest purely so that *rotating* it
    invalidates the cached backoff: without this, a user who fixed a wrong
    client secret would go on seeing the auth error until the backoff expired,
    which is exactly when they are looking at the tile to see whether the fix
    worked. An unchanged wrong secret still backs off, so a bad credential is
    not retried every five minutes against Entra ID.
    """
    digest = hashlib.sha256(client_secret.encode("utf-8", "replace")).hexdigest()
    return (tenant_id, client_id, digest[:16])


def state_for(subscription_id: str) -> _State:
    with _STATES_LOCK:
        return _STATES.setdefault(subscription_id, _State())


def reset_states() -> None:
    """Drop all throttle/cache state (settings change, tests)."""
    with _STATES_LOCK:
        _STATES.clear()


def next_allowed_at(state: _State) -> datetime | None:
    candidates = [
        stamp
        for stamp in (
            state.last_fetch_at + MIN_FETCH_INTERVAL if state.last_fetch_at else None,
            state.blocked_until,
        )
        if stamp is not None
    ]
    return max(candidates) if candidates else None


def _error_backoff(consecutive_errors: int) -> timedelta:
    # The exponent is capped: 2 ** 1029 overflows the float multiply, and
    # anything past the 6 h ceiling is the same wait either way.
    exponent = min(max(0, consecutive_errors - 1), 32)
    seconds = MIN_FETCH_INTERVAL.total_seconds() * (2 ** exponent)
    return timedelta(seconds=min(seconds, MAX_ERROR_BACKOFF.total_seconds()))


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


def _auth_required(message: str) -> UsageSnapshot:
    return UsageSnapshot(
        provider="azure", status=SnapshotStatus.AUTH_REQUIRED, error=message
    )


def _error(message: str) -> UsageSnapshot:
    return UsageSnapshot(provider="azure", status=SnapshotStatus.ERROR, error=message)


def _exception_summary(exc: BaseException) -> str:
    """Name a failure without quoting it.

    ``str(exc)`` on a requests transport exception embeds the whole request
    URL, and every ARM URL in this module contains ``/subscriptions/<guid>/``
    or ``/<tenant guid>/oauth2``. That string reaches ai-gauge.log, the tile
    tooltip and the error-dialog header, none of which redact. The type name -
    plus the status when there is one - is what a bug report actually needs.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"{type(exc).__name__}, HTTP {status}"
    return type(exc).__name__


class AzureProvider(Provider):
    name = "azure"
    display_name = "Microsoft · Azure"

    def __init__(self, config: Config, pool=None):
        self._config = config
        self._pool = pool

    def refresh(self, on_done: Callable[[UsageSnapshot], None]) -> None:
        azure_cfg = self._config.azure
        if not azure_cfg.is_configured():
            on_done(
                _auth_required(
                    "Azure is not configured. Add the tenant, client, and "
                    "subscription ids in Settings → Microsoft → Azure."
                )
            )
            return
        secret = get_azure_client_secret()
        if not secret:
            on_done(
                _auth_required(
                    "Azure client secret not set. Add it in Settings → "
                    "Microsoft → Azure."
                )
            )
            return

        subscription_id = azure_cfg.subscription_id or ""
        tenant_id = azure_cfg.tenant_id or ""
        client_id = azure_cfg.client_id or ""
        state = state_for(subscription_id)
        now = datetime.now()

        # A clock that jumped forward (bad RTC, an NTP correction, a DST
        # fall-back) leaves stamps in the future that no elapsed time can ever
        # reach, which would park the tile on its cached number forever. A
        # last_fetch_at later than now is impossible; a blocked_until further
        # out than the largest back-off we can produce is too.
        if state.last_fetch_at is not None and state.last_fetch_at > now:
            state.last_fetch_at = None
        if (
            state.blocked_until is not None
            and state.blocked_until > now + MAX_ERROR_BACKOFF
        ):
            state.blocked_until = None

        identity = _identity(tenant_id, client_id, secret)
        if state.identity is not None and state.identity != identity:
            # Re-pointed at a different app registration: everything cached
            # here describes the old one, including the throttle window.
            log.info(
                "provider api diagnosis provider=azure "
                "classification=identity_changed cache_cleared=1"
            )
            state = _State()
            with _STATES_LOCK:
                _STATES[subscription_id] = state
        state.identity = identity

        # Serve the cache rather than the API. The refresh loop above this can
        # fire every five minutes when the user is active, and every minute
        # while any provider is erroring; this is the only thing standing
        # between that loop and a tenant-wide rate limit.
        def serve_cache() -> None:
            on_done(
                build_snapshot(state.aggregate, azure_cfg, fetched_at=state.fetched_at)
            )

        if state.in_flight:
            # A fetch is out. Dispatching a second one would double the request
            # count for a number that is about to arrive anyway.
            if state.aggregate is not None:
                serve_cache()
            else:
                on_done(_error("An Azure fetch is already in progress."))
            return

        allowed_at = next_allowed_at(state)
        if allowed_at is not None and now < allowed_at:
            if state.aggregate is not None:
                log.debug(
                    "provider api diagnosis provider=azure "
                    "classification=throttled_serving_cache next_fetch_in_s=%s",
                    int((allowed_at - now).total_seconds()),
                )
                serve_cache()
                return
            if state.last_error is not None:
                on_done(state.last_error)
                return
            # Nothing cached and nothing remembered, but the window is still
            # shut. Falling through to a live fetch here is what let a single
            # unexpected exception - which leaves the state blank - turn the
            # hourly floor into a fetch on every refresh cycle. The gate fails
            # closed instead: no state to serve is not permission to fetch.
            minutes = max(1, int((allowed_at - now).total_seconds() // 60))
            log.info(
                "provider api diagnosis provider=azure "
                "classification=throttled_no_cache next_fetch_in_s=%s",
                int((allowed_at - now).total_seconds()),
            )
            on_done(
                _error(
                    f"Waiting for the next Azure fetch window ({minutes} min)."
                )
            )
            return

        state.last_fetch_at = now
        state.in_flight = True

        def work() -> UsageSnapshot:
            try:
                return self._fetch(
                    state,
                    azure_cfg,
                    tenant_id=tenant_id,
                    client_id=client_id,
                    client_secret=secret,
                    subscription_id=subscription_id,
                )
            except Exception as exc:  # noqa: BLE001
                # Anything _fetch does not name still has to be *recorded*:
                # an exception that leaves _State blank is an exception that
                # switches the throttle off. The type name only - a message
                # can carry the request URL, and with it the subscription id.
                log.exception(
                    "provider api diagnosis provider=azure "
                    "classification=unexpected_exception type=%s",
                    type(exc).__name__,
                )
                return self._remember_error(
                    state,
                    _error(f"Azure refresh failed ({type(exc).__name__})."),
                )
            finally:
                state.in_flight = False

        self._run_async(work, on_done)

    # -- the fetch itself ---------------------------------------------------

    def _fetch(
        self,
        state: _State,
        azure_cfg,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
    ) -> UsageSnapshot:
        try:
            token = get_token(tenant_id, client_id, client_secret)
        except AzureAuthError as exc:
            return self._remember_error(state, _auth_required(str(exc)))
        except requests.RequestException as exc:
            return self._remember_error(
                state,
                _error(f"Could not reach Entra ID ({_exception_summary(exc)})."),
            )

        notes: list[str] = []
        now = datetime.now()
        period_start, period_end = period_bounds(now.date(), azure_cfg.reset_day)

        try:
            # Discovery: near-static, so it is cached for a day and its failure
            # is a note rather than an error.
            if (
                state.discovery_at is None
                or now - state.discovery_at > DISCOVERY_TTL
            ):
                state.quota_id = self._safe_quota_id(token, subscription_id, notes)
                state.foundry_ids = self._safe_foundry_ids(
                    token, subscription_id, notes
                )
                state.discovery_at = now

            pinned = {rid.lower() for rid in azure_cfg.foundry_resource_ids}
            foundry_ids = pinned or state.foundry_ids
            if pinned:
                notes.append("Foundry resources pinned in Settings.")
            elif not foundry_ids:
                notes.append(
                    "No Foundry resources found; pin their resource ids under "
                    "Settings → Microsoft → Foundry if the app registration "
                    "cannot list them."
                )

            parsed, cost_metric = fetch_query(
                token,
                subscription_id,
                period_start,
                period_end,
                cost_metric=state.cost_metric,
                resource_group=azure_cfg.resource_group,
            )
            state.cost_metric = cost_metric

            marketplace_costs: dict[tuple[str, str], float] = {}
            marketplace_cost: float | None = None
            if azure_cfg.include_marketplace:
                try:
                    mp_parsed, _ = fetch_query(
                        token,
                        subscription_id,
                        period_start,
                        period_end,
                        cost_metric=cost_metric,
                        resource_group=azure_cfg.resource_group,
                        marketplace_only=True,
                    )
                    for rid, service, cost in mp_parsed.rows:
                        if not rid:
                            continue
                        key = (rid, service)
                        marketplace_costs[key] = (
                            marketplace_costs.get(key, 0.0) + cost
                        )
                    marketplace_cost = mp_parsed.total
                except (requests.HTTPError, requests.RequestException):
                    notes.append("Marketplace breakdown unavailable this refresh.")

            buckets, foundry_cost, bucket_marketplace, service_count = bucket_costs(
                parsed,
                foundry_ids,
                marketplace_costs,
                top_rows=azure_cfg.top_rows,
            )

            budget_amount, budget_grain = self._safe_budget(
                token, subscription_id, notes
            )
            forecast_total = self._safe_forecast(
                token,
                subscription_id,
                period_start,
                period_end,
                cost_metric=cost_metric,
                resource_group=azure_cfg.resource_group,
            )
        except AzureThrottled as exc:
            state.blocked_until = datetime.now() + timedelta(seconds=exc.retry_after)
            log.warning(
                "provider api diagnosis provider=azure classification=throttled_429 "
                "retry_after_s=%s",
                exc.retry_after,
            )
            if state.aggregate is not None:
                return build_snapshot(
                    state.aggregate, azure_cfg, fetched_at=state.fetched_at
                )
            return self._remember_error(
                state,
                _error(
                    "Cost Management is rate limiting this tenant; retrying in "
                    f"{max(1, exc.retry_after // 60)} min."
                ),
                backoff=False,
            )
        except AzurePermissionError as exc:
            return self._remember_error(state, _auth_required(str(exc)))
        except requests.HTTPError as exc:
            return self._remember_error(state, _error(str(exc)))
        except requests.RequestException as exc:
            return self._remember_error(
                state, _error(f"Azure request failed ({_exception_summary(exc)}).")
            )

        aggregate = AzureAggregate(
            total=parsed.total,
            currency=parsed.currency,
            period_start=period_start,
            period_end=period_end,
            data_as_of=parsed.latest_usage_date,
            buckets=buckets,
            foundry_cost=foundry_cost,
            foundry_resource_count=len(foundry_ids),
            marketplace_cost=(
                bucket_marketplace if bucket_marketplace is not None else marketplace_cost
            ),
            budget_amount=budget_amount,
            budget_time_grain=budget_grain,
            forecast_total=forecast_total,
            quota_id=state.quota_id,
            sponsorship=is_sponsorship(state.quota_id),
            service_count=service_count,
            row_count=parsed.row_count,
            cost_metric=cost_metric,
            partial=parsed.truncated,
            notes=notes,
        )
        fetched_at = datetime.now()
        state.aggregate = aggregate
        state.fetched_at = fetched_at
        state.consecutive_errors = 0
        state.blocked_until = None
        state.last_error = None
        log.info(
            "provider api diagnosis provider=azure classification=snapshot "
            "rows=%s services=%s buckets=%s currency=%s data_as_of=%s "
            "sponsorship=%s forecast=%s",
            parsed.row_count,
            service_count,
            len(buckets),
            parsed.currency or "unknown",
            aggregate.data_as_of,
            aggregate.sponsorship,
            forecast_total is not None,
        )
        return build_snapshot(aggregate, azure_cfg, fetched_at=fetched_at)

    # -- tolerant sub-fetches ----------------------------------------------

    def _safe_quota_id(
        self, token: str, subscription_id: str, notes: list[str]
    ) -> str | None:
        try:
            return fetch_quota_id(token, subscription_id)
        except AzurePermissionError:
            notes.append("Offer type unknown (Reader role missing).")
            return None
        except Exception:  # noqa: BLE001 - this layer is tolerant by design:
            # discovery failure is a note, never the reason the tile breaks.
            return None

    def _safe_foundry_ids(
        self, token: str, subscription_id: str, notes: list[str]
    ) -> set[str]:
        try:
            return fetch_foundry_resource_ids(token, subscription_id)
        except AzurePermissionError:
            notes.append(
                "Cannot list Foundry resources (Reader role missing); pin their "
                "resource ids under Settings → Microsoft → Foundry."
            )
            return set()
        except Exception:  # noqa: BLE001 - tolerant by design; see above.
            return set()

    def _safe_budget(
        self, token: str, subscription_id: str, notes: list[str]
    ) -> tuple[float | None, str | None]:
        try:
            return fetch_budget(token, subscription_id)
        except AzurePermissionError:
            return None, None
        except (requests.HTTPError, requests.RequestException):
            return None, None

    def _safe_forecast(
        self,
        token: str,
        subscription_id: str,
        period_start: date,
        period_end: date,
        *,
        cost_metric: str,
        resource_group: str | None,
    ) -> float | None:
        try:
            return fetch_forecast(
                token,
                subscription_id,
                period_start,
                period_end,
                cost_metric=cost_metric,
                resource_group=resource_group,
            )
        except AzurePermissionError:
            return None
        except (requests.HTTPError, requests.RequestException):
            return None

    def _remember_error(
        self, state: _State, snapshot: UsageSnapshot, *, backoff: bool = True
    ) -> UsageSnapshot:
        state.last_error = snapshot
        if backoff:
            state.consecutive_errors += 1
            state.blocked_until = datetime.now() + _error_backoff(
                state.consecutive_errors
            )
        return snapshot

    def _run_async(
        self,
        work: Callable[[], UsageSnapshot],
        on_done: Callable[[UsageSnapshot], None],
    ) -> None:
        from PyQt6.QtCore import QRunnable, QThreadPool  # local import: keep tests Qt-free

        class _Worker(QRunnable):
            def run(self_inner) -> None:  # noqa: N805
                try:
                    snapshot = work()
                except Exception as exc:  # noqa: BLE001 - work() already
                    # records its own failures; this only covers the dispatch.
                    log.exception(
                        "provider api diagnosis provider=azure "
                        "classification=unexpected_exception type=%s",
                        type(exc).__name__,
                    )
                    snapshot = _error(f"Azure refresh failed ({type(exc).__name__}).")
                on_done(snapshot)

        pool = self._pool or QThreadPool.globalInstance()
        pool.start(_Worker())


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------


def _probe() -> int:
    """python -m aigauge.providers.azure --probe

    Prints, against the real account, the handful of things that cannot be
    verified from documentation: which cost-metric name this billing system
    accepts, what the Currency column actually says, which quotaId the offer
    is, and whether any resource comes back with kind == AIServices. Output is
    sanitized - no ids, no resource-group or resource names.
    """
    config = Config.load()
    azure_cfg = config.azure
    if not azure_cfg.is_configured():
        print("azure: tenant/client/subscription ids are not configured")
        return 2
    secret = get_azure_client_secret()
    if not secret:
        print("azure: no client secret in the credential store")
        return 2

    try:
        token = get_token(azure_cfg.tenant_id, azure_cfg.client_id, secret)
    except (AzureAuthError, requests.RequestException) as exc:
        # Never {exc}: a transport error carries the tenant id in its URL.
        print(f"azure: token request failed ({_exception_summary(exc)})")
        return 1
    print("token: ok")

    subscription_id = azure_cfg.subscription_id or ""
    start, end = period_bounds(date.today(), azure_cfg.reset_day)
    print(f"period: {start} -> {end} (reset day {azure_cfg.reset_day})")

    try:
        quota_id = fetch_quota_id(token, subscription_id)
        print(f"offer quotaId: {quota_id or 'unavailable'}")
        print(f"sponsorship (unsupported by Cost Management): {is_sponsorship(quota_id)}")
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        print(f"offer quotaId: failed ({type(exc).__name__})")

    try:
        foundry = fetch_foundry_resource_ids(token, subscription_id)
        print(f"Foundry resources (kind=AIServices): {len(foundry)}")
    except Exception as exc:  # noqa: BLE001
        foundry = set()
        print(f"Foundry resources: failed ({type(exc).__name__})")

    try:
        parsed, metric = fetch_query(
            token,
            subscription_id,
            start,
            end,
            resource_group=azure_cfg.resource_group,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"cost query: failed ({_exception_summary(exc)})")
        return 1
    print(f"cost metric accepted: {metric}")
    print(f"currency reported: {parsed.currency or '(none)'}")
    print(f"rows: {parsed.row_count}  total: {parsed.total:.2f}")
    print(f"data as of: {parsed.latest_usage_date or '(no dated rows)'}")
    print("ServiceName values seen:")
    for name, cost in sorted(
        parsed.by_service.items(), key=lambda kv: kv[1], reverse=True
    )[:15]:
        print(f"  {name}: {cost:.2f}")

    buckets, foundry_cost, _mp, service_count = bucket_costs(
        parsed, foundry, {}, top_rows=azure_cfg.top_rows
    )
    print(f"buckets ({service_count} distinct services):")
    for name, cost in buckets:
        print(f"  {name}: {cost:.2f}")
    print(f"Foundry roll-up: {foundry_cost if foundry_cost is not None else 'n/a'}")

    try:
        amount, grain = fetch_budget(token, subscription_id)
        print(f"azure budget: {amount if amount else 'none'} ({grain or '-'})")
    except Exception as exc:  # noqa: BLE001
        print(f"azure budget: failed ({type(exc).__name__})")

    try:
        forecast = fetch_forecast(
            token, subscription_id, start, end, cost_metric=metric,
            resource_group=azure_cfg.resource_group,
        )
        print(f"forecast: {forecast if forecast is not None else 'unavailable'}")
    except Exception as exc:  # noqa: BLE001
        print(f"forecast: failed ({type(exc).__name__})")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator tool
    import sys

    if "--probe" in sys.argv:
        raise SystemExit(_probe())
    print("usage: python -m aigauge.providers.azure --probe")
    raise SystemExit(2)
