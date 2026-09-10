"""Azure month-to-date spend provider.

Fixtures are shaped exactly like the documented REST responses - a
``properties.columns`` array of {name,type} and a ``properties.rows`` array of
positional lists - because the whole parser is positional lookups against that
column map. A fixture written as a convenient dict would test nothing.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest
import requests
import responses

from aigauge.config import AzureConfig, Config
from aigauge.models import SnapshotStatus
from aigauge.providers import azure as az
from aigauge.providers._azure_auth import AzureAuthError, clear_cache, get_token
from aigauge.providers.openrouter import MODEL_BREAKDOWN_TAG

SUB = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"
FOUNDRY_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
    "Microsoft.CognitiveServices/accounts/my-foundry"
)
OPENAI_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
    "Microsoft.CognitiveServices/accounts/my-openai"
)
STORAGE_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-app/providers/"
    "Microsoft.Storage/storageAccounts/blobs"
)
MARKET_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
    "Microsoft.SaaS/resources/llama-marketplace"
)

QUERY_URL = (
    f"https://management.azure.com/subscriptions/{SUB}"
    "/providers/Microsoft.CostManagement/query"
)
FORECAST_URL = (
    f"https://management.azure.com/subscriptions/{SUB}"
    "/providers/Microsoft.CostManagement/forecast"
)
BUDGETS_URL = (
    f"https://management.azure.com/subscriptions/{SUB}"
    "/providers/Microsoft.Consumption/budgets"
)
ACCOUNTS_URL = (
    f"https://management.azure.com/subscriptions/{SUB}"
    "/providers/Microsoft.CognitiveServices/accounts"
)
SUBSCRIPTION_URL = f"https://management.azure.com/subscriptions/{SUB}"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"


def query_payload(rows, *, cost_name="Cost", currency="CAD"):
    """A Query API response in the documented shape."""
    return {
        "id": f"/subscriptions/{SUB}/providers/Microsoft.CostManagement/Query/x",
        "name": "x",
        "type": "microsoft.costmanagement/Query",
        "properties": {
            "nextLink": None,
            "columns": [
                {"name": cost_name, "type": "Number"},
                {"name": "UsageDate", "type": "Number"},
                {"name": "ResourceId", "type": "String"},
                {"name": "ServiceName", "type": "String"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": rows,
        },
    }


DEFAULT_ROWS = [
    [12.40, 20260908, FOUNDRY_ID, "Foundry Tools", "CAD"],
    [8.05, 20260907, OPENAI_ID, "Azure OpenAI", "CAD"],
    [6.90, 20260906, STORAGE_ID, "Azure Container Apps", "CAD"],
    [1.20, 20260905, STORAGE_ID, "Storage", "CAD"],
]


@pytest.fixture(autouse=True)
def _clean_state():
    az.reset_states()
    clear_cache()
    yield
    az.reset_states()
    clear_cache()


@pytest.fixture
def config():
    cfg = Config()
    cfg.azure = AzureConfig(
        tenant_id=TENANT,
        client_id=CLIENT,
        subscription_id=SUB,
        monthly_allowance=150.0,
    )
    return cfg


# --- period arithmetic -----------------------------------------------------


def test_period_bounds_calendar_month_when_reset_day_is_one():
    assert az.period_bounds(date(2026, 9, 9), 1) == (date(2026, 9, 1), date(2026, 10, 1))


def test_period_bounds_uses_configured_anniversary_not_calendar_month():
    """The whole reason timeframe is Custom rather than MonthToDate."""
    assert az.period_bounds(date(2026, 9, 9), 15) == (
        date(2026, 8, 15),
        date(2026, 9, 15),
    )
    assert az.period_bounds(date(2026, 9, 20), 15) == (
        date(2026, 9, 15),
        date(2026, 10, 15),
    )


def test_period_bounds_rolls_year_backwards_and_forwards():
    assert az.period_bounds(date(2026, 1, 3), 15) == (date(2025, 12, 15), date(2026, 1, 15))
    assert az.period_bounds(date(2026, 12, 20), 15) == (
        date(2026, 12, 15),
        date(2027, 1, 15),
    )


def test_period_bounds_clamps_a_day_that_february_does_not_have():
    start, end = az.period_bounds(date(2026, 3, 5), 31)
    assert start == date(2026, 2, 28)
    assert end == date(2026, 3, 28)


# --- response parsing ------------------------------------------------------


def test_parse_query_response_reads_columns_positionally():
    parsed = az.parse_query_response(query_payload(DEFAULT_ROWS))
    assert parsed.total == pytest.approx(28.55)
    assert parsed.currency == "CAD"
    assert parsed.row_count == 4
    assert parsed.by_service["Azure OpenAI"] == pytest.approx(8.05)


def test_parse_query_response_survives_reordered_columns():
    """Column order is not contractual and has changed between api-versions."""
    payload = {
        "properties": {
            "columns": [
                {"name": "ServiceName", "type": "String"},
                {"name": "Currency", "type": "String"},
                {"name": "PreTaxCost", "type": "Number"},
                {"name": "ResourceId", "type": "String"},
                {"name": "UsageDate", "type": "Number"},
            ],
            "rows": [["Storage", "EUR", 5.5, STORAGE_ID, 20260901]],
        }
    }
    parsed = az.parse_query_response(payload)
    assert parsed.total == pytest.approx(5.5)
    assert parsed.currency == "EUR"
    assert parsed.latest_usage_date == date(2026, 9, 1)


def test_currency_is_taken_from_the_response_never_assumed():
    parsed = az.parse_query_response(
        query_payload([[10.0, 20260901, STORAGE_ID, "Storage", "JPY"]])
    )
    assert parsed.currency == "JPY"
    snapshot = az.build_snapshot(
        az.AzureAggregate(total=10.0, currency=parsed.currency, buckets=[]),
        AzureConfig(monthly_allowance=100.0),
    )
    row = snapshot.metrics[0].reset_label or ""
    assert "JPY 10.00" in row
    assert "$" not in row


def test_data_as_of_is_the_latest_day_that_actually_has_cost():
    """A trailing zero-cost day is the API padding the range, not a processed day."""
    rows = [
        [4.0, 20260905, STORAGE_ID, "Storage", "CAD"],
        [0.0, 20260909, STORAGE_ID, "Storage", "CAD"],
    ]
    parsed = az.parse_query_response(query_payload(rows))
    assert parsed.latest_usage_date == date(2026, 9, 5)


def test_parse_usage_date_accepts_both_documented_shapes():
    assert az._parse_usage_date(20260908) == date(2026, 9, 8)
    assert az._parse_usage_date("2026-09-08T00:00:00Z") == date(2026, 9, 8)
    assert az._parse_usage_date("nonsense") is None
    assert az._parse_usage_date(None) is None


def test_cost_column_falls_back_to_a_column_that_names_itself_a_cost():
    """The candidate list covers every documented name; the fallback only has
    to survive a rename, so it matches on the name and never on position."""
    columns = [
        {"name": "UsageDate", "type": "Number"},
        {"name": "SomeFutureBilledCost", "type": "Number"},
    ]
    assert az._cost_column(columns, az._column_index(columns)) == 1


# --- bucketing -------------------------------------------------------------


def test_buckets_partition_rows_so_foundry_cannot_double_count():
    parsed = az.parse_query_response(query_payload(DEFAULT_ROWS))
    buckets, foundry, _marketplace, services = az.bucket_costs(
        parsed, {FOUNDRY_ID.lower()}, set()
    )
    assert foundry == pytest.approx(12.40)
    assert sum(cost for _name, cost in buckets) == pytest.approx(parsed.total)
    names = [name for name, _cost in buckets]
    assert names[0] == az.FOUNDRY_BUCKET
    # The Foundry resource's spend is NOT also present under its ServiceName.
    assert "Foundry Tools" not in names
    assert services == 3


def test_foundry_row_is_keyed_on_resource_id_not_resource_type():
    """Azure OpenAI shares Microsoft.CognitiveServices/accounts with Foundry."""
    parsed = az.parse_query_response(query_payload(DEFAULT_ROWS))
    buckets, foundry, _mp, _services = az.bucket_costs(
        parsed, {FOUNDRY_ID.lower()}, set()
    )
    assert foundry == pytest.approx(12.40)
    assert ("Azure OpenAI", pytest.approx(8.05)) in [
        (name, pytest.approx(cost)) for name, cost in buckets
    ]


def test_foundry_roll_up_is_none_when_no_foundry_resource_is_known():
    parsed = az.parse_query_response(query_payload(DEFAULT_ROWS))
    _buckets, foundry, _mp, _services = az.bucket_costs(parsed, set(), set())
    assert foundry is None


def test_remainder_collapses_into_an_other_row():
    rows = [
        [float(10 - i), 20260901, f"{STORAGE_ID}-{i}", f"Service {i}", "CAD"]
        for i in range(9)
    ]
    parsed = az.parse_query_response(query_payload(rows))
    buckets, _foundry, _mp, services = az.bucket_costs(parsed, set(), set())
    assert len(buckets) == az.MAX_BREAKDOWN_ROWS + 1
    assert buckets[-1][0] == "Other (3 services)"
    assert sum(cost for _n, cost in buckets) == pytest.approx(parsed.total)
    assert services == 9


def test_foundry_is_never_folded_into_other_even_in_a_quiet_month():
    rows = [[100.0, 20260901, f"{STORAGE_ID}-{i}", f"Service {i}", "CAD"] for i in range(8)]
    rows.append([0.01, 20260901, FOUNDRY_ID, "Foundry Tools", "CAD"])
    parsed = az.parse_query_response(query_payload(rows))
    buckets, _foundry, _mp, _services = az.bucket_costs(parsed, {FOUNDRY_ID.lower()}, set())
    assert buckets[0][0] == az.FOUNDRY_BUCKET
    assert len(buckets) == az.MAX_BREAKDOWN_ROWS + 1


def test_marketplace_rows_are_reclassified_not_added():
    """Marketplace usage is already inside the primary total, so it must move
    buckets rather than be summed on top of one."""
    rows = DEFAULT_ROWS + [[4.10, 20260908, MARKET_ID, "Global resources", "CAD"]]
    parsed = az.parse_query_response(query_payload(rows))
    buckets, _foundry, marketplace, _services = az.bucket_costs(
        parsed, {FOUNDRY_ID.lower()}, {(MARKET_ID.lower(), "Global resources"): 4.10}
    )
    assert marketplace == pytest.approx(4.10)
    assert sum(cost for _n, cost in buckets) == pytest.approx(parsed.total)
    assert az.MARKETPLACE_BUCKET in [name for name, _c in buckets]


def test_top_rows_setting_narrows_the_breakdown():
    rows = [
        [float(10 - i), 20260901, f"{STORAGE_ID}-{i}", f"Service {i}", "CAD"]
        for i in range(5)
    ]
    parsed = az.parse_query_response(query_payload(rows))
    buckets, _f, _m, _s = az.bucket_costs(parsed, set(), set(), top_rows=2)
    assert [name for name, _c in buckets] == ["Service 0", "Service 1", "Other (3 services)"]


def test_the_kept_buckets_are_capped_and_still_sum_to_the_total():
    """The aggregate keeps every distinct bucket so the row count can be
    re-sliced from it, which makes that list something a response can grow.
    The cap is on distinct services rather than rows, and whatever is past it
    is folded rather than dropped, so the rows still add up."""
    ranked = [(f"Service {i}", float(300 - i)) for i in range(300)]
    total = sum(cost for _, cost in ranked)

    kept = az.fold_buckets(ranked, az.MAX_KEPT_BUCKETS, cap=az.MAX_KEPT_BUCKETS)
    assert len(kept) == az.MAX_KEPT_BUCKETS + 1
    assert sum(cost for _, cost in kept) == pytest.approx(total)

    shown = az.fold_buckets(kept, 3)
    assert len(shown) == 4
    assert sum(cost for _, cost in shown) == pytest.approx(total)


def test_a_folded_row_is_counted_as_a_bucket_when_it_folds_again():
    """The fetch-time fold can leave an Other row in the list the render-time
    fold re-slices. It is an aggregation, not a service, so the noun says so
    rather than counting a hundred services as one."""
    folded = az.fold_buckets([("Other (9 services)", 1.0), ("A", 5.0), ("B", 4.0)], 1)
    assert folded == [("A", 5.0), ("Other (2 buckets)", 5.0)]


# --- snapshot --------------------------------------------------------------


def _aggregate(**overrides):
    base = dict(
        total=36.10,
        currency="CAD",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 10, 1),
        data_as_of=date(2026, 9, 8),
        buckets=[("Foundry", 12.40), ("Azure OpenAI", 8.05)],
        foundry_cost=12.40,
        foundry_resource_count=1,
    )
    base.update(overrides)
    return az.AzureAggregate(**base)


def test_only_the_budget_row_can_drive_the_tray_colour():
    from aigauge.gauge import provider_max_percent

    snapshot = az.build_snapshot(
        _aggregate(buckets=[("Foundry", 35.0), ("Storage", 1.10)]),
        AzureConfig(monthly_allowance=150.0),
    )
    breakdown = [m for m in snapshot.metrics if m.tag == az.BREAKDOWN_TAG]
    assert breakdown, "expected tagged breakdown rows"
    assert all(m.percent_used is not None for m in breakdown)
    # A single service at 96% of spend must not read as 96% of a quota.
    assert provider_max_percent(snapshot) == pytest.approx(24.07, abs=0.01)


def test_breakdown_tag_matches_openrouters_literal():
    """history.py, gauge.py and the menu-bar dot all filter on the string."""
    assert az.BREAKDOWN_TAG == MODEL_BREAKDOWN_TAG == "model_breakdown"


def test_summary_row_carries_reset_and_window():
    snapshot = az.build_snapshot(_aggregate(), AzureConfig(monthly_allowance=150.0))
    summary = snapshot.metrics[0]
    assert summary.resets_at == datetime(
        2026, 10, 1, tzinfo=timezone.utc
    ).astimezone().replace(tzinfo=None)
    assert summary.window == timedelta(days=30)
    assert summary.tag is None


def test_summary_row_states_the_credit_and_latency_caveats():
    snapshot = az.build_snapshot(_aggregate(), AzureConfig(monthly_allowance=150.0))
    note = snapshot.metrics[0].note or ""
    assert "gross" in note
    assert "2026-09-08" in note
    assert "8-24 h" in note


def test_no_allowance_means_no_gauge_but_still_a_number():
    snapshot = az.build_snapshot(_aggregate(), AzureConfig())
    summary = snapshot.metrics[0]
    assert summary.percent_used is None
    assert "CAD 36.10" in (summary.reset_label or "")
    assert "Set a monthly allowance" in (summary.note or "")


def test_a_typed_allowance_is_always_the_denominator():
    """"Smallest qualifying budget wins" is the right rule between budgets. It
    is the wrong rule against a number the user typed by hand: a 1.00 alert
    canary or a per-team budget they may not even own would take the tile
    over, and the tray dot with it."""
    snapshot = az.build_snapshot(
        _aggregate(budget_amount=200.0, budget_time_grain="Monthly"),
        AzureConfig(monthly_allowance=150.0),
    )
    assert snapshot.metrics[0].percent_used == pytest.approx(24.07, abs=0.01)
    note = snapshot.metrics[0].note or ""
    assert "Azure Budget of CAD 200.00" in note
    assert "Settings" in note


def test_a_budget_is_the_denominator_when_nothing_is_typed():
    snapshot = az.build_snapshot(
        _aggregate(budget_amount=200.0, budget_time_grain="Monthly"),
        AzureConfig(monthly_allowance=0.0),
    )
    assert snapshot.metrics[0].percent_used == pytest.approx(18.05, abs=0.01)
    assert "Allowance read from an Azure Budget" in (snapshot.metrics[0].note or "")


def test_settings_allowance_is_used_when_there_is_no_budget():
    snapshot = az.build_snapshot(_aggregate(), AzureConfig(monthly_allowance=150.0))
    assert snapshot.metrics[0].percent_used == pytest.approx(24.07, abs=0.01)


def test_sponsorship_replaces_the_gauge_with_a_warning():
    """Cost Management reports 0 for sponsorship offers while the credit
    drains, so a reassuring 0% gauge is the wrong output."""
    snapshot = az.build_snapshot(
        _aggregate(total=0.0, quota_id="Sponsored_2016-01-01", sponsorship=True),
        AzureConfig(monthly_allowance=150.0),
    )
    assert snapshot.metrics[0].label.startswith("Sponsorship offer")
    assert all(m.percent_used is None for m in snapshot.metrics if m.tag is None)


def test_a_sponsorship_offer_leaves_no_percentage_anywhere_on_the_tile():
    """The warning row says Cost Management does not report this spend at all.
    A share of that total, or a forecast projected from it, is the same
    unreported number one row further down, so sponsorship suppresses every
    percentage exactly as a truncated read or an unreadable offer type does."""
    snapshot = az.build_snapshot(
        _aggregate(
            total=6.75,
            quota_id="Sponsored_2016-01-01",
            sponsorship=True,
            buckets=[("Foundry", 4.50), ("Marketplace models", 2.25)],
            forecast_total=42.0,
        ),
        AzureConfig(monthly_allowance=100.0),
    )
    assert all(m.percent_used is None for m in snapshot.metrics)
    assert not any(m.label == "Forecast end of month" for m in snapshot.metrics)
    # The money is still reported; only the percentages are refused.
    assert "CAD 6.75" in (snapshot.metrics[1].reset_label or "")
    assert "CAD 4.50" in (snapshot.metrics[2].note or "")


@pytest.mark.parametrize(
    "quota_id,expected",
    [
        ("Sponsored_2016-01-01", True),
        ("sponsored_2016-01-01", True),
        ("EnterpriseAgreement_2014-09-01", False),
        ("MSDN_2014-09-01", False),
        (None, False),
    ],
)
def test_sponsorship_detection(quota_id, expected):
    assert az.is_sponsorship(quota_id) is expected


def test_forecast_row_is_tagged_and_optional():
    with_forecast = az.build_snapshot(
        _aggregate(forecast_total=71.0), AzureConfig(monthly_allowance=150.0)
    )
    row = with_forecast.metrics[-1]
    assert row.label == "Forecast end of month"
    assert row.tag == az.BREAKDOWN_TAG
    assert row.percent_used == pytest.approx(47.33, abs=0.01)

    without = az.build_snapshot(_aggregate(), AzureConfig(monthly_allowance=150.0))
    assert not any(m.label == "Forecast end of month" for m in without.metrics)


def test_long_service_names_are_truncated_with_the_full_name_in_the_note():
    snapshot = az.build_snapshot(
        _aggregate(buckets=[("Azure Database for PostgreSQL flexible server", 5.0)]),
        AzureConfig(monthly_allowance=150.0),
    )
    row = snapshot.metrics[-1]
    assert len(row.label) == az.LABEL_MAX_LEN
    assert row.label.endswith("…")
    assert "Azure Database for PostgreSQL flexible server" in (row.note or "")


def test_diagnostic_payload_carries_no_ids_or_resource_names():
    snapshot = az.build_snapshot(_aggregate(), AzureConfig(monthly_allowance=150.0))
    blob = repr(snapshot.raw)
    assert SUB not in blob
    assert "resourceGroups" not in blob
    assert "my-foundry" not in blob


# --- request shape ---------------------------------------------------------


def test_query_body_matches_the_documented_request():
    body = az.query_body(date(2026, 9, 1), date(2026, 10, 1), cost_metric="Cost")
    assert body["type"] == "ActualCost"
    # Custom, not MonthToDate: the allowance period is an anniversary.
    assert body["timeframe"] == "Custom"
    assert body["timePeriod"]["from"].startswith("2026-09-01")
    assert body["timePeriod"]["to"].startswith("2026-09-30")
    dataset = body["dataset"]
    assert dataset["granularity"] == "Daily"
    assert dataset["aggregation"] == {"totalCost": {"name": "Cost", "function": "Sum"}}
    # The Query API caps grouping at two dimensions.
    assert len(dataset["grouping"]) == 2
    assert [g["name"] for g in dataset["grouping"]] == ["ResourceId", "ServiceName"]
    assert dataset["filter"]["dimensions"]["name"] == "ChargeType"
    assert dataset["filter"]["dimensions"]["values"] == ["Usage"]


def test_query_body_ands_extra_filters():
    body = az.query_body(
        date(2026, 9, 1),
        date(2026, 10, 1),
        cost_metric="Cost",
        resource_group="rg-ai",
        marketplace_only=True,
    )
    clauses = body["dataset"]["filter"]["and"]
    names = [c["dimensions"]["name"] for c in clauses]
    assert names == ["ChargeType", "PublisherType", "ResourceGroupName"]


@responses.activate
def test_query_retries_once_with_the_other_cost_metric_name():
    """MCA calls it Cost; EA and pay-as-you-go call it PreTaxCost."""
    responses.add(responses.POST, QUERY_URL, json={"error": "bad metric"}, status=400)
    responses.add(
        responses.POST,
        QUERY_URL,
        json=query_payload(DEFAULT_ROWS, cost_name="PreTaxCost"),
        status=200,
    )
    parsed, metric = az.fetch_query("t", SUB, date(2026, 9, 1), date(2026, 10, 1))
    assert metric == "PreTaxCost"
    assert parsed.total == pytest.approx(28.55)
    assert len(responses.calls) == 2


@responses.activate
def test_query_sends_the_clienttype_header():
    responses.add(responses.POST, QUERY_URL, json=query_payload([]), status=200)
    az.fetch_query("tok", SUB, date(2026, 9, 1), date(2026, 10, 1))
    request = responses.calls[0].request
    assert request.headers["ClientType"] == az.CLIENT_TYPE
    assert request.headers["Authorization"] == "Bearer tok"
    assert f"api-version={az.COST_MANAGEMENT_API_VERSION}" in request.url


@responses.activate
def test_foundry_discovery_selects_only_aiservices_kind():
    responses.add(
        responses.GET,
        ACCOUNTS_URL,
        json={
            "value": [
                {"id": FOUNDRY_ID, "kind": "AIServices"},
                {"id": OPENAI_ID, "kind": "OpenAI"},
                {"id": f"{OPENAI_ID}-speech", "kind": "SpeechServices"},
            ]
        },
        status=200,
    )
    found = az.fetch_foundry_resource_ids("tok", SUB)
    assert found == {FOUNDRY_ID.lower()}


@responses.activate
def test_budget_prefers_a_monthly_cost_budget():
    responses.add(
        responses.GET,
        BUDGETS_URL,
        json={
            "value": [
                {"properties": {"category": "Usage", "timeGrain": "Monthly", "amount": 9}},
                {"properties": {"category": "Cost", "timeGrain": "Annual", "amount": 900}},
                {
                    "properties": {
                        "category": "Cost",
                        "timeGrain": "Monthly",
                        "amount": 250.0,
                        "currentSpend": {"amount": 30.0, "unit": "CAD"},
                    }
                },
            ]
        },
        status=200,
    )
    amount, grain, note = az.fetch_budget("tok", SUB, currency="CAD")
    assert amount == pytest.approx(250.0)
    assert grain == "Monthly"
    assert note is None


@responses.activate
def test_forecast_is_omitted_on_4xx():
    responses.add(responses.POST, FORECAST_URL, json={"error": "no history"}, status=400)
    assert (
        az.fetch_forecast("tok", SUB, date(2026, 9, 1), date(2026, 10, 1), cost_metric="Cost")
        is None
    )


@responses.activate
def test_forecast_is_omitted_when_empty():
    responses.add(responses.POST, FORECAST_URL, json=query_payload([]), status=200)
    assert (
        az.fetch_forecast("tok", SUB, date(2026, 9, 1), date(2026, 10, 1), cost_metric="Cost")
        is None
    )


@responses.activate
def test_permission_error_names_the_roles_to_grant():
    responses.add(responses.POST, QUERY_URL, json={}, status=403)
    with pytest.raises(az.AzurePermissionError) as exc:
        az.fetch_query("tok", SUB, date(2026, 9, 1), date(2026, 10, 1))
    assert "Cost Management Reader" in str(exc.value)
    assert "Reader" in str(exc.value)


@responses.activate
def test_429_raises_with_the_servers_own_backoff():
    responses.add(
        responses.POST,
        QUERY_URL,
        json={},
        status=429,
        headers={"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "90"},
    )
    with pytest.raises(az.AzureThrottled) as exc:
        az.fetch_query("tok", SUB, date(2026, 9, 1), date(2026, 10, 1))
    assert exc.value.retry_after == 90


def test_retry_after_reads_the_whole_header_family():
    headers = {
        "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "30",
        "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after": "120",
        "Retry-After": "5",
    }
    assert az.retry_after_seconds(headers) == 120


def test_retry_after_falls_back_when_no_header_is_present():
    assert az.retry_after_seconds({}) == int(az.MIN_FETCH_INTERVAL.total_seconds())


def test_retry_after_is_capped():
    assert az.retry_after_seconds(
        {"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "999999"}
    ) == int(az.MAX_ERROR_BACKOFF.total_seconds())


# --- auth ------------------------------------------------------------------


@responses.activate
def test_token_is_cached_until_shortly_before_expiry():
    responses.add(
        responses.POST,
        TOKEN_URL,
        json={"access_token": "abc", "expires_in": 3600, "token_type": "Bearer"},
        status=200,
    )
    now = datetime(2026, 9, 9, 12, 0)
    assert get_token(TENANT, CLIENT, "secret", now=now) == "abc"
    assert get_token(TENANT, CLIENT, "secret", now=now + timedelta(minutes=50)) == "abc"
    assert len(responses.calls) == 1
    # 5 minutes before the hour is up, the cache is already cold.
    assert get_token(TENANT, CLIENT, "secret", now=now + timedelta(minutes=56)) == "abc"
    assert len(responses.calls) == 2
    body = responses.calls[0].request.body
    assert "grant_type=client_credentials" in body
    assert "scope=https%3A%2F%2Fmanagement.azure.com%2F.default" in body


@responses.activate
def test_token_error_does_not_leak_the_aad_description():
    responses.add(
        responses.POST,
        TOKEN_URL,
        json={
            "error": "invalid_client",
            "error_description": (
                f"AADSTS7000215: Invalid client secret for {CLIENT} in tenant {TENANT}"
            ),
        },
        status=401,
    )
    with pytest.raises(AzureAuthError) as exc:
        get_token(TENANT, CLIENT, "bad", now=datetime(2026, 9, 9))
    message = str(exc.value)
    assert "invalid_client" in message
    assert CLIENT not in message
    assert TENANT not in message
    assert "AADSTS" not in message


# --- provider end to end ---------------------------------------------------


def _stub_everything(*, rows=DEFAULT_ROWS, forecast=True):
    responses.add(
        responses.POST,
        TOKEN_URL,
        json={"access_token": "tok", "expires_in": 3600},
        status=200,
    )
    responses.add(
        responses.GET,
        SUBSCRIPTION_URL,
        json={"subscriptionPolicies": {"quotaId": "MSDN_2014-09-01"}},
        status=200,
    )
    responses.add(
        responses.GET,
        ACCOUNTS_URL,
        json={"value": [{"id": FOUNDRY_ID, "kind": "AIServices"}]},
        status=200,
    )
    responses.add(responses.POST, QUERY_URL, json=query_payload(rows), status=200)
    responses.add(responses.GET, BUDGETS_URL, json={"value": []}, status=200)
    responses.add(
        responses.POST,
        FORECAST_URL,
        json=query_payload([[71.0, 20260930, "", "", "CAD"]]) if forecast else {},
        status=200 if forecast else 400,
    )


def _run(provider, monkeypatch):
    captured: list = []
    monkeypatch.setattr(provider, "_run_async", lambda work, on_done: on_done(work()))
    provider.refresh(captured.append)
    return captured[0]


@responses.activate
def test_refresh_builds_the_full_tile(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    snapshot = _run(az.AzureProvider(config), monkeypatch)

    assert snapshot.status == SnapshotStatus.OK
    labels = [m.label for m in snapshot.metrics]
    assert labels[0].startswith("Spend this month")
    assert az.FOUNDRY_BUCKET in labels
    assert "Forecast end of month" in labels
    assert snapshot.raw["currency"] == "CAD"
    assert snapshot.raw["foundry_resource_count"] == 1


@responses.activate
def test_refresh_without_credentials_asks_for_them(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: None)
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert "client secret" in (snapshot.error or "")
    assert not responses.calls


@responses.activate
def test_refresh_without_ids_does_not_call_anything(monkeypatch):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    snapshot = _run(az.AzureProvider(Config()), monkeypatch)
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert not responses.calls


@responses.activate
def test_second_refresh_within_the_hour_serves_the_cache(monkeypatch, config):
    """The refresh loop above this can fire every minute; Cost Management
    quotas are shared tenant-wide."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    provider = az.AzureProvider(config)
    first = _run(provider, monkeypatch)
    calls_after_first = len(responses.calls)

    second = _run(provider, monkeypatch)
    assert len(responses.calls) == calls_after_first, "second fetch hit the network"
    assert second.status == SnapshotStatus.OK
    # Served from cache means the ORIGINAL fetch time, not "now".
    assert second.fetched_at == first.fetched_at


@responses.activate
def test_changing_the_row_count_re_renders_from_the_cache(monkeypatch, config):
    """"Top rows shown" changes nothing about the question asked, only how
    many of the answer's rows are named. Treating it like the rest of the
    query shape discarded a correct cached aggregate and left the tile reading
    `error - stale` until the hourly window opened, for a display edit."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    rows = [
        [float(10 - i), 20260901, f"{STORAGE_ID}-{i}", f"Service {i}", "CAD"]
        for i in range(6)
    ]
    _stub_everything(rows=rows, forecast=False)
    first = _run(az.AzureProvider(config), monkeypatch)
    calls = len(responses.calls)
    assert [m.label for m in first.metrics if m.tag == az.BREAKDOWN_TAG] == [
        f"Service {i}" for i in range(6)
    ]

    config.azure.top_rows = 3
    second = _run(az.AzureProvider(config), monkeypatch)

    assert len(responses.calls) == calls, "a display setting cost a live fetch"
    assert second.status == SnapshotStatus.OK
    assert [m.label for m in second.metrics if m.tag == az.BREAKDOWN_TAG] == [
        "Service 0",
        "Service 1",
        "Service 2",
        "Other (3 services)",
    ]
    assert second.metrics[0].percent_used is not None
    # The folded rows still add up: 7.00 + 6.00 + 5.00.
    assert second.metrics[-1].note and "CAD 18.00" in second.metrics[-1].note


@responses.activate
def test_throttle_survives_the_provider_rebuild_on_a_settings_save(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    _run(az.AzureProvider(config), monkeypatch)
    calls = len(responses.calls)
    # App rebuilds every provider when Settings is saved.
    _run(az.AzureProvider(config), monkeypatch)
    assert len(responses.calls) == calls


@responses.activate
def test_a_cached_snapshot_re_renders_with_a_new_allowance(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    provider = az.AzureProvider(config)
    first = _run(provider, monkeypatch)
    assert first.metrics[0].percent_used == pytest.approx(19.03, abs=0.01)

    config.azure.monthly_allowance = 50.0
    second = _run(provider, monkeypatch)
    assert second.metrics[0].percent_used == pytest.approx(57.10, abs=0.01)


@responses.activate
def test_429_serves_the_cache_and_records_the_backoff(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    provider = az.AzureProvider(config)
    _run(provider, monkeypatch)

    responses.reset()
    _stub_everything()
    responses.replace(
        responses.POST,
        QUERY_URL,
        json={},
        status=429,
        headers={"x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after": "600"},
    )
    state = az.state_for(SUB)
    state.last_fetch_at = datetime.now() - timedelta(hours=2)

    snapshot = _run(provider, monkeypatch)
    assert snapshot.status == SnapshotStatus.OK  # cache, not an error tile
    assert state.blocked_until is not None
    assert state.blocked_until > datetime.now() + timedelta(minutes=8)


@responses.activate
def test_fixing_a_wrong_secret_clears_the_backoff_immediately(monkeypatch, config):
    """The moment after a user fixes a bad secret is exactly when they are
    watching the tile to see whether the fix worked."""
    secret = {"value": "wrong"}
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: secret["value"])
    responses.add(responses.POST, TOKEN_URL, json={"error": "invalid_client"}, status=401)
    provider = az.AzureProvider(config)

    first = _run(provider, monkeypatch)
    assert first.status == SnapshotStatus.AUTH_REQUIRED
    state = az.state_for(SUB)
    assert state.blocked_until is not None

    # Same wrong secret: still backing off, so Entra ID is not hit again.
    calls = len(responses.calls)
    assert _run(provider, monkeypatch).status == SnapshotStatus.AUTH_REQUIRED
    assert len(responses.calls) == calls

    # Corrected secret: retried at once.
    secret["value"] = "right"
    responses.reset()
    _stub_everything()
    assert _run(provider, monkeypatch).status == SnapshotStatus.OK


@responses.activate
def test_changing_the_app_registration_discards_the_other_tenants_numbers(
    monkeypatch, config
):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    provider = az.AzureProvider(config)
    _run(provider, monkeypatch)
    assert az.state_for(SUB).aggregate is not None

    config.azure.tenant_id = "44444444-4444-4444-4444-444444444444"
    responses.reset()
    responses.add(
        responses.POST,
        f"https://login.microsoftonline.com/{config.azure.tenant_id}"
        "/oauth2/v2.0/token",
        json={"error": "invalid_client"},
        status=401,
    )
    snapshot = _run(provider, monkeypatch)
    # Not the cached OK snapshot from the previous tenant.
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED


@responses.activate
def test_permission_failure_becomes_auth_required_with_a_hint(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    responses.add(
        responses.POST, TOKEN_URL, json={"access_token": "tok", "expires_in": 3600}
    )
    responses.add(responses.GET, SUBSCRIPTION_URL, json={}, status=403)
    responses.add(responses.GET, ACCOUNTS_URL, json={}, status=403)
    responses.add(responses.POST, QUERY_URL, json={}, status=403)
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert "Cost Management Reader" in (snapshot.error or "")


@responses.activate
def test_bad_secret_becomes_auth_required(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "wrong")
    responses.add(responses.POST, TOKEN_URL, json={"error": "invalid_client"}, status=401)
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert "Entra ID" in (snapshot.error or "")


@responses.activate
def test_pinned_foundry_ids_replace_discovery(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.foundry_resource_ids = [OPENAI_ID]
    _stub_everything()
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    foundry = next(m for m in snapshot.metrics if m.label == az.FOUNDRY_BUCKET)
    # The pinned resource wins: the discovered one is not treated as Foundry.
    assert "8.05" in (foundry.note or "")


@responses.activate
def test_marketplace_query_only_runs_when_the_toggle_is_on(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    _run(az.AzureProvider(config), monkeypatch)
    query_calls = [c for c in responses.calls if "CostManagement/query" in c.request.url]
    assert len(query_calls) == 1

    az.reset_states()
    responses.reset()
    _stub_everything()
    responses.add(responses.POST, QUERY_URL, json=query_payload([]), status=200)
    config.azure.include_marketplace = True
    _run(az.AzureProvider(config), monkeypatch)
    query_calls = [c for c in responses.calls if "CostManagement/query" in c.request.url]
    assert len(query_calls) == 2


@responses.activate
def test_sponsorship_subscription_gets_a_warning_not_a_zero_gauge(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything(rows=[])
    responses.replace(
        responses.GET,
        SUBSCRIPTION_URL,
        json={"subscriptionPolicies": {"quotaId": "Sponsored_2016-01-01"}},
        status=200,
    )
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.metrics[0].label.startswith("Sponsorship offer")
    assert snapshot.raw["sponsorship"] is True


@responses.activate
def test_egress_stays_on_the_two_declared_hosts(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.include_marketplace = True
    _stub_everything()
    responses.add(responses.POST, QUERY_URL, json=query_payload([]), status=200)
    _run(az.AzureProvider(config), monkeypatch)
    hosts = {requests.utils.urlparse(c.request.url).hostname for c in responses.calls}
    assert hosts <= {"login.microsoftonline.com", "management.azure.com"}


# --- the throttle must never fail open -------------------------------------
#
# The hourly floor is the module's single safety property: Cost Management
# quotas are tenant-wide, and the refresh loop above this fires every minute
# while any provider is erroring. Every test here is about what happens when a
# response is not the shape the code expected.


class _InlinePool:
    """A QThreadPool double that runs the runnable on this thread.

    ``_run`` monkeypatches ``_run_async`` away, so the real ``_Worker.run`` -
    including its blanket handler - is otherwise never executed by the suite.
    """

    def __init__(self):
        self.started = 0

    def start(self, runnable):
        self.started += 1
        runnable.run()


def _run_through_pool(provider):
    captured: list = []
    provider.refresh(captured.append)
    return captured[0]


@pytest.mark.parametrize(
    "raw", ["inf", "Infinity", "1e400", "9" * 400, "nan", "-inf", "-Infinity"]
)
def test_retry_after_ignores_non_finite_headers(raw):
    """int(float("inf")) raises OverflowError, which used to escape the 429
    handler entirely and take the whole back-off with it."""
    assert az.retry_after_seconds({"Retry-After": raw}) == int(
        az.MIN_FETCH_INTERVAL.total_seconds()
    )


@responses.activate
def test_a_429_with_a_hostile_retry_after_still_records_a_backoff(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    responses.replace(
        responses.POST,
        QUERY_URL,
        json={},
        status=429,
        headers={"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "inf"},
    )
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.ERROR
    state = az.state_for(SUB)
    assert state.blocked_until is not None, "the 429 back-off was thrown away"
    assert state.last_error is not None


@responses.activate
def test_an_unexpected_exception_is_recorded_and_holds_the_hourly_floor(
    monkeypatch, config
):
    """A non-string nextLink used to raise past _fetch, leaving _State blank -
    and the gate only engages when the state is *not* blank."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    responses.replace(
        responses.GET,
        ACCOUNTS_URL,
        json={"value": [], "nextLink": {"href": "x"}},
        status=200,
    )
    pool = _InlinePool()
    provider = az.AzureProvider(config, pool=pool)

    _run_through_pool(provider)
    calls = len(responses.calls)
    state = az.state_for(SUB)
    assert state.aggregate is not None or state.last_error is not None

    second = _run_through_pool(provider)
    assert len(responses.calls) == calls, "second refresh went back to the network"
    assert second is not None
    assert pool.started == 1, "a second work item was dispatched inside the window"


@responses.activate
def test_a_fetch_already_in_flight_dispatches_nothing_more(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()

    dispatched: list = []

    class _HoldingPool:
        def start(self, runnable):
            dispatched.append(runnable)

    provider = az.AzureProvider(config, pool=_HoldingPool())
    provider.refresh(lambda snap: None)
    provider.refresh(lambda snap: None)
    assert len(dispatched) == 1, "two live fetches were dispatched at once"


@responses.activate
def test_a_last_fetch_at_in_the_future_does_not_freeze_the_tile(monkeypatch, config):
    """A bad RTC or an NTP correction must not park the tile for ten years."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    state = az.state_for(SUB)
    state.last_fetch_at = datetime.now() + timedelta(days=3650)
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.OK
    assert az.state_for(SUB).last_fetch_at < datetime.now() + timedelta(minutes=1)


@responses.activate
def test_the_throttle_gate_fails_closed_with_nothing_to_serve(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    state = az.state_for(SUB)
    state.blocked_until = datetime.now() + timedelta(minutes=42)

    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.ERROR
    assert "window" in (snapshot.error or "")
    assert not responses.calls, "the gate fell through to a live fetch"


@pytest.mark.parametrize("bad", [{"a": 1}, 12345, True, ["x"]])
@responses.activate
def test_a_non_string_nextlink_is_not_followed_and_does_not_raise(bad):
    responses.add(
        responses.GET,
        ACCOUNTS_URL,
        json={"value": [{"id": FOUNDRY_ID, "kind": "AIServices"}], "nextLink": bad},
        status=200,
    )
    assert az.fetch_foundry_resource_ids("tok", SUB) == {FOUNDRY_ID.lower()}
    assert len(responses.calls) == 1


@pytest.mark.parametrize("bad", [12345, "abc", {"a": 1}, None])
@responses.activate
def test_a_non_list_value_in_discovery_does_not_raise(bad):
    responses.add(responses.GET, ACCOUNTS_URL, json={"value": bad}, status=200)
    assert az.fetch_foundry_resource_ids("tok", SUB) == set()


def test_the_error_backoff_exponent_is_capped():
    """~1030 consecutive errors used to raise OverflowError out of the backoff."""
    assert az._error_backoff(5000) == az.MAX_ERROR_BACKOFF


_THROTTLE_HEADERS = {"Retry-After": "21600"}
# What each tolerant sub-fetch is followed by, so a 429 on it can be shown to
# stop the refresh rather than merely be swallowed.
_AFTER_ENDPOINT = {
    "subscription": "CostManagement/query",
    "accounts": "CostManagement/query",
    "marketplace": "Consumption/budgets",
    "budgets": "CostManagement/forecast",
    "forecast": None,
}


def _throttle(endpoint):
    """Turn one ARM endpoint into a 429 asking for a six-hour back-off."""
    if endpoint == "subscription":
        responses.replace(
            responses.GET, SUBSCRIPTION_URL, json={}, status=429,
            headers=_THROTTLE_HEADERS,
        )
    elif endpoint == "accounts":
        responses.replace(
            responses.GET, ACCOUNTS_URL, json={}, status=429,
            headers=_THROTTLE_HEADERS,
        )
    elif endpoint == "budgets":
        responses.replace(
            responses.GET, BUDGETS_URL, json={}, status=429,
            headers=_THROTTLE_HEADERS,
        )
    elif endpoint == "forecast":
        responses.replace(
            responses.POST, FORECAST_URL, json={}, status=429,
            headers=_THROTTLE_HEADERS,
        )
    elif endpoint == "marketplace":
        # The marketplace query is the second POST to the query URL, so this
        # is added rather than replaced: responses serves them in order.
        responses.add(
            responses.POST, QUERY_URL, json={}, status=429,
            headers=_THROTTLE_HEADERS,
        )
    else:  # pragma: no cover - guard against a typo in the parametrisation
        raise AssertionError(endpoint)


@pytest.mark.parametrize("endpoint", sorted(_AFTER_ENDPOINT))
@responses.activate
def test_a_429_on_any_sub_fetch_still_records_the_servers_backoff(
    endpoint, monkeypatch, config
):
    """A tolerant sub-fetch may swallow its own failure. It may never swallow
    the tenant-wide back-off the server asked for: the 429 is the one answer
    that is about every other caller in the tenant, not about this fetch."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.include_marketplace = True
    _stub_everything()
    _throttle(endpoint)

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    state = az.state_for(SUB)
    assert state.blocked_until is not None, "the server's back-off was dropped"
    assert snapshot.status == SnapshotStatus.ERROR
    assert "rate limiting" in (snapshot.error or "")
    after = _AFTER_ENDPOINT[endpoint]
    if after is not None:
        assert not [c for c in responses.calls if after in c.request.url], (
            "the refresh carried on issuing ARM requests inside the 429 window"
        )


# The note each tolerant sub-fetch owes the reader when it comes back empty
# for a reason that is not an answer.
_SERVER_ERROR_NOTE = {
    "subscription": "The subscription's offer type could not be read.",
    "accounts": "Foundry resources could not be listed this refresh.",
    "budgets": "Azure Budgets were unavailable this refresh.",
    "forecast": "The forecast was unavailable this refresh.",
    "marketplace": "Marketplace breakdown unavailable this refresh.",
}


def _server_error(endpoint):
    """Turn one ARM endpoint into a 500 with a body that is not JSON."""
    body = "<html>502 Bad Gateway</html>"
    if endpoint == "subscription":
        responses.replace(responses.GET, SUBSCRIPTION_URL, body=body, status=500)
    elif endpoint == "accounts":
        responses.replace(responses.GET, ACCOUNTS_URL, body=body, status=500)
    elif endpoint == "budgets":
        responses.replace(responses.GET, BUDGETS_URL, body=body, status=500)
    elif endpoint == "forecast":
        responses.replace(responses.POST, FORECAST_URL, body=body, status=500)
    elif endpoint == "marketplace":
        # The marketplace query is the second POST to the query URL, so this
        # is added rather than replaced: responses serves them in order.
        responses.add(responses.POST, QUERY_URL, body=body, status=500)
    else:  # pragma: no cover - guard against a typo in the parametrisation
        raise AssertionError(endpoint)


@pytest.mark.parametrize("endpoint", sorted(_SERVER_ERROR_NOTE))
@responses.activate
def test_a_500_on_any_tolerant_sub_fetch_says_so(endpoint, monkeypatch, config):
    """`_check` raises for 429, 401 and 403 only, so every other non-200 used
    to return the same "nothing here" a legitimately empty answer returns: an
    entirely healthy-looking tile with a row silently missing, for the failure
    that happens most often. A 5xx is the server saying "ask again", which is
    not an answer, and the note the tolerant branches already carry is the one
    the reader needs."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.include_marketplace = True
    _stub_everything()
    _server_error(endpoint)

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    assert snapshot.status == SnapshotStatus.OK, "a sub-fetch took the tile down"
    assert _SERVER_ERROR_NOTE[endpoint] in (snapshot.metrics[0].note or "")


@responses.activate
def test_a_403_on_the_marketplace_query_does_not_take_the_tile_down(
    monkeypatch, config
):
    """The marketplace breakdown is an extra row, not the tile."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.include_marketplace = True
    _stub_everything()
    responses.add(responses.POST, QUERY_URL, json={}, status=403)

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    assert snapshot.status == SnapshotStatus.OK
    assert "Marketplace breakdown unavailable" in (snapshot.metrics[0].note or "")


@responses.activate
def test_a_truncated_marketplace_query_drops_the_split_it_could_not_finish(
    monkeypatch, config
):
    """A marketplace read that stopped early moves *part* of a resource's
    charge into the Marketplace row and leaves the rest under its own service.
    The month's total is unaffected - the rows are only re-labelled - so the
    split goes rather than the tile, and the note says why."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.include_marketplace = True
    market_row = [3.0, 20260907, MARKET_ID, "Global resources", "CAD"]
    _stub_everything(rows=DEFAULT_ROWS + [market_row])
    page_one = query_payload([[1.0, 20260907, MARKET_ID, "Global resources", "CAD"]])
    page_one["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=page2"
    responses.add(responses.POST, QUERY_URL, json=page_one, status=200)
    responses.add(responses.POST, QUERY_URL, json={}, status=500)

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    assert snapshot.status == SnapshotStatus.OK
    note = snapshot.metrics[0].note or ""
    assert "Marketplace breakdown unavailable this refresh (results truncated)." in note
    assert az.MARKETPLACE_BUCKET not in [m.label for m in snapshot.metrics]
    assert snapshot.raw["marketplace_cost"] is None
    # The whole charge stays where the main query put it.
    resource_row = [m for m in snapshot.metrics if m.label == "Global resources"]
    assert resource_row and "CAD 3.00" in (resource_row[0].note or "")


@responses.activate
def test_a_backwards_clock_jump_does_not_park_the_in_flight_flag(
    monkeypatch, config
):
    """The staleness rule measures from `last_fetch_at`, and the clock guard
    directly above it nulls a stamp that sits in the future. After a backwards
    correction (an NTP step, a bad RTC) there was therefore no clock left for
    `IN_FLIGHT_STALE_AFTER` to fire against, and a lost worker held the tile
    for the life of the process - the exact failure the expiry was added for."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    state = az.state_for(SUB)
    state.in_flight = True
    state.last_fetch_at = datetime.now() + timedelta(hours=2)

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    assert az.state_for(SUB).in_flight is False
    assert "already in progress" not in (snapshot.error or "")
    assert snapshot.status == SnapshotStatus.OK


@responses.activate
def test_a_failed_dispatch_does_not_park_the_tile(monkeypatch, config):
    """in_flight is set by the caller and cleared by the callee, so the one
    path where the callee never runs used to leak it for the whole process."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()

    class _BoomPool:
        def start(self, runnable):
            raise RuntimeError("QThreadPool has been deleted")

    captured: list = []
    az.AzureProvider(config, pool=_BoomPool()).refresh(captured.append)

    assert captured, "the dispatch failure was raised into the App refresh loop"
    assert captured[0].status == SnapshotStatus.ERROR
    assert "RuntimeError" in (captured[0].error or "")
    state = az.state_for(SUB)
    assert state.in_flight is False
    assert state.last_error is not None

    second: list = []
    az.AzureProvider(config, pool=_BoomPool()).refresh(second.append)
    assert "already in progress" not in (second[0].error or "")


@responses.activate
def test_the_in_flight_flag_alone_refuses_a_second_dispatch(monkeypatch, config):
    """last_fetch_at is nulled between the two refreshes, so the hourly floor
    cannot be what refuses the second one - the flag has to be."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    dispatched: list = []

    class _HoldingPool:
        def start(self, runnable):
            dispatched.append(runnable)

    provider = az.AzureProvider(config, pool=_HoldingPool())
    provider.refresh(lambda snap: None)
    assert len(dispatched) == 1

    az.state_for(SUB).last_fetch_at = None
    captured: list = []
    provider.refresh(captured.append)
    assert len(dispatched) == 1, "a second live fetch was dispatched"
    assert "already in progress" in (captured[0].error or "")


@responses.activate
def test_a_stale_in_flight_flag_does_not_park_the_tile_forever(monkeypatch, config):
    """Nothing but a completed worker clears the flag, so a worker that never
    reported has to time out rather than hold the tile until restart."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    state = az.state_for(SUB)
    state.in_flight = True
    state.last_fetch_at = datetime.now() - timedelta(hours=3)

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    assert snapshot.status == SnapshotStatus.OK
    assert az.state_for(SUB).in_flight is False


@responses.activate
def test_an_exception_no_one_anticipated_is_recorded_by_the_real_worker(
    monkeypatch, config
):
    """C-1's actual hole: the catch-all in work() had no test that failed
    without it, because the fixture that used to reach it stopped raising."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()

    def _boom(*args, **kwargs):
        raise KeyError("surprise")

    monkeypatch.setattr(az.AzureProvider, "_fetch", _boom)
    pool = _InlinePool()
    provider = az.AzureProvider(config, pool=pool)

    snapshot = _run_through_pool(provider)
    assert snapshot.status == SnapshotStatus.ERROR
    assert "KeyError" in (snapshot.error or "")
    state = az.state_for(SUB)
    assert state.blocked_until is not None, "the hourly floor was switched off"
    assert state.consecutive_errors == 1
    assert state.in_flight is False

    calls = len(responses.calls)
    _run_through_pool(provider)
    assert len(responses.calls) == calls, "the second refresh went to the network"
    assert pool.started == 1


@responses.activate
def test_a_blocked_until_past_the_backoff_ceiling_is_cleared(monkeypatch, config):
    """The twin of the last_fetch_at clock-jump reset: no back-off this module
    can produce exceeds MAX_ERROR_BACKOFF, so one that does is a bad clock."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    az.state_for(SUB).blocked_until = datetime.now() + timedelta(days=3650)

    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.OK


@responses.activate
def test_ten_settings_saves_inside_the_hour_buy_one_live_fetch(monkeypatch, config):
    """A settings save is a one-click human action and it is easy to loop.
    Changing what the query asks for invalidates the cached answer; it does
    not reopen the fetch window, which is what README and SECURITY.md promise."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    for press in range(10):
        config.azure.reset_day = 15 if press % 2 else 16
        snapshot = _run(az.AzureProvider(config), monkeypatch)

    queries = [c for c in responses.calls if "CostManagement/query" in c.request.url]
    tokens = [c for c in responses.calls if c.request.url.startswith(TOKEN_URL)]
    assert len(queries) == 1, "a settings toggle reopened the hourly window"
    assert len(tokens) == 1
    # And the stale answer is not replayed either: the tile says it is waiting.
    assert snapshot.status == SnapshotStatus.ERROR
    assert "window" in (snapshot.error or "")


@responses.activate
def test_a_credential_change_still_resets_everything(monkeypatch, config):
    """The credential triple is the one identity that means the cached
    back-off describes a different app registration."""
    secret = {"value": "shhh"}
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: secret["value"])
    _stub_everything()
    _run(az.AzureProvider(config), monkeypatch)
    az.state_for(SUB).blocked_until = datetime.now() + timedelta(hours=5)

    secret["value"] = "rotated"
    snapshot = _run(az.AzureProvider(config), monkeypatch)

    assert snapshot.status == SnapshotStatus.OK
    assert az.state_for(SUB).blocked_until is None


def test_a_refresh_stops_paging_when_its_wall_clock_budget_runs_out():
    """MAX_QUERY_PAGES x REQUEST_TIMEOUT is ~13 minutes on one pool thread,
    shared with every other provider."""
    ticks = iter([0.0])
    budget = az._RequestBudget(clock=lambda: next(ticks, 500.0))
    parsed = az.QueryRows(cost_column_found=True)
    payload = query_payload([])
    payload["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=abc"

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.POST, QUERY_URL, json=query_payload([]), status=200)
        az._follow_query_pages("tok", {}, payload, parsed, budget)
        assert not mock.calls, "a page was fetched past the refresh deadline"
    assert parsed.truncated is True


def test_a_refresh_stops_paging_when_its_request_budget_runs_out():
    budget = az._RequestBudget(max_requests=2, clock=lambda: 0.0)
    parsed = az.QueryRows(cost_column_found=True)
    payload = query_payload([[1.0, 20260901, STORAGE_ID, "Storage", "CAD"]])
    payload["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=a"
    page2 = query_payload([[1.0, 20260902, STORAGE_ID, "Storage", "CAD"]])
    page2["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=b"
    page3 = query_payload([[1.0, 20260903, STORAGE_ID, "Storage", "CAD"]])
    page3["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=c"

    with responses.RequestsMock() as mock:
        mock.add(responses.POST, QUERY_URL, json=page2, status=200)
        mock.add(responses.POST, QUERY_URL, json=page3, status=200)
        az._follow_query_pages("tok", {}, payload, parsed, budget)
        assert len(mock.calls) == 2
    assert parsed.truncated is True


@responses.activate
def test_discovery_stops_at_the_refresh_request_budget(monkeypatch, config):
    responses.add(
        responses.GET,
        ACCOUNTS_URL,
        json={
            "value": [{"id": FOUNDRY_ID, "kind": "AIServices"}],
            "nextLink": f"{ACCOUNTS_URL}?$skiptoken=abc",
        },
        status=200,
    )
    budget = az._RequestBudget(max_requests=1, clock=lambda: 0.0)
    found = az.fetch_foundry_resource_ids("tok", SUB, budget=budget)
    assert found == {FOUNDRY_ID.lower()}
    assert len(responses.calls) == 1
    assert budget.exhausted is True


# --- the cost query is paged ------------------------------------------------
#
# Daily granularity grouped on ResourceId *and* ServiceName reaches the API's
# ~1000-row page at roughly 33 resources over a month, so a busy subscription
# is exactly the one that would have under-reported.


@responses.activate
def test_the_cost_query_follows_nextlink_and_sums_every_page():
    page1 = query_payload([[10.0, 20260901, STORAGE_ID, "Storage", "CAD"]])
    page1["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=abc"
    page2 = query_payload([[5.0, 20260902, OPENAI_ID, "Azure OpenAI", "CAD"]])
    responses.add(responses.POST, QUERY_URL, json=page1, status=200)
    responses.add(responses.POST, QUERY_URL, json=page2, status=200)

    parsed, _metric = az.fetch_query("tok", SUB, date(2026, 9, 1), date(2026, 10, 1))
    assert parsed.total == pytest.approx(15.0)
    assert parsed.row_count == 2
    assert parsed.by_service["Azure OpenAI"] == pytest.approx(5.0)
    assert parsed.truncated is False
    assert len(responses.calls) == 2


@responses.activate
def test_a_foreign_nextlink_on_the_cost_query_is_never_requested(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    payload = query_payload(DEFAULT_ROWS)
    payload["properties"]["nextLink"] = "https://management.azure.com.evil.example/x"
    responses.replace(responses.POST, QUERY_URL, json=payload, status=200)

    snapshot = _run(az.AzureProvider(config), monkeypatch)
    hosts = {requests.utils.urlparse(c.request.url).hostname for c in responses.calls}
    assert hosts <= {"login.microsoftonline.com", "management.azure.com"}
    summary = snapshot.metrics[0]
    # A partial total must not be presented as a clean gauge.
    assert summary.percent_used is None
    assert "truncated" in (summary.note or "").lower()


@responses.activate
def test_the_query_page_loop_is_capped():
    """Each page hands back a *different* link, so the cap is what stops it
    rather than the repeated-link guard."""
    def _page(request):
        token = len(responses.calls)
        payload = query_payload([[1.0, 20260901, STORAGE_ID, "Storage", "CAD"]])
        payload["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken={token}"
        return 200, {}, json.dumps(payload)

    responses.add_callback(responses.POST, QUERY_URL, callback=_page)

    parsed, _metric = az.fetch_query("tok", SUB, date(2026, 9, 1), date(2026, 10, 1))
    assert len(responses.calls) == az.MAX_QUERY_PAGES
    assert parsed.truncated is True


@responses.activate
def test_a_repeated_nextlink_stops_the_loop_instead_of_multiplying_the_total(
    monkeypatch, config
):
    """A page that points back at itself counted its own rows once per page:
    twenty POSTs, and a total twenty times the truth on the row."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    payload = query_payload([[5.0, 20260901, STORAGE_ID, "Storage", "CAD"]])
    payload["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=forever"
    responses.replace(responses.POST, QUERY_URL, json=payload, status=200)

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    posts = [c for c in responses.calls if "CostManagement/query" in c.request.url]
    assert len(posts) <= 2, "the loop followed a link it had already read"
    summary = snapshot.metrics[0]
    assert summary.percent_used is None
    assert "truncated" in (summary.note or "").lower()


@responses.activate
def test_a_repeated_nextlink_says_the_subtotal_may_count_rows_twice(
    monkeypatch, config
):
    """Every other early exit leaves a subtotal, so the note says "read so
    far" - which tells the reader the true figure is *higher*. A link back to
    a page already read is the opposite case: those rows were counted again
    before the guard fired, so the figure is higher than the truth, and the
    same sentence would point the reader the wrong way."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    payload = query_payload([[5.0, 20260901, STORAGE_ID, "Storage", "CAD"]])
    payload["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=forever"
    responses.replace(responses.POST, QUERY_URL, json=payload, status=200)

    note = (_run(az.AzureProvider(config), monkeypatch).metrics[0].note or "")

    assert "more than once" in note
    assert "read so far" not in note


@pytest.mark.parametrize(
    "body",
    [None, {}, {"error": {"code": "GatewayTimeout"}}, "quantity-only"],
    ids=["non-json", "empty", "arm-error-doc", "schema-drift"],
)
@responses.activate
def test_an_unreadable_continuation_page_is_not_an_empty_page(
    monkeypatch, config, body
):
    """fetch_query refuses a *first* page with no cost column - "a 200 whose
    body is an ARM error document is not an empty month". Page 7 of 12 got no
    such check: it merged as zero and the subtotal was gauged at OK."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    page1 = query_payload([[5.0, 20260901, STORAGE_ID, "Storage", "CAD"]])
    page1["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=abc"
    responses.replace(responses.POST, QUERY_URL, json=page1, status=200)
    if body == "quantity-only":
        responses.add(
            responses.POST,
            QUERY_URL,
            json={
                "properties": {
                    "columns": [{"name": "Quantity", "type": "Number"}],
                    "rows": [[999.0]],
                }
            },
            status=200,
        )
    elif body is None:
        responses.add(responses.POST, QUERY_URL, body="not json at all", status=200)
    else:
        responses.add(responses.POST, QUERY_URL, json=body, status=200)

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    summary = snapshot.metrics[0]
    assert summary.percent_used is None, "a subtotal was shown as a gauge"
    assert "truncated" in (summary.note or "").lower()


@responses.activate
def test_an_unreadable_continuation_page_stops_the_loop():
    """Not only marked truncated: there is nothing to be gained from reading
    further pages of a response whose schema this parser cannot follow, and
    each one is a request against a tenant-wide quota."""
    page1 = query_payload([[5.0, 20260901, STORAGE_ID, "Storage", "CAD"]])
    page1["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=a"
    unreadable = {
        "error": {"code": "GatewayTimeout"},
        "properties": {"nextLink": f"{QUERY_URL}?$skiptoken=b"},
    }
    responses.add(responses.POST, QUERY_URL, json=page1, status=200)
    responses.add(responses.POST, QUERY_URL, json=unreadable, status=200)
    responses.add(
        responses.POST,
        QUERY_URL,
        json=query_payload([[900.0, 20260902, STORAGE_ID, "Storage", "CAD"]]),
        status=200,
    )

    parsed, _metric = az.fetch_query("tok", SUB, date(2026, 9, 1), date(2026, 10, 1))

    assert len(responses.calls) == 2, "the loop kept paging past an unreadable page"
    assert parsed.truncated is True
    assert parsed.total == pytest.approx(5.0)


def test_merging_a_page_with_no_cost_column_marks_the_total_a_subtotal():
    """Belt and braces for any future caller of the merge helper."""
    into = az.QueryRows(total=10.0, cost_column_found=True)
    az._merge_query_rows(into, az.QueryRows())
    assert into.truncated is True


# --- identifiers must not ride out on an error string -----------------------
#
# A requests transport exception stringifies with the full request URL, and
# that URL carries /subscriptions/<GUID>/ or /<TENANT GUID>/oauth2. The
# diagnostics blob is redacted; ai-gauge.log, the tile tooltip, the error
# dialog header and --probe stdout are not. So the id must never get into
# snapshot.error in the first place.


@responses.activate
def test_a_transport_failure_does_not_carry_the_subscription_id(
    monkeypatch, config, caplog
):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    responses.add(
        responses.POST, TOKEN_URL, json={"access_token": "tok", "expires_in": 3600}
    )
    responses.add(
        responses.GET,
        SUBSCRIPTION_URL,
        json={"subscriptionPolicies": {"quotaId": "MSDN_2014-09-01"}},
        status=200,
    )
    responses.add(responses.GET, ACCOUNTS_URL, json={"value": []}, status=200)
    responses.add(
        responses.POST,
        QUERY_URL,
        body=requests.ConnectionError(
            "HTTPSConnectionPool(host='management.azure.com', port=443): Max "
            f"retries exceeded with url: /subscriptions/{SUB}/providers/"
            "Microsoft.CostManagement/query?api-version=2025-03-01"
        ),
    )
    with caplog.at_level("WARNING"):
        snapshot = _run(az.AzureProvider(config), monkeypatch)

    assert snapshot.status == SnapshotStatus.ERROR
    assert SUB not in (snapshot.error or "")
    assert "ConnectionError" in (snapshot.error or "")
    assert SUB not in caplog.text


@responses.activate
def test_an_entra_transport_failure_does_not_carry_the_tenant_id(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    responses.add(
        responses.POST,
        TOKEN_URL,
        body=requests.ConnectionError(
            "HTTPSConnectionPool(host='login.microsoftonline.com', port=443): "
            f"Max retries exceeded with url: /{TENANT}/oauth2/v2.0/token"
        ),
    )
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.ERROR
    assert TENANT not in (snapshot.error or "")


def test_the_probe_never_prints_an_id(monkeypatch, capsys, config):
    """--probe output is what an operator pastes into a bug report."""
    monkeypatch.setattr(az.Config, "load", classmethod(lambda cls: config))
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    monkeypatch.setattr(az, "get_token", lambda *a, **k: "tok")

    boom = requests.ConnectionError(
        "HTTPSConnectionPool(host='management.azure.com', port=443): Max retries "
        f"exceeded with url: /subscriptions/{SUB}/providers/"
        "Microsoft.CostManagement/query"
    )

    def _raise(*args, **kwargs):
        raise boom

    for name in (
        "fetch_quota_id",
        "fetch_foundry_resource_ids",
        "fetch_query",
        "fetch_budget",
        "fetch_forecast",
    ):
        monkeypatch.setattr(az, name, _raise)

    az._probe()
    out = capsys.readouterr().out
    assert SUB not in out
    assert TENANT not in out
    assert "ConnectionError" in out


@responses.activate
def test_the_aad_error_code_is_reduced_to_a_code():
    """The AAD body is only as trustworthy as the TLS path to Entra ID, and
    the code lands in the log, the tile, and a RichText dialog header."""
    responses.add(
        responses.POST,
        TOKEN_URL,
        json={
            "error": "invalid_client\nWARNING forged log line <b>markup</b>"
            + "z" * 5000
        },
        status=401,
    )
    with pytest.raises(AzureAuthError) as exc:
        get_token(TENANT, CLIENT, "bad", now=datetime(2026, 9, 9))
    code = exc.value.code
    assert code == "invalid_client"
    assert len(code) <= 32
    assert "\n" not in code and "<b>" not in code and " " not in code
    message = str(exc.value)
    assert "\n" not in message and "<b>" not in message


@responses.activate
def test_a_description_shaped_aad_error_cannot_smuggle_an_id_through():
    """Every character of "AADSTS7000215: tenant <guid> app x" is in the
    allowlist, so the filter alone passed the whole sentence - GUIDs included -
    into the log, the tile and an unredacted tray tooltip. A code is one
    token."""
    responses.add(
        responses.POST,
        TOKEN_URL,
        json={"error": f"AADSTS7000215: tenant {TENANT} app {CLIENT}"},
        status=401,
    )
    with pytest.raises(AzureAuthError) as exc:
        get_token(TENANT, CLIENT, "bad", now=datetime(2026, 9, 9))

    code = exc.value.code
    assert code == "AADSTS7000215", "the description rode out as a code"
    assert TENANT not in str(exc.value) and CLIENT not in str(exc.value)


@pytest.mark.parametrize(
    "expires_in,expected_max_s",
    [
        (float("inf"), 86400),
        (1e20, 86400),
        (10 ** 20, 86400),
        (3600, 3600),
        ("nan", 700),
    ],
)
@responses.activate
def test_a_hostile_expires_in_neither_raises_nor_outlives_the_day(
    expires_in, expected_max_s
):
    """int(float("inf")) raises OverflowError, and json.loads accepts both
    Infinity and 1e20 - the same defect class that threw a 429's own back-off
    away one file over, and _fetch does not name OverflowError."""
    from aigauge.providers import _azure_auth

    responses.add(
        responses.POST,
        TOKEN_URL,
        json={"access_token": "tok", "expires_in": expires_in},
        status=200,
    )
    now = datetime(2026, 9, 9)
    assert get_token(TENANT, CLIENT, "s", now=now) == "tok"

    cached = next(iter(_azure_auth._CACHE.values()))
    assert cached.expires_at > now
    assert (cached.expires_at - now).total_seconds() <= expected_max_s


def test_a_keyring_failure_still_forgets_the_cached_token(monkeypatch):
    """clear_cache ran only after keyring.set_password, so a keyring that
    raised left the bearer minted from the *old* secret live in memory."""
    from aigauge import config as config_module
    from aigauge.providers import _azure_auth

    def _boom(service, account, value):
        raise RuntimeError("keyring is locked")

    monkeypatch.setattr(config_module.keyring, "set_password", _boom)
    _azure_auth._CACHE[(TENANT, CLIENT, "digest")] = _azure_auth._CachedToken(
        token="live", expires_at=datetime.now() + timedelta(hours=1)
    )
    with pytest.raises(RuntimeError):
        config_module.set_azure_client_secret("new-secret")
    assert not _azure_auth._CACHE


@responses.activate
def test_an_unexpected_exception_does_not_log_the_subscription_id(
    caplog, monkeypatch, config
):
    """log.exception implies exc_info=True, so the formatted traceback appends
    the exception's message verbatim - and a transport failure's message is
    the request URL, which carries the subscription id. ai-gauge.log is the
    file the error dialog's "Open log folder" button points at."""
    import logging

    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    url = (
        f"https://management.azure.com/subscriptions/{SUB}"
        "/providers/Microsoft.CostManagement/query"
    )

    def _boom(*args, **kwargs):
        raise RuntimeError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(az.AzureProvider, "_fetch", _boom)
    with caplog.at_level(logging.DEBUG):
        snapshot = _run_through_pool(az.AzureProvider(config, pool=_InlinePool()))

    assert SUB not in caplog.text, "the traceback put the subscription id in the log"
    assert SUB not in (snapshot.error or "")
    assert "classification=unexpected_exception" in caplog.text


# --- marketplace splits a row, it does not swallow a resource ---------------


def test_marketplace_moves_its_own_amount_not_the_whole_resource():
    """A resource with 900 of ordinary usage and 1.00 of Marketplace charge
    must not have all 901 reported as Marketplace model spend."""
    rows = [
        [900.0, 20260908, MARKET_ID, "Azure App Service", "CAD"],
        [1.0, 20260908, MARKET_ID, "Global resources", "CAD"],
    ]
    parsed = az.parse_query_response(query_payload(rows))
    marketplace = {(MARKET_ID.lower(), "Global resources"): 1.0}

    buckets, _foundry, marketplace_cost, _services = az.bucket_costs(
        parsed, set(), marketplace
    )
    by_name = dict(buckets)
    assert marketplace_cost == pytest.approx(1.0)
    assert by_name[az.MARKETPLACE_BUCKET] == pytest.approx(1.0)
    assert by_name["Azure App Service"] == pytest.approx(900.0)
    assert sum(cost for _n, cost in buckets) == pytest.approx(parsed.total)


def test_marketplace_never_moves_more_than_the_main_row_holds():
    rows = [[2.0, 20260908, MARKET_ID, "Global resources", "CAD"]]
    parsed = az.parse_query_response(query_payload(rows))
    buckets, _f, marketplace_cost, _s = az.bucket_costs(
        parsed, set(), {(MARKET_ID.lower(), "Global resources"): 50.0}
    )
    assert marketplace_cost == pytest.approx(2.0)
    assert sum(cost for _n, cost in buckets) == pytest.approx(parsed.total)


@responses.activate
def test_the_marketplace_query_only_reclassifies_its_own_rows(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.include_marketplace = True
    main_rows = [
        [900.0, 20260908, MARKET_ID, "Azure App Service", "CAD"],
        [1.0, 20260908, MARKET_ID, "Global resources", "CAD"],
    ]
    _stub_everything(rows=main_rows)
    responses.add(
        responses.POST,
        QUERY_URL,
        json=query_payload([[1.0, 20260908, MARKET_ID, "Global resources", "CAD"]]),
        status=200,
    )
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.raw["marketplace_cost"] == pytest.approx(1.0)
    rows = {m.label: m.note for m in snapshot.metrics if m.tag == az.BREAKDOWN_TAG}
    assert "CAD 1.00" in rows[az.MARKETPLACE_BUCKET]


# --- an Azure Budget is only sometimes the right denominator ---------------


def _budget(amount, **props):
    body = {"category": "Cost", "timeGrain": "Monthly", "amount": amount}
    body.update(props)
    return {"properties": body}


def _budgets(*items):
    responses.add(responses.GET, BUDGETS_URL, json={"value": list(items)}, status=200)


@pytest.mark.parametrize("amount", [float("inf"), "1e400", float("nan"), "abc", -5, 0])
@responses.activate
def test_a_non_finite_or_unusable_budget_amount_is_ignored(amount):
    """float('inf') > 0 is True, and total/inf*100 is 0.0 - a gauge pinned at
    0% while spend accrues is the failure this module refuses everywhere else."""
    _budgets(_budget(amount))
    got, grain, _note = az.fetch_budget("tok", SUB, currency="CAD")
    assert got is None and grain is None


@responses.activate
def test_a_scoped_budget_is_not_the_subscriptions_allowance():
    """Subscription-scope budgets routinely carry a dimension filter; a £5
    budget on one resource group is not the whole subscription's allowance."""
    _budgets(
        _budget(
            5.0,
            filter={
                "dimensions": {
                    "name": "ResourceGroupName",
                    "operator": "In",
                    "values": ["rg-unrelated"],
                }
            },
        )
    )
    got, _grain, _note = az.fetch_budget("tok", SUB, currency="CAD")
    assert got is None


@responses.activate
def test_an_unfiltered_budget_is_refused_for_a_resource_group_filtered_tile():
    """The scope check was one-directional: it refused a budget narrower than
    the tile and accepted one wider. A subscription-wide budget used as the
    denominator for one resource group under-reports by the ratio between
    them - the same error, in the other direction."""
    _budgets(_budget(9000.0))
    got, _grain, note = az.fetch_budget(
        "tok", SUB, currency="CAD", resource_group="rg-ai"
    )
    assert got is None
    assert "whole subscription" in (note or "")


@responses.activate
def test_an_unfiltered_budget_is_accepted_when_the_tile_is_not_filtered():
    _budgets(_budget(9000.0))
    got, _grain, note = az.fetch_budget("tok", SUB, currency="CAD")
    assert got == pytest.approx(9000.0)
    assert note is None


@responses.activate
def test_a_typed_allowance_survives_an_alert_canary_budget(monkeypatch, config):
    """End to end: a 1.00 canary and the real 5000.00 both qualify, the
    smallest of them wins between themselves, and neither displaces the
    number in Settings."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.monthly_allowance = 5000.0
    _stub_everything()
    responses.replace(
        responses.GET,
        BUDGETS_URL,
        json={"value": [_budget(1.0), _budget(5000.0)]},
        status=200,
    )

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    summary = snapshot.metrics[0]
    assert "of 5,000.00" in (summary.reset_label or "")
    assert "of 1.00" not in (summary.reset_label or "")
    assert summary.percent_used == pytest.approx(28.55 / 50, abs=0.05)
    assert "Azure Budget of CAD 1.00" in (summary.note or "")


@responses.activate
def test_a_budget_scoped_to_the_configured_resource_group_is_accepted():
    _budgets(
        _budget(
            5.0,
            filter={
                "dimensions": {
                    "name": "ResourceGroupName",
                    "operator": "In",
                    "values": ["RG-AI"],
                }
            },
        )
    )
    got, _grain, _note = az.fetch_budget(
        "tok", SUB, currency="CAD", resource_group="rg-ai"
    )
    assert got == pytest.approx(5.0)


@responses.activate
def test_a_budget_in_another_currency_is_refused_with_a_note():
    _budgets(_budget(200.0, currentSpend={"amount": 30.0, "unit": "USD"}))
    got, _grain, note = az.fetch_budget("tok", SUB, currency="CAD")
    assert got is None
    assert "currency" in (note or "").lower()


@responses.activate
def test_the_smallest_qualifying_budget_wins():
    """The smallest is the one that alerts first, and the conservative
    denominator when several apply."""
    _budgets(_budget(2000.0), _budget(300.0), _budget(900.0))
    got, _grain, _note = az.fetch_budget("tok", SUB, currency="CAD")
    assert got == pytest.approx(300.0)


@responses.activate
def test_a_budget_currency_can_come_from_the_forecast_unit():
    _budgets(_budget(250.0, forecastSpend={"amount": 40.0, "unit": "CAD"}))
    got, _grain, _note = az.fetch_budget("tok", SUB, currency="CAD")
    assert got == pytest.approx(250.0)


@responses.activate
def test_a_budget_does_not_apply_to_an_anniversary_period(monkeypatch, config):
    """An Azure Budget is a calendar-month figure; a reset day of 15 measures
    a different window, so the numerator and denominator would disagree."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.reset_day = 15
    _stub_everything()
    responses.replace(
        responses.GET,
        BUDGETS_URL,
        json={"value": [_budget(500.0, currentSpend={"amount": 1.0, "unit": "CAD"})]},
        status=200,
    )
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.raw["budget_amount"] is None
    note = snapshot.metrics[0].note or ""
    assert "budget" in note.lower()
    # 28.55 of the settings allowance (150), not of the 500 budget.
    assert snapshot.metrics[0].percent_used == pytest.approx(19.03, abs=0.01)


# --- never a confident wrong number ----------------------------------------
#
# The module's own doctrine, applied to the Sponsorship case from the start:
# a gauge that reads 0% because something upstream changed shape is worse than
# a visible failure, because the user has no way to detect it.


def test_the_cost_column_is_never_guessed_from_position():
    """A future UsageQuantity column ahead of the cost column would otherwise
    be summed and formatted as money."""
    columns = [
        {"name": "UsageQuantity", "type": "Number"},
        {"name": "SomeFutureBilledCost", "type": "Number"},
    ]
    assert az._cost_column(columns, az._column_index(columns)) == 1


def test_parse_query_response_reports_a_missing_cost_column():
    """Schema drift must be distinguishable from "no spend this month"."""
    parsed = az.parse_query_response({"properties": {"columns": [], "rows": [[1]]}})
    assert parsed.cost_column_found is False
    assert parsed.total == 0.0
    assert parsed.row_count == 0


def test_an_arm_error_body_returned_as_200_is_not_an_empty_month():
    parsed = az.parse_query_response({"error": {"code": "GatewayTimeout"}})
    assert parsed.cost_column_found is False


@responses.activate
def test_a_response_with_no_cost_column_is_an_error_not_a_zero_gauge(
    monkeypatch, config
):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    responses.replace(
        responses.POST, QUERY_URL, json={"error": {"code": "GatewayTimeout"}}, status=200
    )
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.ERROR
    assert "cost column" in (snapshot.error or "").lower()
    # And the error is remembered, so the throttle holds.
    assert az.state_for(SUB).last_error is not None


def test_zero_spend_on_a_known_offer_still_gets_its_gauge():
    """Legitimate at the start of a period: Cost Management lags 8-72 h."""
    snapshot = az.build_snapshot(
        _aggregate(total=0.0, data_as_of=None, buckets=[], row_count=0),
        AzureConfig(monthly_allowance=150.0),
    )
    summary = snapshot.metrics[0]
    assert summary.percent_used == pytest.approx(0.0)
    assert "No usage has been processed" in (summary.note or "")


def test_zero_spend_with_an_unreadable_offer_type_gets_no_gauge():
    """Cost Management Reader without Reader is the likely role split, and a
    Sponsorship subscription reports exactly this - zero, forever."""
    snapshot = az.build_snapshot(
        _aggregate(total=0.0, data_as_of=None, buckets=[], row_count=0, offer_known=False),
        AzureConfig(monthly_allowance=150.0),
    )
    summary = snapshot.metrics[0]
    assert summary.percent_used is None
    note = (summary.note or "").lower()
    assert "offer type" in note
    assert "sponsorship" in note


def test_mixed_currencies_are_not_summed_into_a_gauge():
    rows = [
        [100.0, 20260901, STORAGE_ID, "Storage", "JPY"],
        [10.0, 20260901, OPENAI_ID, "Azure OpenAI", "CAD"],
    ]
    parsed = az.parse_query_response(query_payload(rows))
    assert parsed.mixed_currency is True
    assert parsed.currency == ""
    snapshot = az.build_snapshot(
        _aggregate(total=parsed.total, currency=parsed.currency, mixed_currency=True),
        AzureConfig(monthly_allowance=200.0),
    )
    summary = snapshot.metrics[0]
    assert summary.percent_used is None
    assert "currenc" in (summary.note or "").lower()


def test_breakdown_shares_are_clamped_for_display():
    """A refund row makes a share negative and pushes another over 100."""
    snapshot = az.build_snapshot(
        _aggregate(
            total=10.0,
            buckets=[("Azure OpenAI", 100.0), ("Refunded thing", -90.0)],
        ),
        AzureConfig(monthly_allowance=1000.0),
    )
    shares = [m.percent_used for m in snapshot.metrics if m.tag == az.BREAKDOWN_TAG]
    assert shares == [pytest.approx(100.0), pytest.approx(0.0)]
    # The money is still readable in the note.
    notes = " ".join(m.note or "" for m in snapshot.metrics if m.tag == az.BREAKDOWN_TAG)
    assert "-90.00" in notes


def test_a_usage_date_in_the_future_is_not_data_as_of():
    """Stale data must not read as fresh - the one thing the line prevents."""
    future = (datetime.now(timezone.utc).date() + timedelta(days=400)).strftime("%Y%m%d")
    rows = [
        [4.0, 20260905, STORAGE_ID, "Storage", "CAD"],
        [1.0, int(future), STORAGE_ID, "Storage", "CAD"],
    ]
    parsed = az.parse_query_response(query_payload(rows))
    assert parsed.latest_usage_date == date(2026, 9, 5)


def test_the_forecast_body_filters_the_same_charge_type_as_the_query():
    """Otherwise the two rows are not measuring the same thing."""
    body = az.forecast_body(date(2026, 9, 1), date(2026, 10, 1), cost_metric="Cost")
    clauses = body["dataset"]["filter"]
    assert clauses["dimensions"]["name"] == "ChargeType"
    assert clauses["dimensions"]["values"] == ["Usage"]


@responses.activate
def test_a_forecast_below_the_actual_spend_is_dropped(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    responses.replace(
        responses.POST,
        FORECAST_URL,
        json=query_payload([[0.0, 20260930, "", "", "CAD"]]),
        status=200,
    )
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert not any(m.label == "Forecast end of month" for m in snapshot.metrics)


def test_a_zero_cost_foundry_row_is_still_pinned():
    rows = [[100.0, 20260901, f"{STORAGE_ID}-{i}", f"Service {i}", "CAD"] for i in range(8)]
    rows.append([0.0, 20260901, FOUNDRY_ID, "Foundry Tools", "CAD"])
    parsed = az.parse_query_response(query_payload(rows))
    buckets, _f, _m, _s = az.bucket_costs(parsed, {FOUNDRY_ID.lower()}, {})
    assert buckets[0][0] == az.FOUNDRY_BUCKET


def test_query_rows_are_bounded():
    rows = [
        [1.0, 20260901, f"{STORAGE_ID}-{i}", f"Service {i}", "CAD"]
        for i in range(az.MAX_QUERY_ROWS + 100)
    ]
    parsed = az.parse_query_response(query_payload(rows))
    assert parsed.row_count == az.MAX_QUERY_ROWS
    assert parsed.truncated is True


def test_network_supplied_strings_are_bounded_at_parse_time():
    rows = [[1.0, 20260901, STORAGE_ID, "S" * 5000, "C" * 3000]]
    parsed = az.parse_query_response(query_payload(rows))
    assert len(parsed.currency) <= 8
    assert max(len(name) for name in parsed.by_service) <= 120


# --- the tile never prints a number it has just disowned --------------------
#
# Every rule below is one flag: a snapshot is gaugeable when there is an
# allowance, the read is complete, and nothing about the data makes the sum
# meaningless. The summary percent, the breakdown shares and the forecast row
# all follow it, because a tile that says "no gauge is shown for a subtotal"
# and then prints a percentage two rows down has told the user nothing.


def test_a_subtotal_gets_no_forecast_row_and_no_breakdown_shares():
    snapshot = az.build_snapshot(
        _aggregate(partial=True, forecast_total=90.0),
        AzureConfig(monthly_allowance=150.0),
    )
    labels = [m.label for m in snapshot.metrics]
    assert "Forecast end of month" not in labels
    assert snapshot.metrics[0].percent_used is None
    assert all(
        m.percent_used is None for m in snapshot.metrics if m.tag == az.BREAKDOWN_TAG
    )


def test_a_subtotal_is_never_printed_as_the_total():
    snapshot = az.build_snapshot(
        _aggregate(partial=True), AzureConfig(monthly_allowance=150.0)
    )
    summary = snapshot.metrics[0]
    assert "incomplete" in (summary.reset_label or "")
    assert "36.10" not in (summary.reset_label or "")
    note = summary.note or ""
    assert "CAD 36.10" in note and "incomplete" in note


def test_mixed_currencies_are_shown_as_subtotals_not_as_a_sum():
    """1,100.00 is what CAD 100.00 plus JPY 1,000.00 is not."""
    snapshot = az.build_snapshot(
        _aggregate(
            total=1100.0,
            currency="",
            mixed_currency=True,
            currency_totals=[("CAD", 100.0), ("JPY", 1000.0)],
            buckets=[("Svc A", 100.0), ("Svc B", 1000.0)],
            forecast_total=2000.0,
        ),
        AzureConfig(monthly_allowance=150.0),
    )
    summary = snapshot.metrics[0]
    assert "1,100.00" not in (summary.reset_label or "")
    assert "CAD 100.00" in (summary.reset_label or "")
    assert "JPY 1,000.00" in (summary.reset_label or "")
    assert summary.percent_used is None
    breakdown = [m for m in snapshot.metrics if m.tag == az.BREAKDOWN_TAG]
    assert breakdown and all(m.percent_used is None for m in breakdown)
    assert all("currenc" in (m.note or "").lower() for m in breakdown)
    assert "Forecast end of month" not in [m.label for m in snapshot.metrics]


def test_more_than_three_currencies_are_summarised_not_listed():
    snapshot = az.build_snapshot(
        _aggregate(
            total=10.0,
            currency="",
            mixed_currency=True,
            currency_totals=[
                ("CAD", 4.0), ("JPY", 3.0), ("EUR", 2.0), ("GBP", 1.0)
            ],
        ),
        AzureConfig(monthly_allowance=150.0),
    )
    assert "1 more" in (snapshot.metrics[0].reset_label or "")


def test_the_currency_split_survives_paging():
    rows_page1 = [[100.0, 20260901, STORAGE_ID, "Storage", "CAD"]]
    rows_page2 = [[1000.0, 20260902, OPENAI_ID, "Azure OpenAI", "JPY"]]
    into = az.parse_query_response(query_payload(rows_page1))
    az._merge_query_rows(into, az.parse_query_response(query_payload(rows_page2)))
    assert into.mixed_currency is True
    assert into.by_currency == {"CAD": pytest.approx(100.0), "JPY": pytest.approx(1000.0)}


def test_an_unreadable_offer_type_never_gauges_whatever_the_total():
    """A Sponsorship subscription still bills Marketplace and other
    non-sponsored charges normally, so a positive total is exactly what one
    looks like while the sponsored credit drains unreported."""
    for total in (0.0, 0.001, 75.0):
        snapshot = az.build_snapshot(
            _aggregate(total=total, offer_known=False),
            AzureConfig(monthly_allowance=150.0),
        )
        summary = snapshot.metrics[0]
        assert summary.percent_used is None, f"gauged a {total} total"
        note = (summary.note or "").lower()
        assert "reader" in note and "sponsorship" in note
        # The money is still shown; only the percentage is refused.
        assert _money_in(summary, total)


def _money_in(metric, total):
    return f"{total:,.2f}" in (metric.reset_label or "") + (metric.note or "")


def test_the_printed_reset_day_is_the_one_the_countdown_counts_to():
    """West of UTC the tile printed 1 Oct and counted down to 30 Sep."""
    snapshot = az.build_snapshot(_aggregate(), AzureConfig(monthly_allowance=150.0))
    summary = snapshot.metrics[0]
    assert summary.resets_at is not None
    local = summary.resets_at
    printed = f"{local.day} {local.strftime('%b')}"
    assert f"resets {printed}" in (summary.reset_label or "")


# --- the remaining low-severity edges ---------------------------------------


@pytest.mark.parametrize(
    "name", ["CostCenter", "CostCentre", "CostCategory", "CostAllocationRuleId"]
)
def test_a_column_that_merely_contains_cost_is_not_money(name):
    """The fallback dropped the positional guess because summing a quantity
    into a money label is a wrong number rather than a visible failure. A
    cost-centre id is the same mistake by a different route: 4815162342 read
    as dollars."""
    payload = {
        "properties": {
            "columns": [
                {"name": name, "type": "Number"},
                {"name": "ServiceName", "type": "String"},
            ],
            "rows": [[4815162342, "Storage"]],
        }
    }
    parsed = az.parse_query_response(payload)
    assert parsed.cost_column_found is False
    assert parsed.total == 0.0


@pytest.mark.parametrize("name", ["Cost", "PreTaxCost", "cost", "CostInBillingCurrency"])
def test_the_documented_cost_column_names_all_resolve(name):
    parsed = az.parse_query_response(query_payload([[12.5, 20260901, "", "S", "CAD"]],
                                                   cost_name=name))
    assert parsed.cost_column_found is True
    assert parsed.total == pytest.approx(12.5)


def test_a_renamed_cost_column_is_still_found_by_the_fallback():
    payload = {
        "properties": {
            "columns": [
                {"name": "BilledPreTaxCost", "type": "Number"},
                {"name": "ServiceName", "type": "String"},
            ],
            "rows": [[12.5, "Storage"]],
        }
    }
    parsed = az.parse_query_response(payload)
    assert parsed.cost_column_found is True
    assert parsed.total == pytest.approx(12.5)


@pytest.mark.parametrize(
    "quota_id,expected",
    [
        ("Sponsored_2016-01-01", True),
        ("MS-AZR-0136P", True),
        ("ms-azr-0136p", True),
        ("EnterpriseAgreement_2014-09-01", False),
    ],
)
def test_sponsorship_covers_the_offer_ids_the_docstring_names(quota_id, expected):
    """EA Azure Sponsorship is listed as unsupported under an offer id with no
    "sponsor" in it, which the substring test the docstring claimed to cover
    did not match."""
    assert az.is_sponsorship(quota_id) is expected


@pytest.mark.parametrize("child", [".", ".."])
def test_a_dot_child_segment_is_not_a_resource_name(child):
    """The value is only ever a set member today, never a URL - but "." and
    ".." are path operators, not names, and this is the validator that would
    be relied on if that ever changed."""
    from aigauge.config import validate_azure_resource_id

    with pytest.raises(ValueError):
        validate_azure_resource_id(
            f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
            f"Microsoft.CognitiveServices/accounts/acct/projects/{child}"
        )


@responses.activate
def test_marketplace_charges_are_summed_per_pair_across_days(monkeypatch, config):
    """Daily granularity returns one row per (resource, service, day), so
    keeping the last day's amount instead of the sum moved only a fraction of
    the Marketplace spend out of the service row."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    config.azure.include_marketplace = True
    _stub_everything(
        rows=[
            [10.0, 20260906, MARKET_ID, "Global resources", "CAD"],
            [10.0, 20260907, MARKET_ID, "Global resources", "CAD"],
            [10.0, 20260908, MARKET_ID, "Global resources", "CAD"],
        ]
    )
    responses.add(
        responses.POST,
        QUERY_URL,
        json=query_payload(
            [
                [4.0, 20260906, MARKET_ID, "Global resources", "CAD"],
                [4.0, 20260907, MARKET_ID, "Global resources", "CAD"],
                [4.0, 20260908, MARKET_ID, "Global resources", "CAD"],
            ]
        ),
        status=200,
    )

    snapshot = _run(az.AzureProvider(config), monkeypatch)

    buckets = dict(snapshot.raw["buckets"])
    assert buckets[az.MARKETPLACE_BUCKET] == pytest.approx(12.0)
    assert buckets["Global resources"] == pytest.approx(18.0)


@responses.activate
def test_discovery_is_cached_for_a_day_and_re_read_after_it(monkeypatch, config):
    """The offer type and the Foundry resource list describe things that
    change on the order of months. Two calls an hour for them would double the
    tile's request count for nothing."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    _run(az.AzureProvider(config), monkeypatch)
    discovery = [c for c in responses.calls if "CognitiveServices/accounts" in c.request.url]
    assert len(discovery) == 1

    state = az.state_for(SUB)
    state.last_fetch_at -= az.MIN_FETCH_INTERVAL
    _run(az.AzureProvider(config), monkeypatch)
    discovery = [c for c in responses.calls if "CognitiveServices/accounts" in c.request.url]
    assert len(discovery) == 1, "discovery ran again inside its TTL"

    state = az.state_for(SUB)
    state.last_fetch_at -= az.MIN_FETCH_INTERVAL
    state.discovery_at -= az.DISCOVERY_TTL + timedelta(minutes=1)
    _run(az.AzureProvider(config), monkeypatch)
    discovery = [c for c in responses.calls if "CognitiveServices/accounts" in c.request.url]
    assert len(discovery) == 2, "discovery was never re-read"


# --- the token cache is a credential, and behaves like one -----------------


@responses.activate
def test_a_rotated_client_secret_is_not_served_from_the_token_cache():
    """A secret rotated because it leaked must stop working at once, not in
    55 minutes' time."""
    now = datetime(2026, 9, 9, 12, 0)
    responses.add(
        responses.POST, TOKEN_URL, json={"access_token": "old", "expires_in": 3600}
    )
    assert get_token(TENANT, CLIENT, "secret-1", now=now) == "old"
    responses.replace(
        responses.POST, TOKEN_URL, json={"access_token": "new", "expires_in": 3600}
    )
    assert get_token(TENANT, CLIENT, "secret-2", now=now) == "new"
    assert len(responses.calls) == 2


def test_clearing_the_saved_secret_forgets_the_token(monkeypatch):
    from aigauge import config as config_module
    from aigauge.providers import _azure_auth

    monkeypatch.setattr(
        config_module.keyring, "delete_password", lambda service, account: None
    )
    _azure_auth._CACHE[(TENANT, CLIENT, "digest")] = _azure_auth._CachedToken(
        token="live", expires_at=datetime.now() + timedelta(hours=1)
    )
    config_module.set_azure_client_secret(None)
    assert not _azure_auth._CACHE


def test_saving_a_new_secret_forgets_the_previous_token(monkeypatch):
    from aigauge import config as config_module
    from aigauge.providers import _azure_auth

    monkeypatch.setattr(
        config_module.keyring, "set_password", lambda service, account, value: None
    )
    _azure_auth._CACHE[(TENANT, CLIENT, "digest")] = _azure_auth._CachedToken(
        token="live", expires_at=datetime.now() + timedelta(hours=1)
    )
    config_module.set_azure_client_secret("new-secret")
    assert not _azure_auth._CACHE


@responses.activate
def test_a_401_from_arm_drops_the_cached_token(monkeypatch, config):
    """403 is an RBAC decision made per request; 401 says the bearer is dead,
    and re-presenting it for the rest of its cached lifetime helps nobody."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    responses.add(
        responses.POST, TOKEN_URL, json={"access_token": "tok", "expires_in": 3600}
    )
    responses.add(responses.GET, SUBSCRIPTION_URL, json={}, status=401)
    responses.add(responses.GET, ACCOUNTS_URL, json={}, status=401)
    responses.add(responses.POST, QUERY_URL, json={}, status=401)

    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    from aigauge.providers import _azure_auth

    assert not _azure_auth._CACHE, "the dead bearer is still cached"


@responses.activate
def test_a_403_from_arm_keeps_the_cached_token(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    responses.add(
        responses.POST, TOKEN_URL, json={"access_token": "tok", "expires_in": 3600}
    )
    responses.add(responses.GET, SUBSCRIPTION_URL, json={}, status=403)
    responses.add(responses.GET, ACCOUNTS_URL, json={}, status=403)
    responses.add(responses.POST, QUERY_URL, json={}, status=403)

    _run(az.AzureProvider(config), monkeypatch)
    from aigauge.providers import _azure_auth

    assert _azure_auth._CACHE, "a permission decision is not a dead token"


@responses.activate
def test_changing_the_app_registration_also_drops_the_token(monkeypatch, config):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    provider = az.AzureProvider(config)
    _run(provider, monkeypatch)
    from aigauge.providers import _azure_auth

    assert _azure_auth._CACHE

    config.azure.tenant_id = "44444444-4444-4444-4444-444444444444"
    responses.reset()
    responses.add(
        responses.POST,
        f"https://login.microsoftonline.com/{config.azure.tenant_id}"
        "/oauth2/v2.0/token",
        json={"error": "invalid_client"},
        status=401,
    )
    _run(provider, monkeypatch)
    assert not any(key[0] == TENANT for key in _azure_auth._CACHE)


# --- settings reach the request ---------------------------------------------


@responses.activate
def test_changing_the_reset_day_refetches_instead_of_replaying_the_old_period(
    monkeypatch, config
):
    """_identity detected credential changes only, so every setting that
    shapes the *request* was replayed from the cached aggregate for an hour -
    and a manual Refresh went through the same gate, so it could not force it.

    The cached answer is dropped at once; the *window* is not reopened, so the
    new question is asked at the next one."""
    import json as _json

    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    _run(az.AzureProvider(config), monkeypatch)

    config.azure.reset_day = 15
    responses.reset()
    _stub_everything()
    stale = _run(az.AzureProvider(config), monkeypatch)
    assert stale.status == SnapshotStatus.ERROR, "the old period was replayed"
    az.state_for(SUB).last_fetch_at -= az.MIN_FETCH_INTERVAL
    _run(az.AzureProvider(config), monkeypatch)

    queries = [c for c in responses.calls if "CostManagement/query" in c.request.url]
    assert queries, "the new reset day did not reach a request"
    body = _json.loads(queries[-1].request.body)
    expected_start, _end = az.period_bounds(
        datetime.now(timezone.utc).date(), 15
    )
    assert body["timePeriod"]["from"].startswith(expected_start.isoformat())


@pytest.mark.parametrize(
    "field,value",
    [
        ("resource_group", "rg-ai"),
        ("include_marketplace", True),
        ("foundry_resource_ids", [OPENAI_ID]),
    ],
)
@responses.activate
def test_every_query_shaping_setting_invalidates_the_cache(
    monkeypatch, config, field, value
):
    """Invalidates the answer, not the fetch window: the stale aggregate is
    never served again, and the new question is asked at the next window."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    responses.add(responses.POST, QUERY_URL, json=query_payload([]), status=200)
    _run(az.AzureProvider(config), monkeypatch)
    calls = len(responses.calls)

    setattr(config.azure, field, value)
    stale = _run(az.AzureProvider(config), monkeypatch)
    assert stale.status == SnapshotStatus.ERROR, f"{field} was replayed from the cache"
    assert len(responses.calls) == calls, f"{field} reopened the fetch window"

    az.state_for(SUB).last_fetch_at -= az.MIN_FETCH_INTERVAL
    _run(az.AzureProvider(config), monkeypatch)
    assert len(responses.calls) > calls, f"{field} never reached a request"


@pytest.mark.parametrize("bad", ["evil.example/x", "x/../../y", "", "a?b=c"])
@responses.activate
def test_a_non_guid_id_can_never_reach_a_request_url(monkeypatch, config, bad):
    """Defence in depth: AzureConfig coerces these to None at load, and
    SettingsDialog validates before assigning - but the URL builders trusted
    whatever reached them."""
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    config.azure.subscription_id = bad
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert not responses.calls


def test_the_scope_builder_refuses_a_non_guid_subscription():
    with pytest.raises(ValueError):
        az._scope("evil.example/x")


def test_the_token_endpoint_refuses_a_non_guid_tenant():
    from aigauge.providers import _azure_auth

    with pytest.raises(ValueError):
        _azure_auth.token_endpoint("evil.example/x")


# --- the period is a UTC window --------------------------------------------


_NO_TZSET = not hasattr(time, "tzset")


@pytest.fixture
def _tz(request):
    """Run one test in a named zone, then put the process back.

    The suite runs at TZ=UTC, where every one of these assertions is a
    tautology: the local date *is* the UTC date and the local rendering of a
    UTC midnight *is* that midnight, so the tests passed with the fix reverted.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    try:
        yield request.param
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.skipif(_NO_TZSET, reason="tzset is POSIX-only; CI also runs Windows")
@pytest.mark.parametrize("_tz", ["Pacific/Kiritimati"], indirect=True)
def test_utc_today_is_the_utc_date_not_the_local_one(_tz, monkeypatch):
    """At UTC+14 the local calendar date runs ahead of the UTC one for ten
    hours a day, and Cost Management dates its usage in UTC."""
    fixed = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: N805
            return fixed.astimezone(tz) if tz else fixed.astimezone().replace(
                tzinfo=None
            )

    monkeypatch.setattr(az, "datetime", _Frozen)
    assert _Frozen.now().date() == date(2026, 10, 1), "the fixture proves nothing"
    assert az._utc_today() == date(2026, 9, 30)


@responses.activate
def test_the_query_window_follows_the_utc_date(monkeypatch, config):
    """Cost Management dates are UTC dates. Deriving the period from the local
    calendar date opens the new period up to 13 h early east of UTC, and the
    query then asks for a window that has not started - an empty tile, held by
    the hourly throttle."""
    import json as _json

    monkeypatch.setattr(az, "_utc_today", lambda: date(2026, 9, 30))
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    _run(az.AzureProvider(config), monkeypatch)

    body = _json.loads(
        [c for c in responses.calls if "CostManagement/query" in c.request.url][0]
        .request.body
    )
    assert body["timePeriod"]["from"].startswith("2026-09-01")
    assert body["timePeriod"]["to"].startswith("2026-09-30")


@pytest.mark.skipif(_NO_TZSET, reason="tzset is POSIX-only; CI also runs Windows")
@pytest.mark.parametrize("_tz", ["Pacific/Kiritimati"], indirect=True)
def test_resets_at_is_the_utc_boundary_rendered_locally(_tz):
    """The boundary is a UTC instant; the countdown shows it in local time,
    which is also what Copilot does with the same 1st-of-the-month."""
    snapshot = az.build_snapshot(_aggregate(), AzureConfig(monthly_allowance=150.0))
    assert snapshot.metrics[0].resets_at == datetime(2026, 10, 1, 14, 0)
    assert snapshot.metrics[0].window == timedelta(days=30)


@pytest.mark.skipif(_NO_TZSET, reason="tzset is POSIX-only; CI also runs Windows")
@pytest.mark.parametrize("_tz", ["Etc/GMT+12"], indirect=True)
def test_the_printed_reset_day_agrees_with_the_countdown_west_of_utc(_tz):
    """The label was built from the UTC date and the countdown from its local
    rendering, so at UTC-12 the row said "resets 1 Oct" and counted down to
    30 September."""
    snapshot = az.build_snapshot(_aggregate(), AzureConfig(monthly_allowance=150.0))
    summary = snapshot.metrics[0]
    assert summary.resets_at == datetime(2026, 9, 30, 12, 0)
    assert "resets 30 Sep" in (summary.reset_label or "")


# --- the summary label is a key, so it has to be stable ---------------------


def test_the_summary_label_is_stable_as_spend_changes():
    """history.py keys in-flight periods on provider::label, so a label that
    moves with the money makes a new key on every fetch: the rollover
    comparison never runs, no period is ever closed, and current.json grows
    without bound."""
    first = az.build_snapshot(
        _aggregate(total=36.10), AzureConfig(monthly_allowance=150.0)
    )
    second = az.build_snapshot(
        _aggregate(total=37.01), AzureConfig(monthly_allowance=150.0)
    )
    assert first.metrics[0].label == second.metrics[0].label == "Spend this month"


def test_the_amounts_stay_visible_on_the_row():
    """Moving the money out of the label must not move it out of the tile:
    reset_label is what the row renders inline, next to the bar."""
    snapshot = az.build_snapshot(_aggregate(), AzureConfig(monthly_allowance=150.0))
    summary = snapshot.metrics[0]
    assert summary.reset_label == "CAD 36.10 of 150.00 · resets 1 Oct"


def test_the_row_still_reads_without_an_allowance():
    snapshot = az.build_snapshot(_aggregate(), AzureConfig())
    summary = snapshot.metrics[0]
    assert summary.percent_used is None
    assert summary.reset_label.startswith("CAD 36.10")


# --- wording and counts -----------------------------------------------------


def test_the_other_row_counts_buckets_when_marketplace_is_folded_into_it():
    """The Marketplace row is not a service, so counting it as one mis-states
    what the row holds."""
    rows = [
        [float(10 - i), 20260901, f"{STORAGE_ID}-{i}", f"Service {i}", "CAD"]
        for i in range(8)
    ]
    rows.append([0.5, 20260901, MARKET_ID, "Global resources", "CAD"])
    parsed = az.parse_query_response(query_payload(rows))
    buckets, _f, _m, _s = az.bucket_costs(
        parsed, set(), {(MARKET_ID.lower(), "Global resources"): 0.5}, top_rows=6
    )
    assert buckets[-1][0] == "Other (3 buckets)"


@responses.activate
def test_foundry_resource_count_counts_resources_that_actually_spent(
    monkeypatch, config
):
    monkeypatch.setattr(az, "get_azure_client_secret", lambda: "shhh")
    _stub_everything()
    responses.replace(
        responses.GET,
        ACCOUNTS_URL,
        json={
            "value": [
                {"id": FOUNDRY_ID, "kind": "AIServices"},
                {"id": f"{FOUNDRY_ID}-quiet", "kind": "AIServices"},
            ]
        },
        status=200,
    )
    snapshot = _run(az.AzureProvider(config), monkeypatch)
    assert snapshot.raw["foundry_resource_count"] == 1
    foundry = next(m for m in snapshot.metrics if m.label == az.FOUNDRY_BUCKET)
    assert "1 Foundry resource." in (foundry.note or "")


@responses.activate
def test_an_absurdly_large_budget_is_refused_rather_than_pinning_the_gauge_at_zero():
    """1e308 is finite, so _to_float passes it - and total/1e308*100 rounds to
    zero. AzureConfig caps a typed allowance at the same ceiling."""
    _budgets(_budget(1e308))
    got, _grain, note = az.fetch_budget("tok", SUB, currency="CAD")
    assert got is None
    assert note


def test_a_forecast_below_the_spend_already_recorded_is_not_a_forecast():
    """build_snapshot is re-run over the cached aggregate on every render, so
    the rule has to live there rather than only at fetch time."""
    for forecast in (0.0, -5.0, 10.0):
        snapshot = az.build_snapshot(
            _aggregate(total=36.10, forecast_total=forecast),
            AzureConfig(monthly_allowance=150.0),
        )
        assert not any(
            m.label == "Forecast end of month" for m in snapshot.metrics
        ), forecast


def test_a_forecast_equal_to_the_spend_so_far_is_still_shown():
    """Legitimate at the end of a period, or when nothing more is projected."""
    snapshot = az.build_snapshot(
        _aggregate(total=36.10, forecast_total=36.10),
        AzureConfig(monthly_allowance=150.0),
    )
    assert snapshot.metrics[-1].label == "Forecast end of month"
