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
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import requests

from ..config import Config, get_azure_client_secret, validate_azure_guid
from ..models import SnapshotStatus, UsageMetric, UsageSnapshot
from ._azure_auth import AzureAuthError, clear_cache, get_token, invalidate
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
# in_flight is set by the caller and cleared by the worker, so a worker that
# never reports back - killed, hung on a socket the timeout did not cover -
# would hold the tile until the process restarts. Nothing else can clear it,
# so it expires.
IN_FLIGHT_STALE_AFTER = timedelta(minutes=15)

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
# One refresh's own ceiling, on top of the per-request timeout. Page loops are
# the only thing here that multiplies: 20 cost-query pages plus 20 marketplace
# pages plus 10 discovery pages at REQUEST_TIMEOUT each is ~13 min on one
# QThreadPool thread that every other provider is queued behind.
REFRESH_DEADLINE_SECONDS = 90.0
MAX_ARM_REQUESTS_PER_REFRESH = 40
# A ceiling on what one response can cost us in memory and time. A period with
# 50 000 daily ResourceId x ServiceName rows is already outside this tile's
# design; past this the total is a subtotal and says so.
MAX_QUERY_ROWS = 50_000
# Currency codes are three letters and service names are a phrase. Both come
# off the wire and both reach a Qt label and current.json.
CURRENCY_MAX_LEN = 8
SERVICE_NAME_MAX_LEN = 120
# The same ceiling AzureConfig puts on a typed allowance. A budget above it is
# not a denominator - 1e308 is finite, passes _to_float, and renders every
# possible spend as 0%.
MAX_ALLOWANCE = 100_000_000.0

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


def _utc_today() -> date:
    """Today, as Cost Management dates it.

    Cost Management works in UTC dates, and the query window is sent as
    ``+00:00`` timestamps. Deriving the period from the *local* calendar date
    made the two disagree by the UTC offset around the reset instant: east of
    UTC the new period opened up to 13 h early and the query asked for a window
    that had not begun - returning nothing, showing "no usage processed yet",
    and holding that for an hour behind the throttle. Copilot already computes
    UTC month bounds, so this also stops the two Microsoft tiles disagreeing
    about when the same 1st of the month resets.
    """
    return datetime.now(timezone.utc).date()


def _boundary_local(day: date) -> datetime:
    """A period boundary (a UTC date) as a local wall-clock time.

    The boundary is a UTC instant; the countdown next to the bar is read in
    local time, so it is converted rather than reinterpreted.
    """
    return (
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        .astimezone()
        .replace(tzinfo=None)
    )


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
    # Last resort: a numeric column that *names itself* a cost. The candidate
    # list already covers every documented name, so this only has to survive a
    # rename. It deliberately no longer falls back on position: the first
    # numeric non-date column can be a quantity, and summing a quantity into a
    # money label is a wrong number rather than a visible failure.
    for position, column in enumerate(columns or []):
        if not isinstance(column, dict):
            continue
        name = str(column.get("name") or "").strip().lower()
        if name in ("usagedate", "billingmonth") or "cost" not in name:
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
    # False when the response carried no readable cost column at all - schema
    # drift, or an ARM error document returned with a 200. That is an error,
    # not a month with no spend, and the two must not look alike.
    cost_column_found: bool = False
    # More than one billing currency in one response: the rows cannot be added
    # together, so no gauge is shown for the sum.
    mixed_currency: bool = False
    # Set when a page of results was left unread - a refused nextLink or the
    # page cap. The total is then a subtotal, and must not be shown as a gauge.
    truncated: bool = False


def parse_query_response(payload: Any) -> QueryRows:
    """Turn a Query API payload into totals keyed by resource and service."""
    out = QueryRows()
    properties = payload.get("properties") if isinstance(payload, dict) else None
    if not isinstance(properties, dict):
        return out
    columns = properties.get("columns")
    rows = properties.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return out
    index = _column_index(columns)
    cost_at = _cost_column(columns, index)
    if cost_at is None:
        return out
    out.cost_column_found = True
    resource_at = index.get("resourceid")
    service_at = index.get("servicename")
    currency_at = index.get("currency")
    if currency_at is None:
        currency_at = index.get("billingcurrency")
    date_at = index.get("usagedate")
    # Cost Management is a *lagging* source: a usage date past tomorrow is not
    # a fresh number, it is a malformed one, and "data as of" exists precisely
    # to stop stale data reading as fresh.
    latest_plausible = datetime.now(timezone.utc).date() + timedelta(days=1)
    currencies: set[str] = set()

    if len(rows) > MAX_QUERY_ROWS:
        out.truncated = True
    for row in rows[:MAX_QUERY_ROWS]:
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
            service = str(row[service_at] or "").strip()[:SERVICE_NAME_MAX_LEN]
        if currency_at is not None and currency_at < len(row):
            code = str(row[currency_at] or "").strip()[:CURRENCY_MAX_LEN]
            if code:
                currencies.add(code)
                if not out.currency:
                    out.currency = code
        if date_at is not None and date_at < len(row) and cost:
            # "Data as of" is the latest day that actually carries cost. A
            # trailing zero-cost day is the API padding the range, not evidence
            # that the day has been processed.
            day = _parse_usage_date(row[date_at])
            if (
                day is not None
                and day <= latest_plausible
                and (out.latest_usage_date is None or day > out.latest_usage_date)
            ):
                out.latest_usage_date = day
        if resource_id:
            out.by_resource[resource_id.lower()] = (
                out.by_resource.get(resource_id.lower(), 0.0) + cost
            )
        if service:
            out.by_service[service] = out.by_service.get(service, 0.0) + cost
        out.rows.append((resource_id.lower(), service, cost))
    if len(currencies) > 1:
        # Adding JPY to CAD produces a number that means nothing, and printing
        # it with whichever code came first is the confident wrong answer.
        out.mixed_currency = True
        out.currency = ""
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
    # More than one billing currency appeared in the rows.
    mixed_currency: bool = False
    # Whether the subscription's offer type was actually read. False means the
    # Sponsorship check could not run, which matters only when spend is zero.
    offer_known: bool = True
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
    # show, and a quiet month would otherwise hide it. Pinned on the bucket
    # name alone - a Foundry row that spent 0.00 is still that row, and a
    # quiet month is exactly what the pin is for.
    pinned = [pair for pair in ranked if pair[0] == FOUNDRY_BUCKET]
    rest = [pair for pair in ranked if pair not in pinned]
    top_rows = max(1, min(MAX_BREAKDOWN_ROWS, top_rows))
    keep = pinned + rest[: max(0, top_rows - len(pinned))]
    remainder = [pair for pair in rest if pair not in keep]
    if remainder:
        other_total = sum(cost for _, cost in remainder)
        # "services" only when they all are: the Marketplace row is a bucket,
        # not a service, and counting it as one mis-states what the row holds.
        non_services = (FOUNDRY_BUCKET, MARKETPLACE_BUCKET)
        noun = (
            "buckets"
            if any(name in non_services for name, _ in remainder)
            else "services"
        )
        keep.append((f"Other ({len(remainder)} {noun})", other_total))
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
        _boundary_local(aggregate.period_end) if aggregate.period_end else None
    )
    window = (
        timedelta(days=(aggregate.period_end - aggregate.period_start).days)
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

    # Reasons the number on this tile cannot honestly carry a percentage.
    # Each one is a case where a gauge would be confidently wrong rather than
    # visibly broken, which is the trade this whole module is built around.
    ungauged_note: str | None = None
    if aggregate.mixed_currency:
        ungauged_note = (
            "Cost Management returned more than one billing currency for this "
            "period, so the rows cannot be added together and no gauge is "
            "shown."
        )
    elif not aggregate.offer_known and aggregate.total <= 0:
        # Cost Management Reader without Reader is the likely role split, and
        # an Azure Sponsorship subscription reports exactly this: zero, while
        # the credit drains.
        ungauged_note = (
            "The subscription's offer type could not be read (Reader role), "
            "so an Azure Sponsorship offer - which Cost Management reports as "
            "zero cost while the credit drains - cannot be ruled out. No gauge "
            "is shown for a zero total."
        )
    offer_note: str | None = None
    if ungauged_note is None and not aggregate.offer_known:
        offer_note = (
            "The subscription's offer type could not be read (Reader role); "
            "the positive total rules out an unsupported Sponsorship offer."
        )
    spend_text = _money(aggregate.total, currency)
    if allowance and not aggregate.partial and ungauged_note is None:
        percent = max(0.0, min(100.0, aggregate.total / allowance * 100.0))
    else:
        percent = None
    # The label is a key, not a caption: history.py keys an in-flight period on
    # provider::label, so a label carrying the running total makes a new key on
    # every fetch - the rollover comparison never runs, no period is ever
    # closed, and current.json grows without bound. The money moves to
    # reset_label, which _MetricRow renders inline next to the bar, so it is
    # still on the collapsed row rather than hidden in a tooltip.
    label = "Spend this month"
    money_text = (
        f"{spend_text} of {allowance:,.2f}" if allowance else spend_text
    )
    if aggregate.period_end:
        end = aggregate.period_end
        reset_label = f"{money_text} · resets {end.day} {end.strftime('%b')}"
    else:
        reset_label = money_text

    note_parts = [_as_of_note(aggregate)]
    note_parts.append(
        "Costs are gross: Cost Management excludes free and prepaid credits, so "
        "this measures consumption against your stated allowance, not a live "
        "credit balance."
    )
    if ungauged_note:
        note_parts.append(ungauged_note)
    elif offer_note:
        note_parts.append(offer_note)
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
            reset_label=reset_label,
            note=" ".join(note_parts),
            window=window,
        )
    )

    total = aggregate.total
    for name, cost in aggregate.buckets:
        # Clamped for display only: a refund row makes a share negative and
        # pushes another over 100, and the row label prints the percentage
        # verbatim. The money stays exact in the note.
        share = (
            max(0.0, min(100.0, cost / total * 100.0)) if total > 0 else None
        )
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

    forecast_total = aggregate.forecast_total
    if forecast_total is not None and (
        forecast_total <= 0 or forecast_total < aggregate.total
    ):
        # A full-period projection at or below what has already been spent is
        # not a projection - it contradicts the row above it. The row is built
        # to come and go silently when Cost Management cannot produce one, so
        # omitting it is the honest outcome. (The number stays in the
        # diagnostics payload, which is where a bug report needs it.)
        forecast_total = None
    if forecast_total is not None:
        forecast_share = (
            max(0.0, min(100.0, forecast_total / allowance * 100.0))
            if allowance
            else None
        )
        metrics.append(
            UsageMetric(
                label="Forecast end of month",
                percent_used=forecast_share,
                note=(
                    f"~{_money(forecast_total, currency)} projected by "
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
    """401/403 from ARM: the app registration is missing a role.

    ``status`` distinguishes the two: 401 says the bearer is not acceptable,
    403 says this identity may not do this. Only the first invalidates a token.
    """

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


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
            + _PERMISSION_HINT,
            status=response.status_code,
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


@dataclass
class _RequestBudget:
    """A wall-clock and request-count ceiling for one refresh.

    It guards the two loops that can multiply - the cost-query ``nextLink``
    chain and the discovery page loop - because those are what turn a slow or
    hostile ARM into minutes of a shared ``QThreadPool`` thread. The fixed
    handful of calls around them (token, subscription, budgets, forecast) is
    deliberately outside it: they cannot repeat, so counting them would only
    make the ceiling harder to reason about.

    ``clock`` is injected so a test can exhaust the deadline without waiting.
    """

    deadline_seconds: float = REFRESH_DEADLINE_SECONDS
    max_requests: int = MAX_ARM_REQUESTS_PER_REFRESH
    clock: Callable[[], float] = time.monotonic
    used: int = 0
    exhausted: bool = False
    started_at: float = 0.0

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    def spend(self) -> bool:
        """Take one request from the budget; False when there is none left."""
        if self.used >= self.max_requests or (
            self.clock() - self.started_at > self.deadline_seconds
        ):
            self.exhausted = True
            return False
        self.used += 1
        return True


def _scope(subscription_id: str) -> str:
    # Re-validated here, at the point the id becomes a URL, and not only where
    # it was stored: AzureConfig coerces a bad id to None at load and the
    # settings dialog validates before assigning, but neither is in this call
    # path, and this is the last place that can still refuse. Same shape as
    # opencode_go.usage_url(), which re-runs its validator at the point of use.
    checked = validate_azure_guid(subscription_id, "subscription id")
    return f"{MANAGEMENT_HOST}/subscriptions/{checked}"


def _charge_type_filter() -> dict:
    """Usage only - the same clause on the query and the forecast.

    The two rows are compared on the tile, so they have to be measuring the
    same thing: a forecast that includes purchases and refunds is not a
    projection of the usage number above it.
    """
    return {
        "dimensions": {"name": "ChargeType", "operator": "In", "values": ["Usage"]}
    }


def _resource_group_filter(resource_group: str) -> dict:
    return {
        "dimensions": {
            "name": "ResourceGroupName",
            "operator": "In",
            "values": [resource_group],
        }
    }


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
    filters: list[dict] = [_charge_type_filter()]
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
        filters.append(_resource_group_filter(resource_group))
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
    if page.mixed_currency or (
        page.currency and into.currency and page.currency != into.currency
    ):
        into.mixed_currency = True
        into.currency = ""
    elif not into.currency and not into.mixed_currency:
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
    # Belt and braces: _follow_query_pages refuses an unreadable page before
    # it gets here, and any future caller gets the same answer.
    into.truncated = into.truncated or page.truncated or not page.cost_column_found
    if into.row_count > MAX_QUERY_ROWS:
        into.truncated = True


def _follow_query_pages(
    token: str,
    body: dict,
    payload: Any,
    parsed: QueryRows,
    budget: "_RequestBudget | None" = None,
) -> None:
    """Read the remaining pages of a query into ``parsed``.

    Cost Management continues a POST query by re-POSTing the same body to the
    ``nextLink`` it returned. Every way this loop can end early - a refused
    host, the page cap, a non-200, a body with no readable cost column, a link
    it has already followed, an exhausted request budget - marks the result
    truncated, because the alternative is a subtotal shown as a total.
    """
    pages = 1
    followed: set[str] = set()
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
        if url in followed:
            # A link back to a page already read double-counts every row on it.
            log.warning(
                "provider api diagnosis provider=azure "
                "classification=query_nextlink_repeated pages=%s",
                pages,
            )
            parsed.truncated = True
            return
        followed.add(url)
        if budget is not None and not budget.spend():
            log.warning(
                "provider api diagnosis provider=azure "
                "classification=query_budget_exhausted pages=%s",
                pages,
            )
            parsed.truncated = True
            return
        response = arm_post(token, url, body, "cost query")
        if response.status_code != 200:
            parsed.truncated = True
            return
        payload = _json(response)
        page = parse_query_response(payload)
        if not page.cost_column_found:
            # The same rule fetch_query applies to page 1: a 200 whose body is
            # an ARM error document, or a schema this parser cannot read, is a
            # page that was not read - not a page that held nothing.
            log.warning(
                "provider api diagnosis provider=azure "
                "classification=query_page_unreadable page=%s",
                pages + 1,
            )
            parsed.truncated = True
            return
        _merge_query_rows(parsed, page)
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
    budget: "_RequestBudget | None" = None,
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
            if not parsed.cost_column_found:
                # A 200 whose body is an ARM error document, or a schema the
                # parser cannot read. Either way it is not an empty month.
                raise requests.HTTPError(
                    "Cost Management returned no cost column"
                )
            _follow_query_pages(token, body, payload, parsed, budget)
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


def forecast_body(
    period_start: date,
    period_end: date,
    *,
    cost_metric: str,
    resource_group: str | None = None,
) -> dict:
    """Build the Forecast API request, filtered like the actual-cost query."""
    filters: list[dict] = [_charge_type_filter()]
    if resource_group:
        filters.append(_resource_group_filter(resource_group))
    return {
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
            "filter": filters[0] if len(filters) == 1 else {"and": filters},
        },
        # Actual-to-date plus projected-remainder, so the row is a full-period
        # number rather than only the unspent tail.
        "includeActualCost": True,
        "includeFreshPartialCost": False,
    }


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
    body = forecast_body(
        period_start,
        period_end,
        cost_metric=cost_metric,
        resource_group=resource_group,
    )
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


def _budget_currency(properties: dict) -> str:
    """The currency a Budget is denominated in.

    The Budget resource does not carry a currency field of its own; the unit
    rides on currentSpend, and on forecastSpend when the budget has not been
    spent against yet.
    """
    for key in ("currentSpend", "forecastSpend"):
        block = properties.get(key)
        if isinstance(block, dict):
            unit = str(block.get("unit") or "").strip()
            if unit:
                return unit
    return ""


def _budget_scope_matches(properties: dict, resource_group: str | None) -> bool:
    """Whether this budget measures the same thing the tile does.

    A subscription-scope budget routinely carries a dimension filter - one
    resource group, one service - and such a budget is *not* the whole
    subscription's allowance. The only filter accepted is one that names
    exactly the resource group the tile is already filtered to.
    """
    budget_filter = properties.get("filter")
    if not budget_filter:
        return True
    if not resource_group or not isinstance(budget_filter, dict):
        return False
    dimensions = budget_filter.get("dimensions")
    if not isinstance(dimensions, dict):
        return False
    if str(dimensions.get("name") or "").strip().lower() != "resourcegroupname":
        return False
    values = dimensions.get("values")
    if not isinstance(values, list) or len(values) != 1:
        return False
    return str(values[0]).strip().lower() == resource_group.strip().lower()


def fetch_budget(
    token: str,
    subscription_id: str,
    *,
    currency: str = "",
    resource_group: str | None = None,
) -> tuple[float | None, str | None, str | None]:
    """A real monthly cost Budget on the subscription, if one applies.

    Preferred over the settings allowance: if the owner already told Azure what
    the monthly number is, restating it in this app is a second copy to keep in
    sync. But it is only the right denominator when it measures the same money:
    a budget in another currency, or scoped to some other resource group, is
    refused rather than silently divided into a CAD total. Where several
    qualify the smallest wins - it is the one that alerts first, and the
    conservative choice. Not paged on purpose: a subscription with more monthly
    cost budgets than one page holds is outside this tile's design.

    Returns (amount, timeGrain, note).
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
        return None, None, None
    payload = _json(response)
    values = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return None, None, None

    note: str | None = None
    candidates: list[tuple[float, str]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        properties = item.get("properties")
        if not isinstance(properties, dict):
            continue
        if str(properties.get("category") or "").strip().lower() != "cost":
            continue
        grain = str(properties.get("timeGrain") or "").strip()
        if grain.lower() != "monthly":
            continue
        # _to_float, not float(): json.loads accepts Infinity and NaN, and
        # float("inf") > 0 is True - an infinite denominator reads as 0%.
        value = _to_float(properties.get("amount"))
        if value <= 0:
            continue
        if value > MAX_ALLOWANCE:
            note = (
                "An Azure Budget exists but its amount is implausibly large; "
                "using the allowance from Settings."
            )
            continue
        if not _budget_scope_matches(properties, resource_group):
            note = (
                "An Azure Budget exists but is scoped to something other than "
                "this subscription view; using the allowance from Settings."
            )
            continue
        budget_currency = _budget_currency(properties)
        if currency and budget_currency and budget_currency.lower() != currency.lower():
            note = (
                "An Azure Budget exists but is in a different currency from "
                "the cost data; using the allowance from Settings."
            )
            continue
        candidates.append((value, grain))
    if not candidates:
        return None, None, note
    amount, grain = min(candidates, key=lambda pair: pair[0])
    return amount, grain, None


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


def fetch_foundry_resource_ids(
    token: str,
    subscription_id: str,
    *,
    budget: "_RequestBudget | None" = None,
) -> set[str]:
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
        if budget is not None and not budget.spend():
            log.warning(
                "provider api diagnosis provider=azure "
                "classification=discovery_budget_exhausted pages=%s",
                pages,
            )
            break
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
    # What the *request* asks for, as opposed to who is asking. Kept apart
    # from ``identity`` because the two invalidate different things: see
    # _query_identity().
    query_identity: tuple | None = None
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


def _identity(tenant_id: str, client_id: str, client_secret: str) -> tuple:
    """*Who* is asking. A change here resets the whole _State.

    The secret is reduced to a truncated digest purely so that *rotating* it
    invalidates the cached backoff: without this, a user who fixed a wrong
    client secret would go on seeing the auth error until the backoff expired,
    which is exactly when they are looking at the tile to see whether the fix
    worked. An unchanged wrong secret still backs off, so a bad credential is
    not retried every five minutes against Entra ID.

    Everything cached under the old triple - the aggregate, the bearer, the
    throttle window, the error backoff - describes a different app
    registration, so all of it goes.
    """
    digest = hashlib.sha256(client_secret.encode("utf-8", "replace")).hexdigest()
    return (tenant_id, client_id, digest[:16])


def _query_identity(azure_cfg) -> tuple:
    """*What* the request asks for. A change here drops only the answer.

    These settings are consumed inside _fetch - which period, which scope,
    which queries, which buckets - so a cached aggregate built before the
    change answers a different question than the one the settings now ask, and
    the throttle would otherwise replay it for an hour.

    What it deliberately does **not** drop is ``last_fetch_at``,
    ``blocked_until`` and ``consecutive_errors``. Those are promises made to
    the *tenant*, not to this tile: README and SECURITY.md say at most one live
    fetch an hour, and a settings save is a one-click human action that is easy
    to loop. So the next render is the fail-closed "waiting for the next Azure
    fetch window" snapshot rather than a fresh query.

    ``top_rows`` is only a display setting, but re-bucketing it without a
    refetch would mean keeping every parsed row alongside the aggregate (up to
    MAX_QUERY_ROWS of them) for the life of the process, so it is treated like
    the rest: the answer is dropped and re-read at the next window.
    ``monthly_allowance`` is absent because build_snapshot re-reads it on every
    render, so editing it re-colours the gauge with no API call at all.
    """
    return (
        getattr(azure_cfg, "reset_day", 1),
        getattr(azure_cfg, "resource_group", None) or "",
        bool(getattr(azure_cfg, "include_marketplace", False)),
        getattr(azure_cfg, "top_rows", MAX_BREAKDOWN_ROWS),
        tuple(sorted(getattr(azure_cfg, "foundry_resource_ids", []) or [])),
    )


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
        shape = _query_identity(azure_cfg)
        if state.identity is not None and state.identity != identity:
            # Re-pointed at a different app registration: everything cached
            # here describes the old one, including the throttle window.
            log.info(
                "provider api diagnosis provider=azure "
                "classification=identity_changed cache_cleared=1"
            )
            # Including the bearer: it was minted for the old registration.
            clear_cache()
            state = _State()
            with _STATES_LOCK:
                _STATES[subscription_id] = state
        elif state.query_identity is not None and state.query_identity != shape:
            # The question changed, not the asker. Drop the answer; keep the
            # promise about how often this tile may ask.
            log.info(
                "provider api diagnosis provider=azure "
                "classification=query_settings_changed cache_dropped=1"
            )
            state.aggregate = None
            state.fetched_at = None
            state.last_error = None
        state.identity = identity
        state.query_identity = shape

        # Nothing but a finished worker clears in_flight, so a dispatch that
        # was lost - a pool torn down mid-shutdown, a worker killed - would
        # park the tile for the life of the process.
        if (
            state.in_flight
            and state.last_fetch_at is not None
            and now - state.last_fetch_at > IN_FLIGHT_STALE_AFTER
        ):
            log.warning(
                "provider api diagnosis provider=azure "
                "classification=in_flight_stale cleared=1"
            )
            state.in_flight = False

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
                # switches the throttle off. The type name only, and no
                # traceback: a message can carry the request URL, and with it
                # the subscription id, and this line goes to a log file the
                # error dialog invites the user to attach to a bug report.
                log.error(
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

        try:
            self._run_async(work, on_done)
        except Exception as exc:  # noqa: BLE001 - the dispatch failed, so
            # work() never ran and its finally never cleared the flag. Route
            # it through the same recording path a fetch failure takes, and
            # deliver a snapshot rather than raising into App's refresh loop.
            state.in_flight = False
            log.error(
                "provider api diagnosis provider=azure "
                "classification=dispatch_failed type=%s",
                type(exc).__name__,
            )
            on_done(
                self._remember_error(
                    state,
                    _error(f"Azure refresh failed ({type(exc).__name__})."),
                )
            )

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
            for field, value in (
                ("tenant id", tenant_id),
                ("client id", client_id),
                ("subscription id", subscription_id),
            ):
                validate_azure_guid(value, field)
        except ValueError:
            # Something bypassed the config validators. Nothing has been sent
            # yet and nothing will be: the URL builders refuse these too.
            log.warning(
                "provider api diagnosis provider=azure "
                "classification=invalid_id_refused"
            )
            return self._remember_error(
                state,
                _auth_required(
                    "Azure is not configured. Add the tenant, client, and "
                    "subscription ids in Settings → Microsoft → Azure."
                ),
            )

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
        budget = _RequestBudget()
        period_start, period_end = period_bounds(_utc_today(), azure_cfg.reset_day)

        try:
            # Discovery: near-static, so it is cached for a day and its failure
            # is a note rather than an error.
            if (
                state.discovery_at is None
                or now - state.discovery_at > DISCOVERY_TTL
            ):
                state.quota_id = self._safe_quota_id(token, subscription_id, notes)
                state.foundry_ids = self._safe_foundry_ids(
                    token, subscription_id, notes, budget=budget
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
                budget=budget,
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
                        budget=budget,
                    )
                    for rid, service, cost in mp_parsed.rows:
                        if not rid:
                            continue
                        key = (rid, service)
                        marketplace_costs[key] = (
                            marketplace_costs.get(key, 0.0) + cost
                        )
                    marketplace_cost = mp_parsed.total
                except AzureThrottled:
                    raise
                except AzurePermissionError:
                    # An extra row, not the tile: a 403 here used to discard a
                    # cost query that had already succeeded.
                    notes.append("Marketplace breakdown unavailable this refresh.")
                except Exception:  # noqa: BLE001 - tolerant by design.
                    notes.append("Marketplace breakdown unavailable this refresh.")

            buckets, foundry_cost, bucket_marketplace, service_count = bucket_costs(
                parsed,
                foundry_ids,
                marketplace_costs,
                top_rows=azure_cfg.top_rows,
            )

            budget_amount, budget_grain = self._safe_budget(
                token,
                subscription_id,
                notes,
                currency=parsed.currency,
                resource_group=azure_cfg.resource_group,
            )
            if budget_amount is not None and azure_cfg.reset_day != 1:
                # A Budget's Monthly grain is the calendar month. An
                # anniversary period measures a different window, so using it
                # would divide one window's spend by another window's budget.
                notes.append(
                    "An Azure Budget exists, but it covers the calendar month "
                    "rather than this allowance period; using the allowance "
                    "from Settings."
                )
                budget_amount, budget_grain = None, None
            forecast_total = self._safe_forecast(
                token,
                subscription_id,
                period_start,
                period_end,
                notes,
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
            if getattr(exc, "status", 0) == 401:
                invalidate(tenant_id, client_id)
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
            # Resources that actually spent, not resources discovered: "across
            # 5 Foundry resources" reading over a total that four of them
            # contributed nothing to is a count of the wrong thing.
            foundry_resource_count=sum(
                1 for rid in foundry_ids if parsed.by_resource.get(rid)
            ),
            marketplace_cost=(
                bucket_marketplace if bucket_marketplace is not None else marketplace_cost
            ),
            budget_amount=budget_amount,
            budget_time_grain=budget_grain,
            forecast_total=forecast_total,
            quota_id=state.quota_id,
            sponsorship=is_sponsorship(state.quota_id),
            mixed_currency=parsed.mixed_currency,
            offer_known=state.quota_id is not None,
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

    # Every one of these follows the same three-branch shape:
    #
    #   except AzureThrottled: raise      - the server's back-off is a fact
    #                                       about the whole tenant, not a
    #                                       detail of this sub-fetch, and the
    #                                       only place it is recorded is
    #                                       _fetch's handler.
    #   except AzurePermissionError:      - note and carry on: a missing role
    #                                       costs one row, never the tile.
    #   except Exception:                 - note and carry on.
    #
    # Widening the last branch without the first is what let a 429 on
    # discovery be swallowed while the refresh issued four more ARM requests
    # inside the window Azure had just asked us to stay out of.

    def _safe_quota_id(
        self, token: str, subscription_id: str, notes: list[str]
    ) -> str | None:
        try:
            return fetch_quota_id(token, subscription_id)
        except AzureThrottled:
            raise
        except AzurePermissionError:
            notes.append("Offer type unknown (Reader role missing).")
            return None
        except Exception:  # noqa: BLE001 - this layer is tolerant by design:
            # discovery failure is a note, never the reason the tile breaks.
            notes.append("The subscription's offer type could not be read.")
            return None

    def _safe_foundry_ids(
        self,
        token: str,
        subscription_id: str,
        notes: list[str],
        *,
        budget: "_RequestBudget | None" = None,
    ) -> set[str]:
        try:
            found = fetch_foundry_resource_ids(
                token, subscription_id, budget=budget
            )
        except AzureThrottled:
            raise
        except AzurePermissionError:
            notes.append(
                "Cannot list Foundry resources (Reader role missing); pin their "
                "resource ids under Settings → Microsoft → Foundry."
            )
            return set()
        except Exception:  # noqa: BLE001 - tolerant by design; see above.
            notes.append("Foundry resources could not be listed this refresh.")
            return set()
        if budget is not None and budget.exhausted:
            notes.append(
                "Foundry discovery stopped at this refresh's request budget."
            )
        return found

    def _safe_budget(
        self,
        token: str,
        subscription_id: str,
        notes: list[str],
        *,
        currency: str,
        resource_group: str | None,
    ) -> tuple[float | None, str | None]:
        try:
            amount, grain, note = fetch_budget(
                token,
                subscription_id,
                currency=currency,
                resource_group=resource_group,
            )
        except AzureThrottled:
            raise
        except AzurePermissionError:
            notes.append("Azure Budgets could not be read (role missing).")
            return None, None
        except Exception:  # noqa: BLE001 - tolerant by design; see above.
            notes.append("Azure Budgets were unavailable this refresh.")
            return None, None
        if note:
            notes.append(note)
        return amount, grain

    def _safe_forecast(
        self,
        token: str,
        subscription_id: str,
        period_start: date,
        period_end: date,
        notes: list[str],
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
        except AzureThrottled:
            raise
        except AzurePermissionError:
            notes.append("The forecast could not be read (role missing).")
            return None
        except Exception:  # noqa: BLE001 - tolerant by design; see above.
            notes.append("The forecast was unavailable this refresh.")
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
                    # No traceback: see work()'s handler.
                    log.error(
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
    start, end = period_bounds(_utc_today(), azure_cfg.reset_day)
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
        amount, grain, _note = fetch_budget(
            token,
            subscription_id,
            currency=parsed.currency,
            resource_group=azure_cfg.resource_group,
        )
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
