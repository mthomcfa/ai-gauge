"""The total-response bound the three REST providers share.

``requests``' ``timeout=`` bounds one socket operation, never the exchange, so
a server that drips a byte just inside it held a ``QThreadPool`` worker for as
long as it liked. These are the tests for the thing that stops it: the clock is
injected, the stream is a fake, and nothing here opens a socket.
"""

from __future__ import annotations

import requests
import pytest
import responses

from aigauge.providers import _http


class _FakeRaw:
    """The urllib3 handle the helper drains.

    ``read1`` is what urllib3 2.x offers and what the helper prefers: it comes
    back with whatever has arrived rather than waiting for a full chunk, which
    is the only reason the deadline can be checked at all against a drip.
    ``stream`` is here too, because ``Response.iter_content`` uses it and the
    legacy path below has to reach it.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.reads = 0
        self.streamed = 0
        self.closed = 0

    def _next(self) -> bytes:
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""

    def read1(self, amt=None, decode_content=None):  # noqa: ARG002
        return self._next()

    def stream(self, chunk_size, decode_content=True):  # noqa: ARG002
        self.streamed += 1
        while True:
            chunk = self._next()
            if not chunk:
                return
            yield chunk

    def close(self):
        self.closed += 1


class _LegacyRaw(_FakeRaw):
    """urllib3 1.x: no ``read1``, so the helper falls back to one byte at a
    time through ``iter_content`` rather than blocking behind a full chunk."""

    read1 = None


class _Clock:
    """A monotonic that advances a fixed step on every read.

    Never starts at zero: a fresh CI runner's ``time.monotonic()`` has a small
    origin, and code that treats the first reading as zero passes here and
    fails there.
    """

    def __init__(self, step: float, start: float = 1234.5):
        self.t = start
        self.step = step
        self.reads = 0

    def __call__(self) -> float:
        now = self.t
        self.reads += 1
        self.t += self.step
        return now


def _fake_response(raw, *, status: int = 200, headers=None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.raw = raw
    response.url = "https://example.invalid/x"
    response.headers.update(headers or {"Content-Type": "application/json"})
    return response


def _patch_request(monkeypatch, response, recorder: list | None = None):
    def fake_request(method, url, **kwargs):
        if recorder is not None:
            recorder.append((method, url, kwargs))
        return response

    monkeypatch.setattr(_http.requests, "request", fake_request)


# --- the deadline ----------------------------------------------------------


def test_a_dripping_body_raises_once_the_deadline_passes(monkeypatch):
    """One byte per chunk with 14 s between them against a 15 s socket timeout
    is what `requests` calls a healthy connection. The deadline is the only
    thing that ends it."""
    raw = _FakeRaw([b"x"] * 100)
    response = _fake_response(raw)
    _patch_request(monkeypatch, response)
    clock = _Clock(step=14.0)

    with pytest.raises(_http.ResponseDeadlineExceeded) as excinfo:
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=30.0,
            monotonic=clock,
        )

    assert raw.closed == 1, "the socket was left open"
    # started, the pre-loop check, then one check per chunk: the third read is
    # the first past 30 s, so exactly two chunks are drained and no more.
    assert raw.reads == 2, raw.reads
    # The message names the bound and nothing else: it reaches snapshot.error,
    # the tile tooltip and ai-gauge.log, none of which redact a URL.
    message = str(excinfo.value)
    assert "30" in message
    assert "http" not in message.lower()
    assert "example.invalid" not in message


def test_a_headers_phase_stall_raises_before_any_chunk_is_read(monkeypatch):
    """A server that stalls in the headers has already spent the budget.

    The check runs before the first read, so the answer does not wait out a
    whole chunk of a body that may never come.
    """
    raw = _FakeRaw([b"x"] * 100)
    _patch_request(monkeypatch, _fake_response(raw))
    # First reading is `started`; the second is the pre-loop check, 99 s later.
    clock = _Clock(step=99.0)

    with pytest.raises(_http.ResponseDeadlineExceeded):
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=30.0,
            monotonic=clock,
        )

    assert raw.reads == 0, "the body was read after the deadline had passed"
    assert raw.closed == 1


def test_a_body_inside_the_deadline_is_returned_whole(monkeypatch):
    raw = _FakeRaw([b'{"a": ', b'1}'])
    _patch_request(monkeypatch, _fake_response(raw))
    clock = _Clock(step=0.5)

    response = _http.bounded_request(
        "GET",
        "https://example.invalid/x",
        timeout=15,
        total_seconds=30.0,
        monotonic=clock,
    )

    assert response.json() == {"a": 1}
    assert raw.closed == 0, "a healthy response must not be closed under the caller"


# --- the size cap ----------------------------------------------------------


def test_a_body_over_the_cap_raises_and_stops_reading(monkeypatch):
    """The read is bounded, not merely refused after the fact: the point is
    that a hostile endpoint cannot make a background worker allocate a
    gigabyte before anyone objects."""
    chunk = b"y" * 16
    raw = _FakeRaw([chunk] * 100)
    _patch_request(monkeypatch, _fake_response(raw))

    with pytest.raises(_http.ResponseTooLarge) as excinfo:
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=30.0,
            max_bytes=64,
            chunk_bytes=16,
        )

    assert raw.closed == 1
    # 64 bytes is four chunks; the fifth is what crosses the cap.
    assert raw.reads == 5, raw.reads
    assert raw.reads * len(chunk) <= 64 + len(chunk), "the read was not bounded"
    assert "http" not in str(excinfo.value).lower()


def test_a_body_exactly_at_the_cap_is_allowed(monkeypatch):
    raw = _FakeRaw([b"z" * 16] * 4)
    _patch_request(monkeypatch, _fake_response(raw))

    response = _http.bounded_request(
        "GET",
        "https://example.invalid/x",
        timeout=15,
        max_bytes=64,
        chunk_bytes=16,
    )

    assert response.content == b"z" * 64


# --- the Response the caller gets back --------------------------------------


@responses.activate
def test_a_helper_returned_response_behaves_like_a_buffered_one():
    """`_content` / `_content_consumed` is a private pair of requests'.

    It is the idiom requests itself uses, and there is no public setter - so
    this test is what makes a requests upgrade that renames either attribute
    fail loudly instead of producing empty tiles.
    """
    responses.add(
        responses.GET,
        "https://example.invalid/ok",
        json={"total_credits": 100.0, "name": "acct"},
        status=200,
        headers={"X-Request-Id": "abc"},
    )

    response = _http.bounded_request(
        "GET", "https://example.invalid/ok", timeout=15
    )

    assert response.status_code == 200
    assert response.json() == {"total_credits": 100.0, "name": "acct"}
    assert response.content == b'{"total_credits": 100.0, "name": "acct"}'
    assert response.text == '{"total_credits": 100.0, "name": "acct"}'
    assert response.headers["X-Request-Id"] == "abc"
    assert response.raise_for_status() is None


@responses.activate
def test_raise_for_status_still_carries_the_response():
    """Copilot and OpenRouter both read `exc.response.status_code` and
    `exc.response.headers` out of the HTTPError to classify a failure."""
    responses.add(
        responses.GET,
        "https://example.invalid/missing",
        json={"message": "Not Found"},
        status=404,
        headers={"x-github-request-id": "req-1"},
    )

    response = _http.bounded_request(
        "GET", "https://example.invalid/missing", timeout=15
    )
    with pytest.raises(requests.HTTPError) as excinfo:
        response.raise_for_status()

    assert excinfo.value.response is response
    assert excinfo.value.response.status_code == 404
    assert excinfo.value.response.headers["x-github-request-id"] == "req-1"


# --- what reaches requests.request ------------------------------------------


def test_redirects_are_refused_by_default(monkeypatch):
    """Every host this app speaks to is fixed and listed in SECURITY.md, so a
    redirect is a failure rather than something to follow."""
    calls: list = []
    _patch_request(monkeypatch, _fake_response(_FakeRaw([b"{}"])), calls)

    _http.bounded_request("GET", "https://example.invalid/x", timeout=15)

    (method, url, kwargs) = calls[0]
    assert method == "GET"
    assert url == "https://example.invalid/x"
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == 15


def test_every_caller_keyword_is_forwarded(monkeypatch):
    """The call sites pass headers, params, json and data; losing one silently
    would send an unauthenticated or unfiltered request."""
    calls: list = []
    _patch_request(monkeypatch, _fake_response(_FakeRaw([b"{}"])), calls)

    _http.bounded_request(
        "POST",
        "https://example.invalid/x",
        timeout=(5, 20),
        headers={"Authorization": "Bearer x"},
        params={"year": 2026},
        json={"body": 1},
        allow_redirects=True,
    )

    kwargs = calls[0][2]
    assert kwargs["headers"] == {"Authorization": "Bearer x"}
    assert kwargs["params"] == {"year": 2026}
    assert kwargs["json"] == {"body": 1}
    assert kwargs["timeout"] == (5, 20)
    assert kwargs["allow_redirects"] is True


# --- how the body is drained -----------------------------------------------


def test_the_body_is_drained_through_read1_when_the_handle_has_it(monkeypatch):
    """`iter_content(chunk_size=N)` blocks until N bytes have arrived - urllib3's
    `stream()` says so - and the per-socket timeout never fires on a server that
    drips inside it. Measured on a loopback server at one byte per 50 ms,
    `iter_content(64 KiB)` never yielded at all; `read1` yielded at once."""
    raw = _FakeRaw([b"{}"])
    _patch_request(monkeypatch, _fake_response(raw))

    response = _http.bounded_request(
        "GET", "https://example.invalid/x", timeout=15
    )

    assert response.content == b"{}"
    assert raw.streamed == 0, "the blocking reader was used"


def test_a_handle_without_read1_still_gets_a_bounded_read(monkeypatch):
    """urllib3 1.x, which `requests>=2.32` still permits. One byte at a time is
    slow (3.9 s per MiB, measured) but it is the only granularity there that
    cannot block behind a drip."""
    raw = _LegacyRaw([b"x"] * 100)
    _patch_request(monkeypatch, _fake_response(raw))
    clock = _Clock(step=14.0)

    with pytest.raises(_http.ResponseDeadlineExceeded):
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=30.0,
            monotonic=clock,
        )

    assert raw.streamed == 1, "the fallback did not reach the stream"
    assert raw.reads == 2, raw.reads
    assert raw.closed == 1


def test_both_failures_are_request_exceptions():
    """Every call site already has an `except requests.RequestException`; that
    is what makes this change one helper rather than a branch per site."""
    assert issubclass(_http.ResponseDeadlineExceeded, requests.RequestException)
    assert issubclass(_http.ResponseTooLarge, requests.RequestException)


# --- the arithmetic ---------------------------------------------------------


def test_the_worst_case_is_derived_from_the_two_bounds():
    """max(connect, total) + read, not connect + read + total + read: the
    clock starts before the call, so the connect and header phases are inside
    the deadline rather than added to it."""
    assert _http.request_worst_case_seconds(15, total_seconds=30.0) == 45.0
    assert _http.request_worst_case_seconds(10, total_seconds=30.0) == 40.0
    assert _http.request_worst_case_seconds((5, 20), total_seconds=30.0) == 50.0
    # A total below the connect timeout cannot make a call shorter than the
    # connect it has to wait out.
    assert _http.request_worst_case_seconds(15, total_seconds=1.0) == 30.0


@pytest.mark.parametrize(
    "module_name, provider_name, timeouts",
    [
        pytest.param(
            "aigauge.providers.copilot",
            "CopilotProvider",
            # Username resolve, credit usage, legacy premium fallback.
            ("USERNAME_TIMEOUT", "USAGE_TIMEOUT", "USAGE_TIMEOUT"),
            id="copilot",
        ),
        pytest.param(
            "aigauge.providers.openrouter",
            "OpenRouterProvider",
            # /credits, /key, /activity.
            ("REQUEST_TIMEOUT", "REQUEST_TIMEOUT", "REQUEST_TIMEOUT"),
            id="openrouter",
        ),
    ],
)
def test_a_rest_providers_refresh_fits_the_budget_it_advertises(
    module_name, provider_name, timeouts
):
    """Computed from the constants on both sides, so re-tuning either one
    fails here rather than arming a watchdog that fires inside a refresh which
    is still within its own bound."""
    import importlib

    from aigauge.app import _REST_REFRESH_BUDGET_SECONDS, _refresh_budget_seconds

    module = importlib.import_module(module_name)
    worst = sum(
        _http.request_worst_case_seconds(getattr(module, name)) for name in timeouts
    )
    assert module.REFRESH_WORST_CASE_SECONDS == worst
    provider = getattr(module, provider_name)
    assert _refresh_budget_seconds(provider) >= worst
    # And the reason it has to declare one at all: three bounded calls do not
    # fit app.py's flat default.
    assert worst > _REST_REFRESH_BUDGET_SECONDS


def test_azures_refresh_worst_case_counts_whole_calls():
    """Its page loops have their own deadline; the fixed handful around them
    does not, so the watchdog budget is that deadline plus a whole call each -
    a call, not the per-socket timeout it used to multiply by."""
    from aigauge.app import _refresh_budget_seconds
    from aigauge.providers import azure as az

    assert az.REQUEST_WORST_CASE_SECONDS == _http.request_worst_case_seconds(
        az.REQUEST_TIMEOUT
    )
    assert az.REFRESH_WORST_CASE_SECONDS == (
        az.REFRESH_DEADLINE_SECONDS
        + (az.MAX_FIXED_REQUESTS_PER_REFRESH + 1) * az.REQUEST_WORST_CASE_SECONDS
    )
    assert _refresh_budget_seconds(az.AzureProvider) == az.REFRESH_WORST_CASE_SECONDS


def test_the_entra_token_call_is_bounded_like_an_arm_call():
    """`get_token` is one of the fixed calls azure's worst case counts, so its
    timeout has to be the one that count assumes."""
    from aigauge.providers import _azure_auth
    from aigauge.providers import azure as az

    assert _azure_auth.REQUEST_TIMEOUT == az.REQUEST_TIMEOUT
