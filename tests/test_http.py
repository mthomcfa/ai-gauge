"""The total-response bound the three REST providers share.

``requests``' ``timeout=`` bounds one socket operation, never the exchange, so
a server that drips a byte just inside it held a ``QThreadPool`` worker for as
long as it liked. These are the tests for the thing that stops it: the clock is
injected, the stream is a fake, and nothing here opens a socket.
"""

from __future__ import annotations

import socket
import threading

import requests
import pytest
import responses
from urllib3.exceptions import ProtocolError

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
    """Inject the transport at ``Session.request``.

    The helper builds a ``Session`` of its own for every call - that is where
    the deadline's adapter is mounted - so this is the seam, and it is one
    level above the adapter on purpose: what the call sites depend on is the
    method, the URL and the keywords, and those are still plain arguments
    here rather than a built ``PreparedRequest``.
    """

    def fake_request(session, method, url, **kwargs):
        if recorder is not None:
            recorder.append((method, url, kwargs))
        return response

    monkeypatch.setattr(_http.requests.Session, "request", fake_request)


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


# --- the out-of-band half of the deadline -----------------------------------
#
# Nothing inside `requests` or urllib3 bounds an exchange: a per-socket read
# timeout is reset by every byte that arrives, so a server dripping a response
# header - or a Content-Encoding stream whose bytes decode to nothing - held a
# worker for as long as it liked while this helper's clock was never reached.
# The answer is a timer that shuts the socket down from another thread. The
# suite opens no socket, so the socket here is a fake; everything around it -
# the timer, the adapter, the pool hook, the mapping - is the shipped wiring.


class _FakeSocket:
    def __init__(self, error: OSError | None = None):
        self.shutdowns: list = []
        self._error = error

    def shutdown(self, how):
        self.shutdowns.append(how)
        if self._error is not None:
            raise self._error


class _FakeConnection:
    def __init__(self, sock=None):
        self.sock = sock


class _FakePool:
    """The urllib3 pool the adapter hooks: `_get_conn` is the one place a
    connection passes through, new or reused."""

    def __init__(self, connection):
        self._connection = connection
        self.calls = 0

    def _get_conn(self, timeout=None):  # noqa: ARG002
        self.calls += 1
        return self._connection


def _patch_timer(monkeypatch, events: list | None = None) -> list:
    """Replace `threading.Timer` with a double that records and never fires."""
    made: list = []

    class _Timer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False
            self.started = False
            self.cancelled = False
            made.append(self)

        def start(self):
            self.started = True
            if events is not None:
                events.append("armed")

        def cancel(self):
            self.cancelled = True
            if events is not None:
                events.append("cancelled")

    monkeypatch.setattr(_http.threading, "Timer", _Timer)
    return made


def test_the_deadline_timer_is_armed_before_the_request_and_covers_the_body(
    monkeypatch,
):
    """Armed before, cancelled after - and after the *body*, not after the
    headers.

    A stall inside one read is the whole reason this exists, and a
    `Content-Encoding` stream that decodes to nothing stalls in the body
    phase, so a timer cancelled when `requests.request` returns would bound
    the headers and nothing else.
    """
    events: list = []
    made = _patch_timer(monkeypatch, events)

    class _NotingRaw(_FakeRaw):
        def read1(self, amt=None, decode_content=None):  # noqa: ARG002
            events.append("read")
            return self._next()

    def fake_request(session, method, url, **kwargs):  # noqa: ARG001
        events.append("request")
        return _fake_response(_NotingRaw([b"{", b"}"]))

    monkeypatch.setattr(_http.requests.Session, "request", fake_request)

    _http.bounded_request(
        "GET", "https://example.invalid/x", timeout=15, total_seconds=30.0
    )

    assert events == ["armed", "request", "read", "read", "read", "cancelled"]
    assert len(made) == 1
    assert made[0].interval == 30.0
    assert made[0].daemon, "a live timer would hold the process open at exit"
    assert made[0].cancelled


def test_the_timer_does_not_outlive_the_call(monkeypatch):
    """The real `threading.Timer`, so this is the thread itself and not a
    double: a per-call timer left running would be a thread per REST call."""
    made: list = []
    real = _http.threading.Timer

    class _Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(_http.threading, "Timer", _Spy)
    _patch_request(monkeypatch, _fake_response(_FakeRaw([b"{}"])))

    _http.bounded_request("GET", "https://example.invalid/x", timeout=15)

    assert len(made) == 1
    assert made[0].finished.is_set(), "the deadline timer was left armed"
    made[0].join(5)
    assert not made[0].is_alive()


def test_the_deadline_shuts_the_recorded_socket_down():
    """`shutdown(SHUT_RDWR)` is the one thing that reaches a thread blocked in
    `recv`; it is documented as unblocking a blocking read on POSIX and on
    Windows alike, and the loopback proof is in the review drivers."""
    deadline = _http._DeadlineShutdown(30.0)
    sock = _FakeSocket()
    deadline.record(_FakeConnection(sock))

    deadline._fire()

    assert deadline.fired is True
    assert sock.shutdowns == [socket.SHUT_RDWR]


@pytest.mark.parametrize(
    "connection",
    [
        pytest.param(None, id="none"),
        pytest.param(_FakeConnection(None), id="no sock"),
        pytest.param(_FakeConnection(_FakeSocket(OSError("gone"))), id="raises"),
    ],
)
def test_the_deadline_fires_cleanly_with_nothing_to_shut_down(connection):
    """A timer that fires during DNS or connect finds no socket; one that
    fires on a socket the peer already closed gets an OSError. Neither is a
    crash in a background thread, and the connect timeout bounds that phase."""
    deadline = _http._DeadlineShutdown(30.0)
    if connection is not None:
        deadline.record(connection)

    deadline._fire()

    assert deadline.fired is True


def test_the_pool_hook_records_the_connection_once():
    """urllib3 offers no callback for "the connection this request is about to
    use", so the hook is `_get_conn` on the pool - patched on the instance,
    which is built for one call and thrown away with it."""
    deadline = _http._DeadlineShutdown(30.0)
    connection = _FakeConnection(_FakeSocket())
    pool = _FakePool(connection)

    _http._watch_pool(pool, deadline)
    _http._watch_pool(pool, deadline)
    assert pool._get_conn() is connection

    assert pool.calls == 1, "the pool was wrapped twice"
    deadline._fire()
    assert connection.sock.shutdowns == [socket.SHUT_RDWR]


def test_every_call_gets_its_own_session_on_the_deadlines_adapter(monkeypatch):
    """`requests.request` opened and discarded a Session per call, so no pool,
    cookie jar or connection survived a refresh. That property is kept - the
    Session is here only because a mounted adapter is the one way to learn
    which socket to shut down."""
    seen: list = []

    def fake_request(session, method, url, **kwargs):  # noqa: ARG001
        seen.append(session)
        return _fake_response(_FakeRaw([b"{}"]))

    monkeypatch.setattr(_http.requests.Session, "request", fake_request)

    _http.bounded_request("GET", "https://example.invalid/x", timeout=15)
    _http.bounded_request("GET", "https://example.invalid/x", timeout=15)

    assert seen[0] is not seen[1], "a Session was kept across calls"
    for scheme in ("http://", "https://"):
        adapter = seen[0].get_adapter(f"{scheme}example.invalid/x")
        assert isinstance(adapter, _http._ConnectionRecordingAdapter)


def test_a_read_the_timer_cuts_is_reported_as_the_deadline(monkeypatch):
    """The whole wiring, end to end, with a fake socket instead of a real one.

    The injected clock never advances, so nothing in-band can raise: the only
    way out of this call is the timer firing, shutting the socket down and the
    blocked read failing because of it. A `ChunkedEncodingError` here would
    tell the call site the endpoint broke, when in fact this app gave up.
    """
    released = threading.Event()

    class _CutSocket:
        def __init__(self):
            self.shutdowns: list = []

        def shutdown(self, how):
            self.shutdowns.append(how)
            released.set()

    class _BlockingRaw(_FakeRaw):
        def read1(self, amt=None, decode_content=None):  # noqa: ARG002
            # What a blocking recv inside urllib3 does: it ends when the
            # socket is shut down under it, and then the read fails.
            assert released.wait(10), "the deadline timer never fired"
            raise ProtocolError("Connection broken: IncompleteRead(0 bytes read)")

    sock = _CutSocket()
    response = _fake_response(_BlockingRaw([]))

    def fake_request(session, method, url, **kwargs):  # noqa: ARG001
        session.get_adapter(url)._deadline.record(_FakeConnection(sock))
        return response

    monkeypatch.setattr(_http.requests.Session, "request", fake_request)

    with pytest.raises(_http.ResponseDeadlineExceeded) as excinfo:
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=0.05,
            monotonic=lambda: 1234.5,
        )

    assert sock.shutdowns == [socket.SHUT_RDWR]
    assert isinstance(
        excinfo.value.__cause__, requests.exceptions.ChunkedEncodingError
    ), "the read's own failure is not chained"
    assert response.raw.closed == 1
    assert "http" not in str(excinfo.value).lower()


def test_a_response_the_timer_cut_short_is_the_deadline_not_an_empty_body(
    monkeypatch,
):
    """What a cut header block really produces: a perfectly ordinary 200.

    The shutdown ends `http.client`'s header read, so `requests` returns a
    response with whatever headers arrived and no body at all - measured on a
    loopback server. Handing that back would put an empty tile where a
    transport failure belongs, and no in-band clock can see it: the injected
    one here never advances, so the only witness is the timer's own flag.
    """
    released = threading.Event()

    class _CutSocket:
        def shutdown(self, how):  # noqa: ARG002
            released.set()

    raw = _FakeRaw([])

    def fake_request(session, method, url, **kwargs):  # noqa: ARG001
        session.get_adapter(url)._deadline.record(_FakeConnection(_CutSocket()))
        assert released.wait(10), "the deadline timer never fired"
        return _fake_response(raw)

    monkeypatch.setattr(_http.requests.Session, "request", fake_request)

    with pytest.raises(_http.ResponseDeadlineExceeded):
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=0.05,
            monotonic=lambda: 1234.5,
        )

    assert raw.reads == 0, "a body was read after the deadline had passed"
    assert raw.closed == 1


@pytest.mark.parametrize(
    "step, expected",
    [
        pytest.param(1.0, requests.exceptions.ChunkedEncodingError, id="before"),
        pytest.param(20.0, _http.ResponseDeadlineExceeded, id="after"),
    ],
)
def test_a_mid_stream_failure_is_the_deadline_only_after_the_deadline(
    monkeypatch, step, expected
):
    """A broken read past the bound is this app giving up; the same break
    inside the bound is the endpoint, and has to keep requests' own name so
    the call sites classify it as they always did."""

    class _BrokenRaw(_FakeRaw):
        def read1(self, amt=None, decode_content=None):  # noqa: ARG002
            raise ProtocolError("Connection broken")

    raw = _BrokenRaw([])
    _patch_request(monkeypatch, _fake_response(raw))

    with pytest.raises(expected):
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=30.0,
            monotonic=_Clock(step=step),
        )

    assert raw.closed == 1


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
