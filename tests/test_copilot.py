from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses

from aigauge.config import Config
from aigauge.models import SnapshotStatus
from aigauge.providers.copilot import (
    COPILOT_PRODUCT,
    GITHUB_API,
    GITHUB_API_VERSION,
    _build_snapshot,
    _next_month_start_utc,
    _this_month_start_utc,
)


def test_next_month_start_utc_normal():
    d = _next_month_start_utc(datetime(2026, 4, 27, tzinfo=timezone.utc))
    assert d == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert d.tzinfo == timezone.utc


def test_next_month_start_utc_december_rolls_year():
    d = _next_month_start_utc(datetime(2026, 12, 15, tzinfo=timezone.utc))
    assert d == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert d.tzinfo == timezone.utc


def test_this_month_start_utc():
    d = _this_month_start_utc(datetime(2026, 4, 27, tzinfo=timezone.utc))
    assert d == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert d.tzinfo == timezone.utc


def test_build_snapshot_uses_utc_month_boundary(monkeypatch):
    import aigauge.providers.copilot as copilot

    real_datetime = datetime
    now = real_datetime(2026, 4, 30, 23, 30, tzinfo=timezone.utc)

    class FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            if tz is timezone.utc:
                return now
            return now.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(copilot, "datetime", FrozenDatetime)

    snap = _build_snapshot({"usageItems": []}, quota=1500)
    metric = snap.metrics[0]
    next_utc = _next_month_start_utc(now)
    assert metric.resets_at == next_utc.astimezone().replace(tzinfo=None)
    assert metric.resets_at - FrozenDatetime.now() < timedelta(hours=1)
    assert metric.window == next_utc - _this_month_start_utc(now)


def test_build_snapshot_sums_ai_credit_quantity_for_included_allowance():
    payload = {
        "usageItems": [
            {
                "product": "copilot",
                "sku": "copilot_ai_credits",
                "unitType": "credits",
                "netQuantity": 0,
                "grossQuantity": 50,
            },
            {
                "product": "copilot",
                "sku": "copilot_ai_credits",
                "unitType": "credits",
                "netQuantity": 0,
                "grossQuantity": 20,
            },
        ]
    }
    snap = _build_snapshot(payload, quota=1500)
    assert snap.status == SnapshotStatus.OK
    assert len(snap.metrics) == 1
    m = snap.metrics[0]
    assert m.percent_used == pytest.approx(70 / 1500 * 100)
    assert "70/1500" in m.label
    assert m.resets_at is not None
    assert m.window is not None and timedelta(days=28) <= m.window <= timedelta(days=31)


def test_build_snapshot_falls_back_to_credit_amount():
    payload = {
        "usageItems": [
            {
                "product": "copilot",
                "sku": "Copilot AI credits",
                "unitType": "AI credits",
                "grossAmount": 0.12,
            }
        ]
    }
    snap = _build_snapshot(payload, quota=1500)
    assert snap.metrics[0].percent_used == pytest.approx(12 / 1500 * 100)


def test_build_snapshot_accepts_github_ai_unit_sku():
    payload = {
        "usageItems": [
            {
                "product": "Copilot",
                "sku": "copilot_ai_unit",
                "unitType": "ai-units",
                "grossQuantity": 68.5548,
                "grossAmount": 0.685548,
                "netQuantity": 0.0,
            }
        ]
    }
    snap = _build_snapshot(payload, quota=1500)
    metric = snap.metrics[0]
    assert metric.percent_used == pytest.approx(68.5548 / 1500 * 100)
    assert "68.6/1500" in metric.label


def test_build_snapshot_handles_empty_usage():
    snap = _build_snapshot({"usageItems": []}, quota=1500)
    assert snap.status == SnapshotStatus.OK
    assert snap.metrics[0].percent_used == 0.0
    assert snap.metrics[0].note is not None


def test_build_snapshot_ignores_non_credit_items():
    snap = _build_snapshot(
        {"usageItems": [{"product": "copilot", "sku": "copilot_standalone"}]},
        quota=1500,
    )
    assert snap.metrics[0].percent_used == 0.0


@responses.activate
def test_fetch_credit_usage_calls_current_endpoint():
    from aigauge.providers.copilot import _fetch_credit_usage

    responses.add(
        responses.GET,
        f"{GITHUB_API}/users/octocat/settings/billing/usage/summary",
        json={"usageItems": [], "user": "octocat"},
        status=200,
    )
    result = _fetch_credit_usage("ghp_test", "octocat")
    assert result["user"] == "octocat"
    call = responses.calls[0]
    assert "Bearer ghp_test" in call.request.headers["Authorization"]
    assert call.request.headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION
    assert "year=" in call.request.url
    assert "month=" in call.request.url
    assert f"product={COPILOT_PRODUCT}" in call.request.url


@responses.activate
def test_fetch_premium_usage_calls_correct_endpoint():
    from aigauge.providers.copilot import _fetch_premium_usage

    responses.add(
        responses.GET,
        f"{GITHUB_API}/users/octocat/settings/billing/premium_request/usage",
        json={"usageItems": [], "user": "octocat"},
        status=200,
    )
    result = _fetch_premium_usage("ghp_test", "octocat")
    assert result["user"] == "octocat"
    call = responses.calls[0]
    assert "Bearer ghp_test" in call.request.headers["Authorization"]
    assert call.request.headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION
    assert "year=" in call.request.url
    assert "month=" in call.request.url


@responses.activate
def test_fetch_org_premium_usage_calls_correct_endpoint():
    from aigauge.providers.copilot import _fetch_org_premium_usage

    responses.add(
        responses.GET,
        f"{GITHUB_API}/organizations/my-org/settings/billing/premium_request/usage",
        json={"usageItems": [], "organization": "my-org"},
        status=200,
    )
    result = _fetch_org_premium_usage("ghp_test", "my-org", "octocat")
    assert result["organization"] == "my-org"
    call = responses.calls[0]
    assert "Bearer ghp_test" in call.request.headers["Authorization"]
    assert call.request.headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION
    assert "year=" in call.request.url
    assert "month=" in call.request.url
    assert "user=octocat" in call.request.url


@responses.activate
def test_resolve_username_from_pat():
    from aigauge.providers.copilot import _resolve_username

    responses.add(
        responses.GET,
        f"{GITHUB_API}/user",
        json={"login": "octocat"},
        status=200,
    )
    assert _resolve_username("ghp_test", configured=None) == "octocat"


def test_resolve_username_uses_configured_if_set():
    from aigauge.providers.copilot import _resolve_username

    # No HTTP call should happen — configured value short-circuits.
    assert _resolve_username("anything", configured="myname") == "myname"


def test_config_smoke():
    # Ensures provider class can be constructed
    from aigauge.providers.copilot import CopilotProvider

    cfg = Config()
    # Don't actually call refresh — needs Qt event loop.
    provider = CopilotProvider(cfg, pool=None)
    assert provider.name == "copilot"


# --- the total-response deadline -------------------------------------------


class _InlinePool:
    """A QThreadPool double that runs the runnable on this thread.

    The real `_Worker.run` - and with it the blanket handler that turns an
    unexpected exception into an ERROR snapshot - is otherwise never executed
    by the suite.
    """

    def __init__(self):
        self.started = 0

    def start(self, runnable):
        self.started += 1
        runnable.run()


def test_a_response_deadline_ends_the_refresh_as_an_error(monkeypatch, caplog):
    """A dripping GitHub endpoint used to hold a pool slot forever.

    It now fails like any other transport failure: an ERROR tile, not
    AUTH_REQUIRED - the PAT is fine - and one call to `on_done`.
    """
    import logging

    import aigauge.providers.copilot as copilot_mod
    from aigauge.providers._http import ResponseDeadlineExceeded
    from aigauge.providers.copilot import CopilotProvider

    monkeypatch.setattr(copilot_mod, "get_github_pat", lambda: "ghp_test")

    def boom(*args, **kwargs):
        raise ResponseDeadlineExceeded("Response deadline exceeded (30s).")

    monkeypatch.setattr(copilot_mod, "bounded_request", boom)

    cfg = Config()
    # Configured, so the failure lands on the usage read rather than on the
    # username resolve - which answers AUTH_REQUIRED for any transport error
    # by design.
    cfg.copilot.username = "octocat"
    captured: list = []
    pool = _InlinePool()
    with caplog.at_level(logging.INFO, logger="aigauge"):
        CopilotProvider(cfg, pool=pool).refresh(captured.append)

    assert pool.started == 1
    assert len(captured) == 1, "on_done was not called exactly once"
    snap = captured[0]
    assert snap.status == SnapshotStatus.ERROR
    assert "deadline" in (snap.error or "").lower()
    # The string reaches the tile tooltip, which does not redact.
    assert "api.github.com" not in (snap.error or "")

    line = next(
        record.getMessage()
        for record in caplog.records
        if "classification=request_failed" in record.getMessage()
    )
    # The type name, never the message: this record goes to the file users are
    # invited to attach to a bug report. The branch is `work()`'s own named
    # one - before this release the deadline fell through to the worker's
    # blanket handler, which logged a traceback.
    assert "type=ResponseDeadlineExceeded" in line
    assert "deadline exceeded (30s)" not in line
    assert not any(
        record.exc_info for record in caplog.records
    ), "a traceback whose last line is the exception message"


@responses.activate
def test_an_offline_failure_keeps_the_github_username_out_of_every_sink(caplog):
    """The plainest failure there is: the user's network is down.

    `work()` caught only `requests.HTTPError` - a reply GitHub actually sent -
    so this fell to the worker's blanket handler, which did `log.exception`
    and `error=str(exc)`. A `requests` connection error carries the URL it
    failed on, and a Copilot usage URL carries the username as a path
    segment, so going offline put an account identifier in the log, on the
    tile and in Copy diagnostics. SECURITY.md says it never reaches them.
    """
    import logging

    import aigauge.providers.copilot as copilot_mod
    from aigauge.error_dialog import _format_diagnostics
    from aigauge.providers.copilot import CopilotProvider

    username = "octocat"
    pat = "ghp_" + "A" * 36
    url = (
        f"{GITHUB_API}/users/{username}/settings/billing/usage/summary"
    )
    responses.add(
        responses.GET,
        url,
        body=requests.ConnectionError(
            f"HTTPSConnectionPool(host='api.github.com', port=443): Max "
            f"retries exceeded with url: /users/{username}/settings/billing"
            f"/usage/summary (Caused by NameResolutionError(...))"
        ),
    )

    cfg = Config()
    cfg.copilot.username = username
    captured: list = []
    with caplog.at_level(logging.DEBUG, logger="aigauge"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(copilot_mod, "get_github_pat", lambda: pat)
            CopilotProvider(cfg, pool=_InlinePool()).refresh(captured.append)

    snapshot = captured[0]
    assert snapshot.status == SnapshotStatus.ERROR
    # A `requests` exception, so the type name and nothing else: its own
    # message is the URL it failed on.
    assert snapshot.error == "GitHub request failed (ConnectionError)."
    diagnostics = _format_diagnostics("copilot", snapshot)
    for sink in (snapshot.error or "", caplog.text, diagnostics):
        assert username not in sink
        assert pat not in sink
        assert "api.github.com" not in sink
    assert "classification=request_failed type=ConnectionError" in caplog.text


@responses.activate
def test_a_redirect_on_the_username_path_is_an_error_not_a_bad_pat(caplog):
    """GitHub answers a renamed user or org with a 301, and this app does not
    follow one. `_resolve_username` answering `None` for that would put "PAT
    may lack read:user" on the tile - a wrong diagnosis of a credential that
    is fine - so the redirect is left to `work()`'s branch."""
    import logging

    import aigauge.providers.copilot as copilot_mod
    from aigauge.providers.copilot import CopilotProvider

    responses.add(
        responses.GET,
        f"{GITHUB_API}/user",
        body="",
        status=301,
        headers={"Location": "https://api.github.invalid/user/renamed"},
    )

    captured: list = []
    with caplog.at_level(logging.DEBUG, logger="aigauge"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(copilot_mod, "get_github_pat", lambda: "ghp_test")
            CopilotProvider(Config(), pool=_InlinePool()).refresh(captured.append)

    snapshot = captured[0]
    assert snapshot.status == SnapshotStatus.ERROR, snapshot.error
    assert snapshot.status != SnapshotStatus.AUTH_REQUIRED
    assert snapshot.error == (
        "GitHub request failed: The endpoint redirected (301); "
        "this app does not follow redirects."
    )
    for sink in (snapshot.error or "", caplog.text):
        assert "api.github.invalid" not in sink
        assert "renamed" not in sink


@responses.activate
def test_a_truncated_reply_on_the_username_path_is_an_error_not_a_bad_pat():
    """A reply cut inside the header block is a 200 with no body.

    `http.client` reads EOF as the end of the headers, so a server or a
    middlebox that drops the connection there produces an ordinary 200 whose
    body is empty - measured against a loopback server, on this release and
    on the one before it. `.json()` then raises
    `requests.exceptions.JSONDecodeError`, which is a `RequestException`, so
    `_resolve_username` answered `None` and the tile read "PAT may lack
    read:user": the same wrong diagnosis the redirect fix removed, reached by
    a different route. It is left to `work()`'s branch, which says what
    happened; a real refusal (a non-200) is still `None`.
    """
    import aigauge.providers.copilot as copilot_mod
    from aigauge.providers.copilot import CopilotProvider

    responses.add(responses.GET, f"{GITHUB_API}/user", body="", status=200)

    captured: list = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(copilot_mod, "get_github_pat", lambda: "ghp_test")
        CopilotProvider(Config(), pool=_InlinePool()).refresh(captured.append)

    snapshot = captured[0]
    assert snapshot.status == SnapshotStatus.ERROR, snapshot.error
    assert snapshot.status != SnapshotStatus.AUTH_REQUIRED
    assert snapshot.error == "GitHub request failed (JSONDecodeError)."


@responses.activate
def test_a_refusal_on_the_username_path_is_still_a_pat_diagnosis():
    """The other half: a 401 is GitHub saying no, which is exactly what
    "PAT may lack read:user" is for."""
    import aigauge.providers.copilot as copilot_mod
    from aigauge.providers.copilot import CopilotProvider

    responses.add(responses.GET, f"{GITHUB_API}/user", json={}, status=401)

    captured: list = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(copilot_mod, "get_github_pat", lambda: "ghp_test")
        CopilotProvider(Config(), pool=_InlinePool()).refresh(captured.append)

    assert captured[0].status == SnapshotStatus.AUTH_REQUIRED
    assert "read:user" in (captured[0].error or "")


@responses.activate
def test_a_redirect_on_the_usage_path_is_reported_as_a_redirect():
    """The other half: before this release a 301 here reached `.json()` and
    the tile read "Expecting value: line 1 column 1 (char 0)"."""
    import aigauge.providers.copilot as copilot_mod
    from aigauge.providers.copilot import CopilotProvider

    responses.add(
        responses.GET,
        f"{GITHUB_API}/users/octocat/settings/billing/usage/summary",
        body="",
        status=301,
        headers={"Location": "https://api.github.invalid/elsewhere"},
    )

    cfg = Config()
    cfg.copilot.username = "octocat"
    captured: list = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(copilot_mod, "get_github_pat", lambda: "ghp_test")
        CopilotProvider(cfg, pool=_InlinePool()).refresh(captured.append)

    assert captured[0].status == SnapshotStatus.ERROR
    # The helper's own message, not its type name: it is built from the
    # status and carries neither the URL nor the `Location`, which is what
    # the type-name rule exists to keep off the tile.
    assert captured[0].error == (
        "GitHub request failed: The endpoint redirected (301); "
        "this app does not follow redirects."
    )
    assert "api.github" not in (captured[0].error or "")


def test_an_exception_that_is_not_a_request_failure_names_its_type_too(caplog):
    """The blanket handler is the last resort, not the usual path, and it
    reported `str(exc)` - which for a transport failure is the URL. It says
    the type now, like azure's, and logs no traceback."""
    import logging

    import aigauge.providers.copilot as copilot_mod
    from aigauge.providers.copilot import CopilotProvider

    caplog.set_level(logging.DEBUG, logger="aigauge")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(copilot_mod, "get_github_pat", lambda: "ghp_test")
        patch.setattr(
            copilot_mod,
            "_resolve_username",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("secret-bearing message")
            ),
        )
        captured: list = []
        CopilotProvider(Config(), pool=_InlinePool()).refresh(captured.append)

    assert captured[0].status == SnapshotStatus.ERROR
    assert captured[0].error == "Copilot refresh failed (RuntimeError)."
    assert "classification=unexpected_exception type=RuntimeError" in caplog.text
    assert "secret-bearing message" not in caplog.text
    assert not any(
        record.exc_info for record in caplog.records
    ), "a traceback whose last line is the exception message"


def _username_rows():
    """The failure classes a refresh can meet on `/user`.

    An int is a reply GitHub sent; anything else is raised by the transport
    instead of answering.
    """
    from aigauge.providers._http import (
        ResponseDeadlineExceeded,
        ResponseEncodingRefused,
        ResponseRedirected,
        ResponseTooLarge,
    )

    refused = "Could not resolve GitHub username (PAT may lack read:user)."
    return [
        pytest.param(
            ResponseDeadlineExceeded("Response deadline exceeded (30s)."),
            SnapshotStatus.ERROR,
            "GitHub request failed: Response deadline exceeded (30s).",
            id="deadline",
        ),
        pytest.param(
            ResponseTooLarge("Response exceeded the 8388608-byte limit."),
            SnapshotStatus.ERROR,
            "GitHub request failed: Response exceeded the 8388608-byte limit.",
            id="large",
        ),
        pytest.param(
            ResponseEncodingRefused(
                "Response declared 2 content encodings; this app reads at most one."
            ),
            SnapshotStatus.ERROR,
            "GitHub request failed: Response declared 2 content encodings; "
            "this app reads at most one.",
            id="coding",
        ),
        pytest.param(
            ResponseRedirected(
                "The endpoint redirected (301); this app does not follow redirects."
            ),
            SnapshotStatus.ERROR,
            "GitHub request failed: The endpoint redirected (301); "
            "this app does not follow redirects.",
            id="redirect",
        ),
        pytest.param(
            requests.exceptions.InvalidJSONError("not json"),
            SnapshotStatus.ERROR,
            "GitHub request failed (InvalidJSONError).",
            id="json",
        ),
        pytest.param(
            requests.ConnectionError("offline"),
            SnapshotStatus.ERROR,
            "GitHub request failed (ConnectionError).",
            id="offline",
        ),
        pytest.param(
            requests.ReadTimeout("slow"),
            SnapshotStatus.ERROR,
            "GitHub request failed (ReadTimeout).",
            id="timeout",
        ),
        pytest.param(401, SnapshotStatus.AUTH_REQUIRED, refused, id="401"),
        pytest.param(403, SnapshotStatus.AUTH_REQUIRED, refused, id="403"),
        pytest.param(
            500,
            SnapshotStatus.ERROR,
            "GitHub request failed (HTTPError).",
            id="500",
        ),
    ]


@responses.activate
@pytest.mark.parametrize("failure, status, message", _username_rows())
def test_the_username_path_blames_the_pat_only_when_github_refused_it(
    monkeypatch, failure, status, message
):
    """One rule, not a list of exceptions.

    "PAT may lack read:user" is a diagnosis of a credential, so it is the
    answer for a reply that refuses one - a 401 or a 403 - and for nothing
    else. Every other failure leaves the question unanswered: a deadline, an
    oversized reply, a refused encoding, a redirect, a reply that was not
    JSON, a 500 and an ordinary offline machine all used to arrive on the
    tile as the same wrong advice, which is to re-issue a PAT that is fine.
    """
    import aigauge.providers.copilot as copilot_mod
    from aigauge.providers.copilot import CopilotProvider

    if isinstance(failure, int):
        responses.add(
            responses.GET, f"{GITHUB_API}/user", json={}, status=failure
        )
    else:

        def boom(*args, **kwargs):
            raise failure

        monkeypatch.setattr(copilot_mod, "bounded_request", boom)

    captured: list = []
    monkeypatch.setattr(copilot_mod, "get_github_pat", lambda: "ghp_test")
    CopilotProvider(Config(), pool=_InlinePool()).refresh(captured.append)

    snapshot = captured[0]
    assert snapshot.status == status, snapshot.error
    assert snapshot.error == message
    # The four the helper raises carry a status, a count or a bound; the rest
    # are reported by type name, because a `requests` message is the URL it
    # failed on and a Copilot URL carries the account name.
    assert "api.github.com" not in (snapshot.error or "")


def test_copilot_advertises_the_budget_its_three_calls_need():
    from aigauge.providers._http import request_worst_case_seconds
    from aigauge.providers.copilot import (
        REFRESH_WORST_CASE_SECONDS,
        USAGE_TIMEOUT,
        USERNAME_TIMEOUT,
        CopilotProvider,
    )

    assert CopilotProvider.refresh_budget_seconds == REFRESH_WORST_CASE_SECONDS
    assert REFRESH_WORST_CASE_SECONDS == request_worst_case_seconds(
        USERNAME_TIMEOUT
    ) + 2 * request_worst_case_seconds(USAGE_TIMEOUT)
