"""A total-response bound for the three REST providers' `requests` calls.

``requests``' ``timeout=`` is per *socket operation* - the connect, then each
individual read - never a bound on the whole exchange. A server that sends one
byte every 14 s against a 15 s timeout is, as far as ``requests`` is concerned,
a healthy connection, and it holds the ``QThreadPool`` worker that is reading
it for as long as it cares to keep dripping. 1.3.1+cfa.6 bounded how many such
workers could accumulate (the REST park, the one-hour backstop) and what one
could cost the log; nothing bounded how long one *lives*, and the pool is
global, so a stuck worker is a stuck slot until the process exits.

This module bounds the socket, in both halves an exchange has. In-band:
``bounded_request`` starts a clock, asks for the response with ``stream=True``,
and drains the body itself, checking the elapsed time and the running byte
count between reads. Out of band: a ``threading.Timer`` armed with the same
deadline shuts that connection's socket down from another thread, which is the
only thing that reaches a stall *inside* one read - the status line, the
response headers, or a ``Content-Encoding`` stream whose bytes decode to
nothing. Both failures raise a ``requests.RequestException`` subclass, so they
reach the call sites as a transport failure like any other.

Nothing here retries: a deadline is a failure, not a reason to ask again. No
new host, no new request, no new dependency - stdlib plus ``requests``.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Any, Callable, Iterator

import requests
from requests.adapters import HTTPAdapter

# requests' own transitive dependency, imported exactly as `requests.models`
# imports it - nothing is added to pyproject.toml by this line. It is here so
# that a failure raised by the raw stream reaches the call sites as the same
# `requests` exception `Response.iter_content` would have wrapped it in.
from urllib3.exceptions import DecodeError, ProtocolError, ReadTimeoutError
from urllib3.exceptions import SSLError as _Urllib3SSLError

# How long one call may spend between "about to connect" and "body fully read".
# The clock starts BEFORE requests.request(), so the connect and header-read
# phases are inside it rather than added to it.
#
# Thirty seconds is twice the largest per-socket timeout any caller here sets,
# and two orders of magnitude past a healthy call: GitHub's usage summary,
# OpenRouter's /credits, /key and /activity, and one Cost Management page all
# answer in well under a second on a working link. It is a bound on a *wedged*
# exchange, not a performance target, so it is set where a slow-but-real
# connection cannot trip it.
REQUEST_TOTAL_SECONDS = 30.0

# What one response may weigh. There is no network here to measure a real
# payload against, so this is derived from the code's own ceilings: Azure caps
# a page-set at MAX_QUERY_ROWS = 50 000 rows, the Query API pages at ~1 000
# rows, and a Cost Management row is a handful of short strings and two
# numbers - a few hundred bytes - so one page is well under 1 MiB. GitHub's
# usage summary and OpenRouter's key/credits/activity documents are smaller
# again. 8 MiB is roughly an order of magnitude of headroom over the largest
# of those, and small enough that a hostile or broken endpoint cannot make a
# background worker allocate a gigabyte. Past it the call fails with
# ResponseTooLarge, which every call site already turns into an ERROR tile.
#
# It is a ceiling on the body this module *keeps*, and the check runs after a
# chunk is appended, so the bytes held can reach max_bytes + chunk_bytes - 1
# (8 MiB + 64 KiB - 1) before the call fails. That slack is one read, by
# construction, which is why the count is checked per read rather than per
# byte.
#
# The count is of *decoded* bytes: the drain asks urllib3 to decode
# content-encoding, so a gzip bomb is measured at the size it would actually
# cost us (see _body_chunks).
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# The most one read asks for. Also how often the deadline and the size cap are
# checked. Reads return as soon as *any* bytes are there (see _body_chunks), so
# this is a ceiling on one read rather than an amount waited for: a healthy
# 1 MiB page is about eighteen reads.
CHUNK_BYTES = 64 * 1024


class ResponseDeadlineExceeded(requests.RequestException):
    """The whole exchange outran ``total_seconds``.

    A ``requests.RequestException`` on purpose: every call site in this package
    already has an ``except requests.RequestException`` branch that turns a
    transport failure into an ERROR snapshot, and a deadline is a transport
    failure. The message carries the bound and nothing else - no URL (an ARM
    URL carries the subscription id), no response text - because it reaches
    ``snapshot.error``, the tile tooltip and ai-gauge.log.
    """


class ResponseTooLarge(requests.RequestException):
    """The body passed ``max_bytes``. Same contract as the class above."""


def request_worst_case_seconds(
    timeout: float | tuple[float, float],
    *,
    total_seconds: float = REQUEST_TOTAL_SECONDS,
) -> float:
    """The longest one ``bounded_request`` call can take, from its own bounds.

    Derived rather than restated, so a provider's refresh budget cannot drift
    away from what the transport actually enforces (that is what the App's
    watchdog reads, and a watchdog that fires inside a refresh which is still
    legitimately running manufactures the failure it exists to catch).

    The arithmetic. ``requests`` splits ``timeout`` into a connect timeout and
    a read timeout - one number means both. The clock starts before the call,
    and the deadline timer is armed with it, so:

    * connect blocks at most ``connect``. A timer cannot shut down a socket
      that does not exist yet, so this phase is bounded by ``connect`` alone;
    * once the socket exists, the shutdown at ``total_seconds`` ends whatever
      read is in flight - status line, header or body - and every read after
      it returns at once, so no phase after the connect outlives
      ``total_seconds``. That is what makes this a bound rather than a hope:
      before the timer, each byte of a dripping header reset the per-socket
      timeout and nothing re-checked the clock until the headers were
      complete;
    * the slack is one read. The timer fires once, and it can fire in the
      window between the connection being handed out and its socket being
      created, where it finds nothing to shut down; the read after that
      window blocks at most ``read``.

    So the call returns or raises by ``max(connect, total_seconds) + read``.
    Note this is NOT ``connect + read + total_seconds + read``: that sum
    double-counts the connect and header phases, which the clock already
    covers. At every timeout this package uses ``total_seconds`` is the larger
    term, so in practice the bound is ``total_seconds + read``: 45 s at a 15 s
    timeout and 40 s at a 10 s one.
    """
    if isinstance(timeout, tuple):
        connect, read = float(timeout[0]), float(timeout[1])
    else:
        connect = read = float(timeout)
    return max(connect, float(total_seconds)) + read


class _DeadlineShutdown:
    """The out-of-band half of the deadline: shut the socket at the bound.

    Nothing inside ``requests`` or urllib3 bounds an exchange. A per-socket
    read timeout is reset by every byte that arrives, and urllib3's own
    ``Timeout(total=...)`` only clamps the value of that per-read timeout -
    measured, a 30 s header drip returned at 30.01 s against a 3 s total. The
    one thing that reaches a thread blocked in ``recv`` is closing the socket
    under it, which is what this does: ``shutdown(SHUT_RDWR)`` from a timer
    thread unblocks a blocking read immediately, on POSIX and on Windows
    (documented on both; the loopback proof for Windows-style behaviour lives
    in the review drivers rather than the suite, which opens no sockets).

    One timer thread per call, cancelled in ``bounded_request``'s ``finally``
    whether the call succeeded or failed, so nothing outlives the call.
    """

    def __init__(self, seconds: float) -> None:
        self._seconds = max(0.0, float(seconds))
        self._lock = threading.Lock()
        self._connection: Any = None
        self._timer: threading.Timer | None = None
        # Read by bounded_request: a socket this timer cut must surface as
        # ResponseDeadlineExceeded rather than as whatever the broken read
        # raised, however the two clocks round.
        self.fired = False

    def record(self, connection: Any) -> None:
        """Remember the connection the request is using."""
        with self._lock:
            self._connection = connection

    def arm(self) -> None:
        timer = threading.Timer(self._seconds, self._fire)
        # Daemon so a timer that somehow outlives its call cannot hold the
        # process open at exit.
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()

    def cancel(self) -> None:
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            connection = self._connection
        sock = getattr(connection, "sock", None)
        if sock is None:
            # Still in DNS or connect: there is nothing to shut down, and the
            # connect timeout is what bounds that phase (see
            # request_worst_case_seconds).
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            # Already closed, never connected, or refused by the platform.
            # The read this was meant to unblock has ended on its own.
            pass


class _ConnectionRecordingAdapter(HTTPAdapter):
    """An ``HTTPAdapter`` that tells the deadline which socket to cut.

    ``requests`` hands out no connection object and urllib3 offers no callback
    for "the connection this request is about to use", so the hook is
    ``HTTPConnectionPool._get_conn`` - the one place a connection passes
    through whether it is new or reused, on urllib3 1.26 and 2.x alike. It is
    private, and patched on the *instance*: the pool, the adapter and the
    session are all built for one call and thrown away with it, so nothing
    here can leak into another request.
    """

    def __init__(self, deadline: _DeadlineShutdown, **kwargs: Any) -> None:
        self._deadline = deadline
        super().__init__(**kwargs)

    def get_connection_with_tls_context(
        self, request: Any, verify: Any, proxies: Any = None, cert: Any = None
    ) -> Any:
        pool = super().get_connection_with_tls_context(
            request, verify, proxies=proxies, cert=cert
        )
        return _watch_pool(pool, self._deadline)


def _watch_pool(pool: Any, deadline: _DeadlineShutdown) -> Any:
    """Record every connection ``pool`` hands out, once per pool."""
    real_get_conn = getattr(pool, "_get_conn", None)
    if not callable(real_get_conn) or getattr(pool, "_aigauge_watched", False):
        return pool

    def _get_conn(timeout: Any = None) -> Any:
        connection = real_get_conn(timeout)
        deadline.record(connection)
        return connection

    pool._get_conn = _get_conn
    pool._aigauge_watched = True
    return pool


def _new_session(deadline: _DeadlineShutdown) -> requests.Session:
    """A ``Session`` for exactly one call, mounted on the recording adapter.

    ``requests.request`` opens and discards a ``Session`` per call itself
    (``requests/api.py``), and that property is worth keeping rather than
    merely inheriting: no connection pool, no cookie jar and no connection
    survives a refresh, so nothing a hostile endpoint sets can reach the next
    one. The only reason this is spelled out here instead is that a mounted
    adapter is the sole way to learn which socket to shut down.
    """
    session = requests.Session()
    adapter = _ConnectionRecordingAdapter(deadline)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def bounded_request(
    method: str,
    url: str,
    *,
    timeout: float | tuple[float, float],
    total_seconds: float = REQUEST_TOTAL_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    chunk_bytes: int = CHUNK_BYTES,
    monotonic: Callable[[], float] = time.monotonic,
    allow_redirects: bool = False,
    **kwargs: Any,
) -> requests.Response:
    """``requests.request`` with a bound on the whole exchange, not each read.

    Returns a ``Response`` that behaves exactly like a non-streamed one:
    ``.status_code``, ``.headers``, ``.content``, ``.text``, ``.json()`` and
    ``.raise_for_status()`` (including the ``HTTPError.response`` back-link)
    are all unchanged, so no call site has to know this helper is in the way.

    Raises ``ResponseDeadlineExceeded`` or ``ResponseTooLarge`` - both
    ``requests.RequestException`` - and closes the response first, so the
    socket is released rather than left to the garbage collector. Every other
    ``requests`` exception propagates as before, *except* one raised by a read
    the deadline timer cut: that is a deadline, and is re-raised as one with
    the original chained, so a call site cannot read it as an ordinary
    connection failure.

    ``allow_redirects`` defaults to **False**: every host this app speaks to is
    fixed and documented in SECURITY.md, so a redirect is a failure, not
    something to follow. Azure already passed this explicitly; Copilot and
    OpenRouter now get it too.

    ``monotonic`` is injectable so the tests can exhaust a deadline without
    waiting. Never use ``time.monotonic()`` as if it started at zero - the
    helper only ever reads differences. The timer is real time by nature and
    is not injectable; a test that exhausts an injected clock cancels it
    unfired.
    """
    # Started before the call, so the connect and header-read phases count
    # against the deadline instead of being added to it.
    started = monotonic()
    deadline = _DeadlineShutdown(total_seconds)
    session = _new_session(deadline)
    response: requests.Response | None = None
    body = bytearray()
    try:
        # Armed before the request, because the request is where the stalls
        # this exists for happen: a dripped status line or header block never
        # returns to this function at all.
        deadline.arm()
        response = session.request(
            method,
            url,
            stream=True,
            timeout=timeout,
            allow_redirects=allow_redirects,
            **kwargs,
        )
        # Checked once before the first read: a server that stalls in the
        # headers has already spent the budget, and there is no reason to
        # wait out a whole chunk read to say so.
        _check_deadline(started, total_seconds, monotonic, deadline)
        for chunk in _body_chunks(response, chunk_bytes):
            body += chunk
            if len(body) > max_bytes:
                raise ResponseTooLarge(
                    f"Response exceeded the {max_bytes}-byte limit."
                )
            _check_deadline(started, total_seconds, monotonic, deadline)
    except BaseException as exc:
        # Covers the two bounds above and anything requests raises mid-stream
        # (a chunked-encoding failure, a read timeout on one chunk). Abandoning
        # the generator stops the read; close() releases the connection.
        if response is not None:
            response.close()
        if _cut_by_the_deadline(exc, started, total_seconds, monotonic, deadline):
            raise ResponseDeadlineExceeded(
                f"Response deadline exceeded ({total_seconds:g}s)."
            ) from exc
        raise
    finally:
        # The timer covers the *whole* exchange, body included - a
        # Content-Encoding stream that decodes to nothing keeps one read
        # blocked with no bytes to count - so it is cancelled here, once the
        # body is drained or the call has failed, and never when the request
        # returns.
        deadline.cancel()
        session.close()

    # The idiom requests itself uses when it has consumed a streamed body and
    # wants the Response to behave like a buffered one (see Response.content,
    # which sets exactly these two). Private, and deliberately so: there is no
    # public setter, and the alternative - building a Response by hand - would
    # have to reproduce cookies, history, elapsed and the raw handle. A
    # requests upgrade that renames either attribute is caught by
    # test_http.py's test on .json()/.content/.text rather than silently
    # producing empty tiles.
    response._content = bytes(body)
    response._content_consumed = True
    return response


def _body_chunks(response: requests.Response, chunk_bytes: int) -> Iterator[bytes]:
    """Yield body bytes as soon as any of them arrive.

    This is the part the deadline depends on, and the obvious spelling does not
    work. ``Response.iter_content(chunk_size=N)`` goes through urllib3's
    ``stream()``, which documents itself as blocking "until ``amt`` bytes have
    been read from the connection or until the connection is closed" - and the
    per-socket read timeout never fires on a server that sends a byte just
    inside it. So on a Content-Length response the deadline check below would
    not run again until 64 KiB had been dripped, which is the exact failure
    this module exists to bound. Measured against a loopback server sending one
    byte every 50 ms with a 2 s socket timeout: ``iter_content(64 KiB)`` never
    yielded at all, ``iter_content(1)`` yielded at once, and ``raw.read1()``
    yielded at once. On a 1 MiB body delivered in one go the same three cost
    0.8 ms, 3 854 ms and 0.5 ms.

    ``read1`` is urllib3 2.x. ``requests>=2.32`` still permits urllib3 1.x, and
    there one byte at a time is the only granularity that cannot block behind a
    drip - correct, and 3.9 s per MiB on a payload that is a few hundred KiB at
    the very most. A handle whose ``read1`` is not urllib3's (``io.BytesIO``
    and ``BufferedReader`` both have one, and both reject the keywords) takes
    the same fallback rather than raising a ``TypeError`` that no
    ``except requests.RequestException`` branch would catch.

    Neither granularity bounds a read that never *returns*: one urllib3
    ``read1`` loops internally until the decoder yields something, so a DEFLATE
    stream of empty stored blocks reads for as long as the peer sends. That one
    is the deadline timer's, not this function's.

    The exception mapping is requests' own, lifted from
    ``Response.iter_content``: without it a mid-stream failure would reach the
    call sites as a urllib3 exception, walk past every
    ``except requests.RequestException`` branch there, and land in a worker's
    blanket handler instead of the named one.
    """
    read1 = getattr(response.raw, "read1", None)
    chunk = b""
    if callable(read1):
        try:
            chunk = _read1(read1, chunk_bytes)
        except TypeError:
            # Argument binding failed, so no byte of the body was consumed
            # and the fallback below starts from the beginning.
            read1 = None
    if not callable(read1):
        for chunk in response.iter_content(chunk_size=1):
            if chunk:
                yield chunk
        return
    while chunk:
        yield chunk
        chunk = _read1(read1, chunk_bytes)


def _read1(read1: Callable[..., bytes], chunk_bytes: int) -> bytes:
    """One ``read1``, wearing requests' own urllib3-to-requests mapping."""
    try:
        return read1(chunk_bytes, decode_content=True)
    except ProtocolError as exc:
        raise requests.exceptions.ChunkedEncodingError(exc) from exc
    except DecodeError as exc:
        raise requests.exceptions.ContentDecodingError(exc) from exc
    except ReadTimeoutError as exc:
        raise requests.exceptions.ConnectionError(exc) from exc
    except _Urllib3SSLError as exc:
        raise requests.exceptions.SSLError(exc) from exc


def _cut_by_the_deadline(
    exc: BaseException,
    started: float,
    total_seconds: float,
    monotonic: Callable[[], float],
    deadline: _DeadlineShutdown,
) -> bool:
    """Is this failure the timer's doing rather than the endpoint's?

    A socket the timer shut down surfaces as whatever the broken read raised -
    a ``ChunkedEncodingError`` on a truncated body, a ``ConnectionError`` on a
    cut header block - which would tell the call site the endpoint failed when
    in fact this app gave up. The two bounds below are already the right
    answer and keep their own names; ``KeyboardInterrupt`` and ``SystemExit``
    are not transport failures and keep theirs.
    """
    if isinstance(
        exc,
        (
            ResponseDeadlineExceeded,
            ResponseTooLarge,
        ),
    ):
        return False
    if not isinstance(exc, Exception):
        return False
    # `fired` as well as the clock: the timer and the clock are the same
    # deadline read two ways, and the exception surfaces a hair after the
    # shutdown that caused it.
    return deadline.fired or monotonic() - started > total_seconds


def _check_deadline(
    started: float,
    total_seconds: float,
    monotonic: Callable[[], float],
    deadline: _DeadlineShutdown | None = None,
) -> None:
    if (deadline is not None and deadline.fired) or (
        monotonic() - started > total_seconds
    ):
        raise ResponseDeadlineExceeded(
            f"Response deadline exceeded ({total_seconds:g}s)."
        )
