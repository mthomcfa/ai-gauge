"""A total-response bound for the three REST providers' `requests` calls.

``requests``' ``timeout=`` is per *socket operation* - the connect, then each
individual read - never a bound on the whole exchange. A server that sends one
byte every 14 s against a 15 s timeout is, as far as ``requests`` is concerned,
a healthy connection, and it holds the ``QThreadPool`` worker that is reading
it for as long as it cares to keep dripping. 1.3.1+cfa.6 bounded how many such
workers could accumulate (the REST park, the one-hour backstop) and what one
could cost the log; nothing bounded how long one *lives*, and the pool is
global, so a stuck worker is a stuck slot until the process exits.

This module bounds the socket. ``bounded_request`` starts a clock, asks for the
response with ``stream=True``, and drains the body itself, checking the elapsed
time and the running byte count between chunks. Both failures raise a
``requests.RequestException`` subclass, so every ``except requests.RequestException``
branch that already exists at the call sites handles them without a new branch.

Nothing here retries: a deadline is a failure, not a reason to ask again. No
new host, no new request, no new dependency - stdlib plus ``requests``.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterator

import requests

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
# The count is of *decoded* bytes: iter_content decodes content-encoding, so a
# gzip bomb is measured at the size it would actually cost us.
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
    so:

    * connect blocks at most ``connect``;
    * the header read blocks at most ``read``, and the deadline is checked as
      soon as that returns, so a headers-phase stall raises at
      ``connect + read`` without reading any body;
    * otherwise every check that passes does so at or under ``total_seconds``,
      and the one chunk read after it blocks at most ``read``.

    So the call returns or raises by ``max(connect, total_seconds) + read``.
    Note this is NOT ``connect + read + total_seconds + read``: that sum
    double-counts the connect and header phases, which the clock already
    covers.
    """
    if isinstance(timeout, tuple):
        connect, read = float(timeout[0]), float(timeout[1])
    else:
        connect = read = float(timeout)
    return max(connect, float(total_seconds)) + read


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
    ``requests`` exception propagates as before.

    ``allow_redirects`` defaults to **False**: every host this app speaks to is
    fixed and documented in SECURITY.md, so a redirect is a failure, not
    something to follow. Azure already passed this explicitly; Copilot and
    OpenRouter now get it too.

    ``monotonic`` is injectable so the tests can exhaust a deadline without
    waiting. Never use ``time.monotonic()`` as if it started at zero - the
    helper only ever reads differences.
    """
    # Started before the call, so the connect and header-read phases count
    # against the deadline instead of being added to it.
    started = monotonic()
    response = requests.request(
        method,
        url,
        stream=True,
        timeout=timeout,
        allow_redirects=allow_redirects,
        **kwargs,
    )
    body = bytearray()
    try:
        # Checked once before the first read: a server that stalls in the
        # headers has already spent the budget, and there is no reason to
        # wait out a whole chunk read to say so.
        _check_deadline(started, total_seconds, monotonic)
        for chunk in _body_chunks(response, chunk_bytes):
            body += chunk
            if len(body) > max_bytes:
                raise ResponseTooLarge(
                    f"Response exceeded the {max_bytes}-byte limit."
                )
            _check_deadline(started, total_seconds, monotonic)
    except BaseException:
        # Covers the two bounds above and anything requests raises mid-stream
        # (a chunked-encoding failure, a read timeout on one chunk). Abandoning
        # the generator stops the read; close() releases the connection.
        response.close()
        raise

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
    the very most.

    The exception mapping is requests' own, lifted from
    ``Response.iter_content``: without it a mid-stream failure would reach the
    call sites as a urllib3 exception, walk past every
    ``except requests.RequestException`` branch there, and land in a worker's
    blanket handler instead of the named one.
    """
    read1 = getattr(response.raw, "read1", None)
    if not callable(read1):
        for chunk in response.iter_content(chunk_size=1):
            if chunk:
                yield chunk
        return
    while True:
        try:
            chunk = read1(chunk_bytes, decode_content=True)
        except ProtocolError as exc:
            raise requests.exceptions.ChunkedEncodingError(exc) from exc
        except DecodeError as exc:
            raise requests.exceptions.ContentDecodingError(exc) from exc
        except ReadTimeoutError as exc:
            raise requests.exceptions.ConnectionError(exc) from exc
        except _Urllib3SSLError as exc:
            raise requests.exceptions.SSLError(exc) from exc
        if not chunk:
            return
        yield chunk


def _check_deadline(
    started: float, total_seconds: float, monotonic: Callable[[], float]
) -> None:
    if monotonic() - started > total_seconds:
        raise ResponseDeadlineExceeded(
            f"Response deadline exceeded ({total_seconds:g}s)."
        )
