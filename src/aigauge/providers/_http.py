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
nothing. Every failure here raises a ``requests.RequestException`` subclass,
and every call site has an ``except requests.RequestException`` branch to take
it - though Copilot's ``work()`` did not when this module landed, and caught
only ``HTTPError`` until 1.3.2+cfa.7 gave it one.

Nothing here retries: a deadline is a failure, not a reason to ask again. No
new host, no new request, no new dependency - stdlib plus ``requests``.
"""
from __future__ import annotations

import logging
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

# One line, one fixed literal, and only from the timer thread: see
# _DeadlineShutdown._note_missed_hook. The URL this module is handed is never
# logged - an ARM URL carries the subscription id and a Copilot one carries
# the GitHub username - and neither is any header, body or exception text.
log = logging.getLogger("aigauge.providers.http")

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
# cost us (see _body_chunks). What keeps one read from *producing* a gigabyte
# before this count is consulted is urllib3's, not ours: every 2.x ends
# `read1` with `self._decoded_buffer.get(amt)`, so a read returns at most
# `chunk_bytes` of decoded bytes - but only 2.6 and later stop the decoder
# itself at `max_length`. On 2.4 and 2.5 the whole raw read is decoded first
# and the surplus buffered, which is why pyproject.toml declares the floor
# rather than leaving it to whatever `requests` resolves: measured on 2.5.0, a
# 988-byte `Content-Encoding: gzip, gzip` body peaked at 1 070 MiB.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# The most one read asks for. Also how often the deadline and the size cap are
# checked. Reads return as soon as *any* bytes are there (see _body_chunks), so
# this is a ceiling on one read rather than an amount waited for: a healthy
# 1 MiB page is about eighteen reads.
CHUNK_BYTES = 64 * 1024

# How long the deadline timer waits before looking again when it fires and
# finds no socket to shut down (see _DeadlineShutdown._fire). A quarter of a
# second is short enough that the exposure it buys back is a fraction of one
# read rather than a whole protocol phase, and long enough that the retry
# costs four short-lived timer threads a second in a window that only exists
# once a call has already outrun its deadline - and that window is closed by
# the call's own `finally`, not by a count here.
REARM_SECONDS = 0.25


class ResponseDeadlineExceeded(requests.RequestException):
    """The whole exchange outran ``total_seconds``.

    A ``requests.RequestException`` on purpose: every call site in this package
    has an ``except requests.RequestException`` branch that turns a transport
    failure into an ERROR snapshot, and a deadline is a transport failure.
    (Copilot's ``work()`` is the one that did not, and was given one in
    1.3.2+cfa.7 rather than assumed.) The message carries the bound and
    nothing else - no URL (an ARM URL carries the subscription id), no
    response text - because it reaches ``snapshot.error``, the tile tooltip
    and ai-gauge.log.
    """


class ResponseTooLarge(requests.RequestException):
    """The body passed ``max_bytes``. Same contract as the class above."""


class ResponseRedirected(requests.RequestException):
    """A 3xx arrived, and this app does not follow redirects.

    ``raise_for_status()`` does not raise on a 3xx, so without this the
    redirect reached the call site as a body that will not parse: a
    ``JSONDecodeError`` on the usage paths, and on Copilot's username resolve
    a ``None`` that the tile reports as "PAT may lack read:user" - sending the
    user to re-issue a credential that is fine. ``api.github.com`` issues a
    301 for a renamed user or org, so this is not hypothetical; until
    1.3.2+cfa.7 those were followed. The status is in the message; the
    ``Location`` value and the URL are not, because this string reaches
    ``snapshot.error``, the tile tooltip and ai-gauge.log.
    """


class ResponseEncodingRefused(requests.RequestException):
    """The response declared more than one ``Content-Encoding``.

    Nothing this app asks for is served under nested codings - the four hosts
    are fixed and documented - and urllib3 decodes them one layer at a time,
    each layer whole before the next sees it below 2.6. So 988 bytes on the
    wire cost a worker 1 070 MiB and the entire deadline there, against a
    constant above that says a gigabyte is exactly what cannot happen. The
    version floor is one half of the answer; refusing before a byte of the
    body is read is the other, and costs nothing a real endpoint would miss.
    """


# The four exceptions this module raises, as a tuple a call site can
# `isinstance` against. Every one of their messages is built here out of a
# status, a count or a bound, so - unlike a `requests` exception, whose
# message carries the URL it failed on - they can be shown to a user as they
# stand. Copilot's `work()` is the caller that needs the distinction: an ARM
# URL carries the subscription id and a Copilot one the GitHub username.
HELPER_EXCEPTIONS = (
    ResponseDeadlineExceeded,
    ResponseTooLarge,
    ResponseRedirected,
    ResponseEncodingRefused,
)


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

    * connect blocks at most ``connect``;
    * once the socket exists, the shutdown at ``total_seconds`` ends whatever
      read is in flight - status line, header or body - and every read after
      it returns at once, so no phase after the connect outlives
      ``total_seconds``. That is what makes this a bound rather than a hope:
      before the timer, each byte of a dripping header reset the per-socket
      timeout and nothing re-checked the clock until the headers were
      complete;
    * the slack is one read plus one re-arm interval. A timer cannot shut
      down a socket that does not exist yet, so a timer that fires before
      there is one does not give up: it re-arms every ``REARM_SECONDS``
      until the call ends (``_DeadlineShutdown._fire``), which catches the
      exchange within a quarter-second of the socket appearing rather than
      leaving everything after it unbounded. The read that catch interrupts
      blocks at most ``read``.

    So the call returns or raises by ``max(connect, total_seconds) + read``.
    Note this is NOT ``connect + read + total_seconds + read``: that sum
    double-counts the connect and header phases, which the clock already
    covers. At every timeout this package uses ``total_seconds`` is the larger
    term, so in practice the bound is ``total_seconds + read``: 45 s at a 15 s
    timeout and 40 s at a 10 s one.

    One phase is outside all of it: **name resolution**.
    ``urllib3.util.connection.create_connection`` calls ``socket.getaddrinfo``
    before it makes a socket, so neither ``connect`` (which is set on a
    socket) nor the timer (which needs one to shut down) reaches it, and the
    resolver's own timeout is the only bound - a glibc default of
    ``timeout:5 attempts:2`` against three nameservers is 30 s on its own.
    That time is added to the number above rather than counted inside it, on
    this helper and on plain ``requests`` alike. The App's watchdog is the
    backstop and not a fix: it ends the App's wait, while the worker stays in
    ``getaddrinfo`` until the resolver gives up. Resolving on a thread is the
    only real answer, and is a larger change than this one.
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

    One timer thread at a time per call, cancelled in ``bounded_request``'s
    ``finally`` whether the call succeeded or failed, so nothing outlives the
    call.
    """

    def __init__(self, seconds: float) -> None:
        self._seconds = max(0.0, float(seconds))
        self._lock = threading.Lock()
        self._connection: Any = None
        self._timer: threading.Timer | None = None
        # Set by cancel(), under the lock: the call has ended, so no timer
        # may be armed again and one already running is a no-op. It is what
        # bounds the re-arm loop below - the call's own `finally`, not a
        # count of attempts here.
        self._ended = False
        # One warning per call, not one per re-arm (see _note_missed_hook).
        self._noted_missed_hook = False
        # Read by bounded_request: a socket this timer cut must surface as
        # ResponseDeadlineExceeded rather than as whatever the broken read
        # raised, however the two clocks round. Written here under the lock
        # and read without it, which needs no lock of its own: it is one-way
        # (never cleared), and an attribute read is atomic in CPython, so the
        # worst a bare read can see is one stale False that the elapsed-time
        # check beside it covers.
        self.fired = False

    def record(self, connection: Any) -> None:
        """Remember the connection the request is using."""
        with self._lock:
            self._connection = connection

    def arm(self) -> None:
        self._arm_in(self._seconds)

    def _arm_in(self, seconds: float) -> None:
        timer = threading.Timer(seconds, self._fire)
        # Daemon so a timer that somehow outlives its call cannot hold the
        # process open at exit.
        timer.daemon = True
        with self._lock:
            if self._ended:
                return
            self._timer = timer
        timer.start()

    def cancel(self) -> None:
        with self._lock:
            self._ended = True
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _fire(self) -> None:
        with self._lock:
            if self._ended:
                # The call is over and this is a timer it already cancelled,
                # racing the cancel. Shutting a socket down now would reach
                # whatever the pool handed out next.
                return
            self.fired = True
            connection = self._connection
        sock = getattr(connection, "sock", None)
        if sock is None:
            # Still resolving or connecting: there is nothing to shut down
            # YET. Giving up here is what left the rest of the exchange
            # unbounded - `HTTPConnection.connect()` assigns `sock` only
            # after the TCP connect, and `getaddrinfo` runs before that and
            # outside every timeout, so a resolver slower than the deadline
            # meant the status line, the header block and the body were back
            # to being bounded by nothing (measured: 40 s and 70 s against a
            # 4.0 s bound, ended only by the harness). So the timer tries
            # again shortly, and keeps trying until the call ends.
            self._note_missed_hook()
            self._arm_in(REARM_SECONDS)
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            # Already closed, never connected, or refused by the platform.
            # The read this was meant to unblock has ended on its own.
            pass

    def _note_missed_hook(self) -> None:
        """Say once that the deadline came due with no socket to shut down.

        Two different things look like this from here. The ordinary one is a
        call still in ``getaddrinfo`` or in the TCP connect, which the re-arm
        above picks up the moment a socket exists. The other is the failure
        this module cannot detect for itself: ``record()`` was never called at
        all, because the single seam that calls it - an
        ``HTTPAdapter.get_connection_with_tls_context`` override (a
        ``requests`` 2.32 method; ``pyproject.toml`` pins only
        ``requests>=2.32``) wrapping urllib3's private ``_get_conn`` - moved
        under a dependency upgrade. That silently removes the whole bound:
        measured by renaming the override, the suite stayed green at 1 805
        while a TLS header drip went from 3.00 s to 14.03 s and stuck. So it
        costs one line in the log instead of nothing. A fixed literal, like
        every other line this package writes: no URL, no host, no header, no
        exception text.
        """
        with self._lock:
            if self._noted_missed_hook:
                return
            self._noted_missed_hook = True
        log.warning("provider http deadline_hook_missed=True")


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
        # Every pool this adapter watched, so the wrapper can be taken off
        # again at the end of the call (see _unwatch_pools).
        self._watched: list[Any] = []
        super().__init__(**kwargs)

    def get_connection_with_tls_context(
        self, request: Any, verify: Any, proxies: Any = None, cert: Any = None
    ) -> Any:
        pool = super().get_connection_with_tls_context(
            request, verify, proxies=proxies, cert=cert
        )
        self._watched.append(pool)
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


def _unwatch_pools(session: requests.Session) -> None:
    """Put back the ``_get_conn`` ``_watch_pool`` replaced, before the close.

    The wrapper is a closure over the pool's own bound method, so
    pool -> closure -> cell -> bound method -> pool is a reference cycle: the
    pool is not freed by refcounting when ``session.close()`` drops it, and
    the connection it holds - with its socket - lives on until the cyclic
    collector runs. Measured against a loopback server, 300 healthy calls
    left 17 established sockets open at once where plain ``requests`` left
    none; they were always reclaimed, but until then the app holds open
    connections to the four hosts after the refresh that opened them has
    finished, which is the one thing ``_new_session``'s docstring says cannot
    happen. Restoring the attribute breaks the cycle, and the pool is freed
    with the call that made it.

    The pools come from the adapter's own list rather than from
    ``poolmanager.pools``, because a proxied call's pool is in
    ``adapter.proxy_manager`` instead - and a proxy is the configuration this
    app runs in on a corporate desktop.
    """
    for adapter in session.adapters.values():
        watched = getattr(adapter, "_watched", None)
        if not watched:
            continue
        for pool in watched:
            # Not every watched pool was wrapped: _watch_pool declines one
            # that has no _get_conn of its own.
            if "_get_conn" in vars(pool):
                del pool._get_conn
                pool._aigauge_watched = False
        watched.clear()


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

    Raises ``ResponseDeadlineExceeded``, ``ResponseTooLarge``,
    ``ResponseRedirected`` or ``ResponseEncodingRefused`` - all
    ``requests.RequestException`` - and closes the response first, so the
    socket is released rather than left to the garbage collector. Every other
    ``requests`` exception propagates as before, *except* one raised by a read
    the deadline timer cut: that is a deadline, and is re-raised as one with
    the original chained, so a call site cannot read it as an ordinary
    connection failure.

    ``allow_redirects`` defaults to **False**: every host this app speaks to is
    fixed and documented in SECURITY.md, so a redirect is a failure, not
    something to follow. Azure already passed this explicitly; Copilot and
    OpenRouter now get it too. Refusing to follow one is only half the job -
    ``raise_for_status()`` says nothing about a 3xx - so a 3xx raises
    ``ResponseRedirected`` here rather than reaching a call site as a body
    that will not parse.

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
        _refuse_redirect(response, allow_redirects)
        _refuse_nested_encoding(response)
        for chunk in _body_chunks(response, chunk_bytes):
            body += chunk
            if len(body) > max_bytes:
                raise ResponseTooLarge(
                    f"Response exceeded the {max_bytes}-byte limit."
                )
            _check_deadline(started, total_seconds, monotonic, deadline)
    except BaseException as exc:
        # Covers the four refusals above and anything requests raises mid-stream
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
        _unwatch_pools(session)
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


# The 3xx statuses that are a redirect: a `Location` to follow, and the
# reason this refusal exists. The rest of the range fails closed too - this
# app reads a 2xx and nothing else - but saying "redirected" about them is
# wrong. 304 Not Modified is the one that can arrive in practice: no call
# site sends `If-None-Match` or `If-Modified-Since` today, so a conformant
# origin will not send one, but a caching proxy can, and the first person to
# add an ETag to a GitHub call would get "The endpoint redirected (304)" on
# the tile for a perfectly ordinary reply.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _refuse_redirect(response: requests.Response, allow_redirects: bool) -> None:
    """A 3xx is a failure here, and has to be reported as the one it is."""
    if allow_redirects or not 300 <= response.status_code < 400:
        return
    if response.status_code in _REDIRECT_STATUSES:
        raise ResponseRedirected(
            f"The endpoint redirected ({response.status_code}); "
            "this app does not follow redirects."
        )
    raise ResponseRedirected(
        f"The endpoint returned {response.status_code}; "
        "this app reads only a 2xx."
    )


def _refuse_nested_encoding(response: requests.Response) -> None:
    """Refuse ``Content-Encoding: gzip, gzip`` before reading a byte of it.

    The test is the comma, not the names around it, because the comma is
    exactly what urllib3 decides on: ``HTTPResponse._init_decoder`` sends any
    header containing one to ``MultiDecoder``, which splits on ``","``
    *without* dropping empty entries and maps every entry it does not
    recognise - ``""`` included - to a ``DeflateDecoder``. So a tidier parse
    disagrees with it at the edges, and disagreeing downwards is a bypass:
    ``Content-Encoding: gzip,`` is one coding to a parse that drops empties
    and two decoder layers to urllib3, and a ``deflate(gzip(16 MiB))`` body
    under that header walked straight through the count this used to do -
    both layers decoded, 8.8 MiB of peak on the shipped urllib3 and the
    1 070 MiB shape on the 2.5.0 the floor forbids.

    ``identity, gzip`` and ``gzip, identity`` are refused for the same
    reason, though each names one real coding: urllib3 builds the same
    two-layer decoder for them, and its ``identity`` layer is a
    ``DeflateDecoder`` that fails on the plain output - so accepting them
    would only move the failure inside urllib3 and cost a body read on the
    way. No host this app speaks to sends either.
    """
    header = response.headers.get("Content-Encoding", "")
    if "," not in header:
        return
    codings = header.count(",") + 1
    raise ResponseEncodingRefused(
        f"Response declared {codings} content encodings; "
        "this app reads at most one."
    )


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
    in fact this app gave up. The four refusals below are already the right
    answer and keep their own names; ``KeyboardInterrupt`` and ``SystemExit``
    are not transport failures and keep theirs.
    """
    if isinstance(
        exc,
        (
            ResponseDeadlineExceeded,
            ResponseTooLarge,
            ResponseRedirected,
            ResponseEncodingRefused,
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
