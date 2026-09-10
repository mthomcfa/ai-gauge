"""Azure month-to-date spend provider.

Fixtures are shaped exactly like the documented REST responses - a
``properties.columns`` array of {name,type} and a ``properties.rows`` array of
positional lists - because the whole parser is positional lookups against that
column map. A fixture written as a convenient dict would test nothing.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

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
    assert "JPY 10.00" in snapshot.metrics[0].label
    assert "$" not in snapshot.metrics[0].label


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


def test_parse_query_response_tolerates_a_missing_cost_column():
    parsed = az.parse_query_response({"properties": {"columns": [], "rows": [[1]]}})
    assert parsed.total == 0.0
    assert parsed.row_count == 0


def test_cost_column_falls_back_to_the_first_numeric_non_date_column():
    columns = [
        {"name": "UsageDate", "type": "Number"},
        {"name": "SomeFutureCostName", "type": "Number"},
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
    assert summary.resets_at == datetime(2026, 10, 1)
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
    assert "CAD 36.10" in summary.label
    assert "Set a monthly allowance" in (summary.note or "")


def test_an_azure_budget_beats_the_settings_allowance():
    snapshot = az.build_snapshot(
        _aggregate(budget_amount=200.0, budget_time_grain="Monthly"),
        AzureConfig(monthly_allowance=150.0),
    )
    assert snapshot.metrics[0].percent_used == pytest.approx(18.05, abs=0.01)
    assert "Azure Budget" in (snapshot.metrics[0].note or "")


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
    payload = query_payload([[1.0, 20260901, STORAGE_ID, "Storage", "CAD"]])
    payload["properties"]["nextLink"] = f"{QUERY_URL}?$skiptoken=forever"
    responses.add(responses.POST, QUERY_URL, json=payload, status=200)

    parsed, _metric = az.fetch_query("tok", SUB, date(2026, 9, 1), date(2026, 10, 1))
    assert len(responses.calls) == az.MAX_QUERY_PAGES
    assert parsed.truncated is True


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
    assert code.startswith("invalid_client")
    assert len(code) <= 64
    assert "\n" not in code and "<b>" not in code and " " not in code
    message = str(exc.value)
    assert "\n" not in message and "<b>" not in message


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
