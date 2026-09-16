"""The total-response bound the three REST providers share.

``requests``' ``timeout=`` bounds one socket operation, never the exchange, so
a server that drips a byte just inside it held a ``QThreadPool`` worker for as
long as it liked. These are the tests for the thing that stops it: the clock is
injected, the stream is a fake, and nothing here opens a socket.
"""

from __future__ import annotations

import errno
import gzip
import logging
import socket
import threading
import tracemalloc
from pathlib import Path

import requests
import pytest
import responses
import urllib3
from packaging.version import Version
from urllib3.exceptions import DecodeError, ProtocolError, ReadTimeoutError
from urllib3.exceptions import SSLError as Urllib3SSLError

from aigauge.providers import _http

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        # What the helper asked for, recorded rather than discarded: the
        # amount is the cap on one read and the granularity the deadline is
        # checked at, so a test that never sees it cannot see the bound.
        self.amounts: list = []
        self.chunk_sizes: list = []

    def _next(self) -> bytes:
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""

    def read1(self, amt=None, decode_content=None):  # noqa: ARG002
        self.amounts.append(amt)
        return self._next()

    def stream(self, chunk_size, decode_content=True):  # noqa: ARG002
        self.streamed += 1
        self.chunk_sizes.append(chunk_size)
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


# --- a redirect this app will not follow ------------------------------------


@responses.activate
@pytest.mark.parametrize(
    "status, redirect",
    [
        pytest.param(300, False, id="300"),
        pytest.param(301, True, id="301"),
        pytest.param(302, True, id="302"),
        pytest.param(303, True, id="303"),
        pytest.param(304, False, id="304"),
        pytest.param(307, True, id="307"),
        pytest.param(308, True, id="308"),
    ],
)
def test_a_redirect_is_refused_with_its_own_name(status, redirect):
    """`allow_redirects=False` stops the hop; `raise_for_status()` says
    nothing about a 3xx, so without this the redirect reached the call site as
    a body that will not parse. `api.github.com` answers a renamed user or org
    with a 301, so this is a real path, not a hypothetical one.

    The whole range still fails closed, because this app reads a 2xx and
    nothing else - but only the statuses that carry a `Location` are reported
    as a redirect. A `304 Not Modified` is not one, and is what a caching
    proxy or the first conditional request on these endpoints would produce.
    """
    responses.add(
        responses.GET,
        "https://example.invalid/moved",
        body="",
        status=status,
        headers={"Location": "https://elsewhere.invalid/secret-path"},
    )

    with pytest.raises(_http.ResponseRedirected) as excinfo:
        _http.bounded_request("GET", "https://example.invalid/moved", timeout=15)

    message = str(excinfo.value)
    assert str(status) in message
    assert ("redirected" in message) is redirect, message
    # The one thing a redirect carries is where it points, and that is the one
    # thing this message must not: it reaches the tile and ai-gauge.log.
    assert "elsewhere.invalid" not in message
    assert "secret-path" not in message
    assert "http" not in message.lower()
    assert isinstance(excinfo.value, requests.RequestException)


@responses.activate
def test_a_caller_that_asks_to_follow_redirects_still_may():
    """The refusal is of an unfollowed redirect, not of the status: a caller
    that passes `allow_redirects=True` gets requests' own behaviour."""
    responses.add(
        responses.GET,
        "https://example.invalid/moved",
        body="",
        status=302,
        headers={"Location": "https://example.invalid/there"},
    )
    responses.add(
        responses.GET, "https://example.invalid/there", json={"a": 1}, status=200
    )

    response = _http.bounded_request(
        "GET",
        "https://example.invalid/moved",
        timeout=15,
        allow_redirects=True,
    )

    assert response.json() == {"a": 1}


def test_a_redirect_is_refused_before_the_body_is_read(monkeypatch):
    raw = _FakeRaw([b"body that should never be read"])
    _patch_request(monkeypatch, _fake_response(raw, status=301))

    with pytest.raises(_http.ResponseRedirected):
        _http.bounded_request("GET", "https://example.invalid/x", timeout=15)

    assert raw.reads == 0
    assert raw.closed == 1


# --- what a Content-Encoding may cost -------------------------------------
#
# The cap above is on decoded bytes, and the count between reads only helps if
# one read cannot produce a gigabyte on its own. That property is urllib3's:
# every 2.x returns at most `amt` decoded bytes from `read1`, and 2.6 is where
# the decoder itself stops at `max_length` rather than decoding the whole raw
# read and buffering the surplus. Hence a declared floor, and a refusal for the
# shape that defeats any single-layer bound.

URLLIB3_FLOOR = Version("2.6")


def test_the_urllib3_floor_the_memory_bound_depends_on_is_installed():
    """Parsed, not string-compared: "2.10" < "2.6" as strings."""
    assert Version(urllib3.__version__) >= URLLIB3_FLOOR


def test_the_urllib3_floor_is_declared_as_a_dependency():
    """`requests>=2.32` asks only for `urllib3>=1.21.1,<3`, so without this
    line a resolver may hand this code a urllib3 whose decompression is not
    bounded - and nothing in build.sh or build.ps1 pins one."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = [
        line.strip().strip('",')
        for line in pyproject.splitlines()
        if line.strip().startswith('"urllib3')
    ]
    assert declared == [f"urllib3>={URLLIB3_FLOOR}"], declared


@responses.activate
@pytest.mark.parametrize(
    "coding",
    [
        pytest.param("gzip, gzip", id="two"),
        pytest.param("gzip,gzip", id="tight"),
        pytest.param("gzip,", id="trailing"),
        pytest.param(", gzip", id="leading"),
        pytest.param("identity, gzip", id="identity"),
        pytest.param("GZIP, GZIP", id="upper"),
    ],
)
def test_a_body_under_more_than_one_coding_is_refused_before_it_is_read(
    monkeypatch, coding
):
    """`gzip, gzip` is 988 bytes on the wire and 1 070 MiB in a worker on a
    urllib3 the floor above now forbids. No host this app speaks to serves
    nested codings, so the body is never read at all - which is also the only
    bound that does not depend on the resolved urllib3.

    The test is the comma, because that is what urllib3 decides on: any
    comma sends the header to `MultiDecoder`, which splits without dropping
    empty entries and gives every unrecognised one - `""` and `identity`
    alike - a `DeflateDecoder`. `gzip,` is therefore two decoder layers
    there and was one coding here, and a `deflate(gzip(16 MiB))` body under
    it went through both of them (8.8 MiB peak measured; the 1 070 MiB shape
    on 2.5.0). `identity, gzip` names one real coding and is refused too:
    urllib3's `identity` layer is a `DeflateDecoder` that fails on the plain
    output, so accepting it would only move the failure into urllib3 after a
    body read.
    """
    responses.add(
        responses.GET,
        "https://example.invalid/nested",
        body=gzip.compress(gzip.compress(b'{"a": 1}')),
        status=200,
        headers={"Content-Encoding": coding},
    )

    def no_reads(response, chunk_bytes):  # noqa: ARG001
        raise AssertionError("the body was read before it was refused")

    monkeypatch.setattr(_http, "_body_chunks", no_reads)

    with pytest.raises(_http.ResponseEncodingRefused) as excinfo:
        _http.bounded_request("GET", "https://example.invalid/nested", timeout=15)

    assert issubclass(_http.ResponseEncodingRefused, requests.RequestException)
    # The message reaches snapshot.error and the log: a count, never the
    # endpoint's own header text.
    assert "gzip" not in str(excinfo.value)
    assert "http" not in str(excinfo.value).lower()


@responses.activate
@pytest.mark.parametrize(
    "coding",
    [
        pytest.param("gzip", id="gzip"),
        pytest.param("x-gzip", id="x-gzip"),
        pytest.param("GZIP", id="upper"),
        pytest.param(None, id="absent"),
    ],
)
def test_one_coding_or_none_is_read_as_it_always_was(coding):
    """The refusal is of the comma, so everything without one still goes
    through the drain and its decoder: the header urllib3 lower-cases and
    the two names it treats as gzip, and a response that declares nothing."""
    body = b'{"a": 1}' if coding is None else gzip.compress(b'{"a": 1}')
    responses.add(
        responses.GET,
        "https://example.invalid/ok",
        body=body,
        status=200,
        headers={} if coding is None else {"Content-Encoding": coding},
    )

    response = _http.bounded_request(
        "GET", "https://example.invalid/ok", timeout=15
    )

    assert response.json() == {"a": 1}


@responses.activate
def test_one_coding_is_still_read():
    """The refusal is of *nested* codings; the ordinary gzip every host may
    send is what the drain exists to decode."""
    responses.add(
        responses.GET,
        "https://example.invalid/ok",
        body=gzip.compress(b'{"a": 1}'),
        status=200,
        headers={"Content-Encoding": "gzip"},
    )

    response = _http.bounded_request(
        "GET", "https://example.invalid/ok", timeout=15
    )

    assert response.json() == {"a": 1}
    # Through `read1`, with the real decoder: `responses` builds a real
    # urllib3 handle. Nothing else in the suite sends a Content-Encoding
    # through this helper, so `decode_content=True` -> `False` - which hands
    # every provider compressed bytes as `.content` in production - survived
    # the whole suite before this test.
    assert isinstance(response.raw, urllib3.response.HTTPResponse)


@responses.activate
def test_one_coding_is_still_read_through_the_legacy_fallback(monkeypatch):
    """The same round trip on the urllib3-1.x path, which `requests>=2.32`
    still permits. `responses` builds a real urllib3 handle, so hiding
    `read1` on it is what that version looks like from here - and
    `iter_content` decodes content-encoding of its own accord, which is the
    property this pins."""
    monkeypatch.setattr(urllib3.response.HTTPResponse, "read1", None)
    responses.add(
        responses.GET,
        "https://example.invalid/ok",
        body=gzip.compress(b'{"a": 1}'),
        status=200,
        headers={"Content-Encoding": "gzip"},
    )

    response = _http.bounded_request(
        "GET", "https://example.invalid/ok", timeout=15
    )

    assert response.json() == {"a": 1}


@responses.activate
def test_a_single_gzip_bomb_still_ends_at_the_cap_without_the_memory():
    """32 MiB of zeros in 33 KiB on the wire. The point is the peak, not the
    exception: a cap that is only enforced after the decoder has finished is
    not a memory bound."""
    responses.add(
        responses.GET,
        "https://example.invalid/bomb",
        body=gzip.compress(b"\0" * (32 * 1024 * 1024), 9),
        status=200,
        headers={"Content-Encoding": "gzip"},
    )

    tracemalloc.start()
    try:
        with pytest.raises(_http.ResponseTooLarge):
            _http.bounded_request(
                "GET", "https://example.invalid/bomb", timeout=15
            )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert peak < 32 * 1024 * 1024, f"peak {peak / 1024 / 1024:.1f} MiB"


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
        "GET", "https://example.invalid/x", timeout=15, chunk_bytes=4096
    )

    assert response.content == b"{}"
    assert raw.streamed == 0, "the blocking reader was used"
    # The cap reaches the read: it is both the most one read may ask for and
    # how often the deadline and the size cap are checked.
    assert raw.amounts == [4096, 4096]


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
    # One byte, and not `chunk_bytes`: urllib3 1.x's `stream()` blocks until
    # the whole chunk has arrived, so any larger granularity here puts the
    # deadline back behind a drip. The fake's `stream` ignores the argument,
    # exactly as urllib3's does not, so it is asserted rather than inferred.
    assert raw.chunk_sizes == [1]


def test_a_handle_whose_read1_is_not_urllib3s_takes_the_fallback(monkeypatch):
    """`io.BytesIO` and `BufferedReader` both have a `read1`, and both reject
    the keywords - a `TypeError`, which is not a `requests.RequestException`
    and so would walk past every branch at the call sites into a worker's
    blanket handler. Unreachable through real HTTP today, reachable through a
    fixture or a future adapter."""

    class _KeywordlessRaw(_FakeRaw):
        def read1(self, amt=-1):  # noqa: ARG002 - no decode_content, as in io
            self.amounts.append(amt)
            return self._next()

    raw = _KeywordlessRaw([b'{"a": ', b"1}"])
    _patch_request(monkeypatch, _fake_response(raw))

    response = _http.bounded_request(
        "GET", "https://example.invalid/x", timeout=15
    )

    assert response.json() == {"a": 1}
    assert raw.streamed == 1, "the fallback was not taken"
    assert raw.amounts == [], "the keyword read was retried"


def test_the_two_constants_are_what_the_arithmetic_and_the_docs_say():
    """Both survived the round-1 mutation run: every test that uses them
    derives both sides from the constant, so 30 s -> 3 000 s and 8 MiB -> 1 GiB
    moved nothing. They are a promise made in `SECURITY.md`, in the changelog
    and in three providers' refresh budgets, so they are pinned bare."""
    # Twice the largest per-socket timeout any caller sets, and the term the
    # providers' worst cases (130/135/495 s) are derived from.
    assert _http.REQUEST_TOTAL_SECONDS == 30.0
    # The memory ceiling against a hostile endpoint, an order of magnitude
    # over the largest response this app can legitimately receive.
    assert _http.MAX_RESPONSE_BYTES == 8 * 1024 * 1024
    assert _http.CHUNK_BYTES == 64 * 1024


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


class _SignallingSocket(_FakeSocket):
    """A `_FakeSocket` that says when it was shut down, so a test can wait for
    a timer thread instead of sleeping for one."""

    def __init__(self):
        super().__init__()
        self.cut = threading.Event()

    def shutdown(self, how):
        super().shutdown(how)
        self.cut.set()


class _DetachedSocket(_FakeSocket):
    """The plain socket urllib3 keeps in `conn.sock` during a TLS handshake.

    `ssl.SSLContext.wrap_socket` builds the SSLSocket on the same descriptor
    and detaches the original before the handshake runs, so `fileno()` is -1
    and `shutdown` raises EBADF for the whole of it. The second look is the
    one that finds a socket it can reach - here the same object, working, as
    the SSLSocket that replaces it would be.
    """

    def __init__(self):
        super().__init__()
        self.cut = threading.Event()

    def shutdown(self, how):
        self.shutdowns.append(how)
        if len(self.shutdowns) == 1:
            raise OSError(errno.EBADF, "Bad file descriptor")
        self.cut.set()


class _LateSocketConnection:
    """A connection whose socket appears only after the deadline has passed.

    What a slow resolver looks like from the timer's side:
    `HTTPConnection.connect()` assigns `sock` after the TCP connect, and
    `getaddrinfo` runs before that and outside every timeout, so a timer armed
    with the deadline can reach `sock` while it is still `None`.
    """

    def __init__(self, sock, *, appears_at: int = 2):
        self._sock = sock
        self._appears_at = appears_at
        self.looks = 0

    @property
    def sock(self):
        self.looks += 1
        return self._sock if self.looks >= self._appears_at else None


class _FakePool:
    """The urllib3 pool the adapter hooks: `_get_conn` is the one place a
    connection passes through, new or reused."""

    def __init__(self, connection):
        self._connection = connection
        self.calls = 0

    def _get_conn(self, timeout=None):  # noqa: ARG002
        self.calls += 1
        return self._connection


class _SendableRaw(_FakeRaw):
    """A `_FakeRaw` shaped like the urllib3 response `requests` builds a
    `Response` from, so a test can let `HTTPAdapter.send` run for real."""

    reason = "OK"
    version = 11
    # `extract_cookies_to_jar` returns at once when this is falsy.
    _original_response = None

    def __init__(self, chunks, status: int = 200, headers=None):
        super().__init__(chunks)
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    def release_conn(self):
        self.closed += 1


class _SendablePool(_FakePool):
    """`_FakePool` plus the one urllib3 method `HTTPAdapter.send` calls.

    `urlopen` takes a connection out of the pool the way urllib3 does, which
    is the call the deadline's hook is wrapped around.
    """

    def __init__(self, connection, raw):
        super().__init__(connection)
        self._raw = raw

    def urlopen(self, **kwargs):  # noqa: ARG002
        self._get_conn()
        return self._raw


def _patch_pool_manager(monkeypatch, pool) -> None:
    """Fake the pool under `requests`, not the transport above it.

    Everything else in this file injects at `Session.request`, which is above
    the adapter; this is below it, so `requests` runs its own `Session.send`,
    its own adapter and this module's override of it.
    """
    monkeypatch.setattr(
        urllib3.poolmanager.PoolManager,
        "connection_from_host",
        lambda self, host, port=None, scheme="http", pool_kwargs=None: pool,
    )


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
    "connection, re_arms",
    [
        pytest.param(None, True, id="none"),
        pytest.param(_FakeConnection(None), True, id="no sock"),
        pytest.param(
            _FakeConnection(_FakeSocket(OSError("gone"))), True, id="raises"
        ),
    ],
)
def test_the_deadline_fires_cleanly_with_nothing_to_shut_down(
    connection, re_arms
):
    """A timer that fires during DNS or connect finds no socket; one that
    fires on a socket the peer already closed - or on one a TLS handshake has
    detached - gets an OSError. Neither is a crash in a background thread, and
    neither gives up: both arm another timer, because the socket there is
    nothing to shut down on yet is the socket the rest of the exchange happens
    on."""
    deadline = _http._DeadlineShutdown(30.0)
    if connection is not None:
        deadline.record(connection)

    deadline._fire()

    assert deadline.fired is True
    assert (deadline._timer is not None) is re_arms
    # However it went, the call's own cancel takes whichever timer is current.
    deadline.cancel()
    assert deadline._timer is None


def test_a_timer_that_finds_no_socket_tries_again_rather_than_giving_up():
    """The narrow way the round-1 stall came back.

    `getaddrinfo` runs before any socket exists and outside every timeout, so
    a resolver slower than the deadline left the timer firing into a
    connection with no `sock`. It used to return there, and was never armed
    again: the status line, the header block and the body after it were
    bounded by nothing (measured, 40 s and 70 s against a 4.0 s bound, ended
    by the harness rather than by the app). Now it looks again.
    """
    sock = _SignallingSocket()
    connection = _LateSocketConnection(sock)
    deadline = _http._DeadlineShutdown(30.0)
    deadline.record(connection)

    deadline._fire()
    try:
        assert deadline._timer is not None, "the timer gave up with no socket"
        assert deadline._timer.interval == _http.REARM_SECONDS
        assert sock.cut.wait(10), "the re-armed timer never fired"
    finally:
        deadline.cancel()

    assert sock.shutdowns == [socket.SHUT_RDWR]
    assert connection.looks == 2


def test_a_socket_the_handshake_detached_is_looked_at_again(monkeypatch):
    """The way the round-1 stall came back over TLS.

    urllib3 puts the plain socket in `conn.sock` before it upgrades the
    connection and only swaps in the `SSLSocket` after the handshake returns,
    and `wrap_socket` detaches that plain object first - so for the whole
    handshake the timer's `shutdown` raises EBADF. Swallowing it ended the
    deadline for the rest of the call on the path all four hosts use
    (measured: 23.05 s and 22.53 s against a 12.0 s bound, and unbounded on a
    dripped header line). The second look is the one that works.
    """
    monkeypatch.setattr(_http, "REARM_SECONDS", 0.01)
    sock = _DetachedSocket()
    deadline = _http._DeadlineShutdown(30.0)
    deadline.record(_FakeConnection(sock))

    deadline._fire()
    try:
        assert deadline._timer is not None, "the timer gave up on an EBADF"
        assert deadline._timer.interval == _http.REARM_SECONDS
        assert sock.cut.wait(10), "the re-armed timer never looked again"
    finally:
        deadline.cancel()

    assert sock.shutdowns == [socket.SHUT_RDWR, socket.SHUT_RDWR]


def test_a_timer_that_fires_after_the_call_ended_does_nothing():
    """`cancel()` is the only thing that ends the re-arm loop, so a timer that
    was already running when it ran has to be a no-op: the pool it would reach
    into is being closed, and the next socket at that address is another
    call's."""
    sock = _FakeSocket()
    deadline = _http._DeadlineShutdown(30.0)
    deadline.record(_FakeConnection(sock))

    deadline.cancel()
    deadline._fire()

    assert sock.shutdowns == []
    assert deadline.fired is False
    assert deadline._timer is None


def test_a_re_arm_that_races_the_call_ending_never_starts(monkeypatch):
    """`_fire` reads the connection's socket outside the lock, so a call that
    ends in that window finds `cancel()` already done by the time the re-arm
    is armed. The guard in `_arm_in` is what keeps that last timer from
    starting - and a started one is a thread outliving its call, which is the
    thing the `finally` exists to prevent."""
    made = _patch_timer(monkeypatch)
    deadline = _http._DeadlineShutdown(30.0)

    class _ConnectionThatEndsTheCall:
        @property
        def sock(self):
            deadline.cancel()
            return None

    deadline.record(_ConnectionThatEndsTheCall())
    deadline._fire()

    assert len(made) == 1, "the re-arm did not reach threading.Timer"
    assert not made[0].started, "a timer was armed after the call had ended"
    assert deadline._timer is None


def test_the_re_arm_loop_is_ended_by_the_call_and_leaves_no_thread(monkeypatch):
    """Nothing counts the re-arms, so what stops them is `bounded_request`'s
    own `finally` - here on the failure path, with a socket that never
    appears at all (a resolver that never answers). The real `threading.Timer`
    is used, so these are threads and not doubles."""
    monkeypatch.setattr(_http, "REARM_SECONDS", 0.01)
    made: list = []
    real = _http.threading.Timer

    class _Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(_http.threading, "Timer", _Spy)
    looked_twice = threading.Event()

    class _NeverConnected:
        def __init__(self):
            self.looks = 0

        @property
        def sock(self):
            self.looks += 1
            if self.looks >= 2:
                looked_twice.set()
            return None

    def fake_request(session, method, url, **kwargs):  # noqa: ARG001
        session.get_adapter(url)._deadline.record(_NeverConnected())
        assert looked_twice.wait(10), "the timer was not armed a second time"
        return _fake_response(_FakeRaw([b"{}"]))

    monkeypatch.setattr(_http.requests.Session, "request", fake_request)

    with pytest.raises(_http.ResponseDeadlineExceeded):
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=0.01,
            monotonic=lambda: 1234.5,
        )

    assert len(made) >= 2, "the timer was armed once and never again"
    for timer in made:
        if timer.is_alive():
            timer.join(5)
    assert not any(timer.is_alive() for timer in made), "a timer outlived the call"


def test_the_pool_hook_records_the_connection_once():
    """urllib3 offers no callback for "the connection this request is about to
    use", so the hook is `_get_conn` on the pool - patched on the instance,
    which is built for one call and thrown away with it."""
    deadline = _http._DeadlineShutdown(30.0)
    connection = _FakeConnection(_FakeSocket())
    pool = _FakePool(connection)

    records: list = []
    deadline.record = lambda conn: records.append(conn)  # type: ignore[method-assign]

    _http._watch_pool(pool, deadline)
    _http._watch_pool(pool, deadline)
    assert pool._get_conn() is connection

    assert pool.calls == 1, "the pool was wrapped twice"
    # And wrapped once, not wrapped around its own wrapper: the count above
    # is of the pool's own method, which a second wrapper would still reach.
    assert records == [connection], "the hook was layered on itself"


def test_a_pool_with_no_get_conn_is_handed_back_untouched():
    """The hook is a private urllib3 attribute, so its absence is a version
    this module does not know rather than something to patch onto: wrapping a
    `None` would raise a TypeError inside `requests`' own send path, where no
    `except requests.RequestException` branch would catch it."""

    class _NotAPool:
        pass

    pool = _NotAPool()

    assert _http._watch_pool(pool, _http._DeadlineShutdown(30.0)) is pool
    assert not hasattr(pool, "_get_conn")


def test_a_real_send_reaches_the_pool_hook(monkeypatch):
    """The one bridge from `requests` to the recording hook, driven by
    `requests`.

    Everything the timer does depends on `record()` having been called, and
    the only thing that calls it in production is `HTTPAdapter.send` ->
    `get_connection_with_tls_context` -> `_watch_pool` -> `_get_conn`. Every
    other transport test in this file injects at `Session.request`, which is
    above the adapter, so that override was covered by nothing: renaming it
    left the suite at 1 805 passed while a TLS header drip went from 3.00 s
    back to 14.03 s and stuck. Here `requests` runs its own `Session.send`
    and only the pool below it is a fake.
    """
    assert hasattr(
        requests.adapters.HTTPAdapter, "get_connection_with_tls_context"
    ), "requests moved the adapter seam this module hooks"

    connection = _FakeConnection(_FakeSocket())
    pool = _SendablePool(connection, _SendableRaw([b'{"a": ', b"1}"]))
    _patch_pool_manager(monkeypatch, pool)
    recorded: list = []
    real_record = _http._DeadlineShutdown.record

    def spy(self, conn):
        recorded.append((self, conn))
        real_record(self, conn)

    monkeypatch.setattr(_http._DeadlineShutdown, "record", spy)

    response = _http.bounded_request(
        "GET",
        "https://example.invalid/x",
        timeout=15,
        proxies={"http": None, "https": None},
    )

    assert response.json() == {"a": 1}
    assert pool.calls == 1
    assert [conn for _, conn in recorded] == [connection]
    # The deadline the recording reached is the one the call armed, not a
    # spare: `_fire` on any other would shut nothing down.
    assert recorded[0][0]._connection is connection
    assert connection.sock.shutdowns == [], "a healthy call was cut short"


def test_a_real_send_lets_the_deadline_cut_the_socket_it_learned(monkeypatch):
    """The other half of the same bridge: a read that only the shutdown ends.

    The injected clock never advances, so nothing in band can raise. The only
    way out is the timer firing on a socket it learned through `requests`' own
    send path - which is the property the release rests on and which no test
    drove before.
    """
    sock = _SignallingSocket()
    connection = _FakeConnection(sock)

    class _BlockingRaw(_SendableRaw):
        def read1(self, amt=None, decode_content=None):  # noqa: ARG002
            assert sock.cut.wait(10), "the deadline never reached the socket"
            raise ProtocolError("Connection broken: IncompleteRead(0 bytes read)")

    pool = _SendablePool(connection, _BlockingRaw([]))
    _patch_pool_manager(monkeypatch, pool)

    with pytest.raises(_http.ResponseDeadlineExceeded):
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=0.05,
            monotonic=lambda: 1234.5,
            proxies={"http": None, "https": None},
        )

    assert sock.shutdowns == [socket.SHUT_RDWR]


def test_a_healthy_calls_pool_is_handed_back_unwrapped(monkeypatch):
    """The wrapper is a closure over the pool's own bound method, so leaving
    it on makes the pool part of a reference cycle: `session.close()` drops
    it, refcounting does not free it, and the connection it holds - with its
    socket - waits for the cyclic collector. Measured, 300 healthy calls left
    17 sockets open at once where plain `requests` left none, against a
    docstring that says no connection survives a refresh."""
    connection = _FakeConnection(_FakeSocket())
    pool = _SendablePool(connection, _SendableRaw([b"{}"]))
    _patch_pool_manager(monkeypatch, pool)

    _http.bounded_request(
        "GET",
        "https://example.invalid/x",
        timeout=15,
        proxies={"http": None, "https": None},
    )

    assert pool.calls == 1, "the hook was not on for the call itself"
    assert "_get_conn" not in pool.__dict__, "the pool was left in a cycle"
    assert pool._aigauge_watched is False
    # And the pool still works: what was removed is the instance attribute,
    # not the method under it.
    assert pool._get_conn() is connection


def test_a_deadline_that_never_learned_a_connection_says_so_once(caplog):
    """The failure mode the line above exists for is silent by construction:
    `_fire` finds no connection and returns. One warning, from the timer
    thread, with no URL in it - and one per call however often the re-arm
    looks again, because the log is the only place a moved seam would show."""
    deadline = _http._DeadlineShutdown(30.0)

    with caplog.at_level(logging.WARNING, logger="aigauge"):
        deadline._fire()
        deadline._fire()
        deadline.cancel()

    assert caplog.text.count("deadline_hook_missed=True") == 1
    # A fixed literal, so there is nothing in it to redact.
    assert "://" not in caplog.text
    assert "example.invalid" not in caplog.text


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


# --- the edges no call site reaches today -----------------------------------
#
# Each of these survived the whole suite as a mutation. None is reachable from
# a call site as the package stands, which is exactly why they are the lines
# that rot: the negative clamp, the `BaseException` branch, the close on the
# failure path, and one arm of the mapping the drain wears.


def test_a_negative_deadline_is_clamped_rather_than_armed(monkeypatch):
    """`threading.Timer(-1, ...)` fires at once, so the clamp is the
    difference between a nonsense argument being a nonsense deadline and
    being a cut socket before the request is made."""
    made = _patch_timer(monkeypatch)

    _http._DeadlineShutdown(-5.0).arm()

    assert made[0].interval == 0.0


def test_a_keyboard_interrupt_is_not_relabelled_as_a_deadline(monkeypatch):
    """The drain catches `BaseException` so that a cut socket is closed
    whatever ended it, which puts `KeyboardInterrupt` and `SystemExit` in the
    same branch as a transport failure. They are not transport failures, and
    a deadline that swallowed one would make Ctrl-C look like a dripping
    server."""

    class _InterruptedRaw(_FakeRaw):
        def read1(self, amt=None, decode_content=None):  # noqa: ARG002
            raise KeyboardInterrupt

    raw = _InterruptedRaw([])
    _patch_request(monkeypatch, _fake_response(raw))

    with pytest.raises(KeyboardInterrupt):
        _http.bounded_request(
            "GET",
            "https://example.invalid/x",
            timeout=15,
            total_seconds=30.0,
            # Past the deadline by the time the interrupt is classified, so
            # the clock alone would call it one.
            monotonic=_Clock(step=20.0),
        )

    assert raw.closed == 1, "the socket was left open"


def test_the_session_is_closed_on_the_failing_path_as_well(monkeypatch):
    """One `Session` per call is only a property if each one is closed; on
    the failure path the pool is what holds the socket, and a `Session` left
    to the collector is what this release removed from the healthy path."""
    closed: list = []
    real_close = _http.requests.Session.close

    def close(session):
        closed.append(session)
        real_close(session)

    monkeypatch.setattr(_http.requests.Session, "close", close)
    _patch_request(monkeypatch, _fake_response(_FakeRaw([b"{}"])))

    _http.bounded_request("GET", "https://example.invalid/x", timeout=15)
    assert len(closed) == 1

    _patch_request(monkeypatch, _fake_response(_FakeRaw([b"x"]), status=301))
    with pytest.raises(_http.ResponseRedirected):
        _http.bounded_request("GET", "https://example.invalid/x", timeout=15)

    assert len(closed) == 2, "the failing call kept its Session"
    assert closed[0] is not closed[1]


@pytest.mark.parametrize(
    "raised, expected",
    [
        pytest.param(
            ProtocolError("broken"),
            requests.exceptions.ChunkedEncodingError,
            id="protocol",
        ),
        pytest.param(
            DecodeError("bad"),
            requests.exceptions.ContentDecodingError,
            id="decode",
        ),
        pytest.param(
            ReadTimeoutError(None, "/x", "read timed out"),
            requests.exceptions.ConnectionError,
            id="readtimeout",
        ),
        pytest.param(
            Urllib3SSLError("tls"), requests.exceptions.SSLError, id="ssl"
        ),
    ],
)
def test_a_raw_read_wears_requests_own_exception_mapping(raised, expected):
    """Lifted from `Response.iter_content`, and load-bearing: without it a
    mid-stream failure reaches the call sites as a urllib3 exception, walks
    past every `except requests.RequestException` branch there, and lands in
    a worker's blanket handler instead of the named one. The read-timeout arm
    is the one no test drove."""

    def read1(amt, decode_content=None):  # noqa: ARG001
        raise raised

    with pytest.raises(expected) as excinfo:
        _http._read1(read1, 4096)

    assert type(excinfo.value) is expected
    assert excinfo.value.__cause__ is raised


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
