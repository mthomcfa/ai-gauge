import logging

import pytest

from aigauge.webview.scraper import HeadlessScraper


def test_extractor_retry_limit_is_retryable_transport_error():
    assert "extractor retry limit exceeded" in HeadlessScraper._RETRYABLE_ERRORS


class _Stand_in:
    """Minimal stand-in: HeadlessScraper._on_js_result is called unbound.

    Subclassing would require QObject.__init__ and a real QWebEnginePage; the
    give-up path only touches these three attributes.
    """

    def __init__(self):
        self._finished = False
        self._extractor_reruns = 0
        self._max_extractor_reruns = 5
        self.finished_with: tuple[object, str] | None = None

    def _finish(self, result, error):
        self.finished_with = (result, error)
        self._finished = True


def test_exhausted_extractor_reruns_hand_back_the_last_payload():
    """The payload is the only record of what the page actually rendered.

    Passing None here meant a provider layout change surfaced as raw_keys=[]
    in the log and an empty "Copy diagnostics" - undiagnosable without a debug
    rebuild. That is what happened to Claude's usage dialog.
    """
    stand_in = _Stand_in()
    payload = {
        "__retry_after_ms": 1200,
        "__retry_reason": "usage dialog not ready",
        "body_text": "whatever Claude renders now",
        "logged_out": False,
    }
    # 5 reruns are allowed; the 6th gives up. _schedule_rerun is never reached
    # because the stand-in reports finished after the give-up.
    for _ in range(6):
        if stand_in._finished:
            break
        try:
            HeadlessScraper._on_js_result(stand_in, dict(payload))
        except AttributeError:
            # The rerun path needs Qt timers we deliberately do not provide.
            stand_in._finished = False

    assert stand_in.finished_with is not None, "never reached the give-up path"
    result, error = stand_in.finished_with
    assert error == "extractor retry limit exceeded"
    assert isinstance(result, dict), "the payload must survive the give-up path"
    assert result["body_text"] == "whatever Claude renders now"


class _LoadFailStandIn:
    """Stand-in carrying only what the load-failure path reads.

    Borrows the real _load_failure_context so the wiring test exercises the
    production implementation rather than a copy of it.
    """

    _load_failure_context = HeadlessScraper._load_failure_context
    _finish = HeadlessScraper._finish
    _is_resume_artifact = HeadlessScraper._is_resume_artifact
    _cleanup = lambda self: None  # noqa: E731 - no Qt objects to tear down

    def __init__(self):
        self._max_progress = 70
        self._last_load_status = "LoadStoppedStatus"
        self._last_load_error_code = -3
        self._last_load_error_domain = "InternalErrorDomain"
        self._last_load_error_string = "net::ERR_ABORTED"
        self._last_load_is_error_page = False
        self._url_change_count = 1
        self._provider = "claude"
        self._attempt = 1
        self._render_terminated = False
        self._started_at = 0.0
        self._timeout_ms = 25000
        self._max_attempts = 1
        self._RETRYABLE_ERRORS = ()

        class _Page:
            def url(self):
                return "https://claude.ai/new?session=SECRET#frag"

            def title(self):
                return "New chat - Claude"

        self._page = _Page()
        self._finished = False
        self.finished_with: tuple[object, str] | None = None
        self._url = "https://claude.ai/new"
        self._last_load_url = "https://claude.ai/new"

        outer = self

        class _Done:
            def emit(self, result, error):
                outer.finished_with = (result, error)

        self.done = _Done()

        class _Timer:
            def stop(self):
                pass

        self._timeout = _Timer()


def test_load_failure_context_carries_the_chromium_error_detail():
    """"page failed to load" alone cannot distinguish a Cloudflare challenge
    from a DNS failure from an aborted navigation. The snapshot must carry the
    detail, or the user has nothing to send but the phrase."""
    ctx = HeadlessScraper._load_failure_context(_LoadFailStandIn())

    assert ctx["load_failed"] is True
    assert ctx["load_error_string"] == "net::ERR_ABORTED"
    assert ctx["load_error_code"] == -3
    assert ctx["max_progress"] == 70
    assert ctx["url_changes"] == 1
    assert ctx["title"] == "New chat - Claude"


def test_load_failure_context_strips_query_and_fragment_from_the_url():
    # Provider auth hops put session material in query strings; this payload
    # reaches both the log and the clipboard.
    ctx = HeadlessScraper._load_failure_context(_LoadFailStandIn())

    assert ctx["page_url"] == "https://claude.ai/new"
    assert "SECRET" not in ctx["page_url"]


def _run_deferred(monkeypatch):
    """Capture what _on_load_finished schedules instead of running it."""
    scheduled: list = []
    monkeypatch.setattr(
        "aigauge.webview.scraper.QTimer.singleShot",
        lambda ms, cb: scheduled.append((ms, cb)),
    )
    return scheduled


def test_failed_load_actually_delivers_the_context_to_the_caller(monkeypatch):
    """Guards the wiring, not just the helper.

    Asserting _load_failure_context() in isolation passes even if
    _on_load_finished still hands back None - which is exactly the bug.
    """
    stand_in = _LoadFailStandIn()
    scheduled = _run_deferred(monkeypatch)
    HeadlessScraper._on_load_finished(stand_in, False)
    for _ms, callback in scheduled:
        callback()

    assert stand_in.finished_with is not None
    result, error = stand_in.finished_with
    assert error == "page failed to load"
    assert isinstance(result, dict), "the load-failure detail must reach the snapshot"
    assert result["load_error_string"] == "net::ERR_ABORTED"


def test_the_failure_detail_is_captured_after_chromium_reports_it(monkeypatch):
    """Found by cold-starting the real app against an unreachable network.

    Chromium emits loadFinished(False) BEFORE the loadingChanged carrying the
    error code, domain, string and isErrorPage. Finishing synchronously read
    those fields while they were still empty, so every real load failure was
    reported as load_error_code=0 / NoErrorDomain / '' - the feature emptying
    itself at the only moment it exists for. Observed live as
    net::ERR_CONNECTION_RESET arriving one line *after* the snapshot that
    should have carried it.
    """
    stand_in = _LoadFailStandIn()
    # Nothing known yet - the state at the instant loadFinished fires.
    stand_in._last_load_status = "LoadStartedStatus"
    stand_in._last_load_error_code = 0
    stand_in._last_load_error_domain = "NoErrorDomain"
    stand_in._last_load_error_string = ""
    stand_in._last_load_is_error_page = False

    scheduled = _run_deferred(monkeypatch)
    HeadlessScraper._on_load_finished(stand_in, False)

    assert stand_in.finished_with is None, "finished before the detail could arrive"
    assert scheduled and scheduled[0][0] == 0, "must yield exactly one event-loop turn"

    # Chromium now delivers the real reason.
    stand_in._last_load_status = "LoadFailedStatus"
    stand_in._last_load_error_code = -101
    stand_in._last_load_error_domain = "ConnectionErrorDomain"
    stand_in._last_load_error_string = "net::ERR_CONNECTION_RESET"
    stand_in._last_load_is_error_page = True

    scheduled[0][1]()

    result, _error = stand_in.finished_with
    assert result["load_error_code"] == -101
    assert result["load_error_string"] == "net::ERR_CONNECTION_RESET"
    assert result["load_error_domain"] == "ConnectionErrorDomain"
    assert result["is_error_page"] is True
    assert result["load_status"] == "LoadFailedStatus"


@pytest.mark.parametrize(
    "error",
    [
        "timeout",
        "page failed to load",
        "extractor returned null",
        "extractor retry limit exceeded",
        "some future error nobody has seen yet",
    ],
)
@pytest.mark.parametrize("payload", [None, "", False, "extractor returned a string"])
def test_every_payloadless_failure_carries_context(error, payload):
    """The chokepoint, not the call sites.

    Attaching context at each call site meant patching them one at a time as
    each failure mode showed up in the wild: "page failed to load" was fixed
    while "timeout" and "extractor returned null" kept reaching the user as
    raw={}. Handling it in _finish means a future error path cannot miss it.

    Parametrised over non-dict results as well as None, because ScrapeRunner
    replaces anything that is not a dict with raw={} - so a condition testing
    only "is None" would leave the same hole open for the next error path.
    """
    stand_in = _LoadFailStandIn()
    HeadlessScraper._finish(stand_in, payload, error)

    assert stand_in.finished_with is not None
    result, reported = stand_in.finished_with
    assert reported == error
    assert isinstance(result, dict), f"{error!r} reached the caller with no context"
    assert result["failure"] == error
    assert result["load_error_string"] == "net::ERR_ABORTED"
    assert "elapsed_s" in result


def test_a_timeout_measured_across_a_suspend_is_named_as_one(caplog):
    """A laptop resumed after two days reported elapsed_s: 228477.

    The scraper bounds itself with a wall-clock timer, so a scrape in flight
    when the lid closes reports `timeout` on resume with a nonsense elapsed.
    That was an ordinary ERROR snapshot, which then armed the fast retry - one
    spurious failure per provider on every resume. Naming it is enough: the
    scrape still failed, it just did not earn a retry it had not lost.
    """
    import logging

    stand_in = _LoadFailStandIn()
    stand_in._started_at = 0.0  # monotonic zero: elapsed is hours, not seconds

    with caplog.at_level(logging.WARNING, logger="aigauge.webview.scraper"):
        HeadlessScraper._finish(stand_in, None, "timeout")

    result, _error = stand_in.finished_with
    assert result["classification"] == "resume_artifact"
    assert "classification=resume_artifact" in caplog.text


def test_an_ordinary_timeout_is_not_called_a_resume_artifact():
    import time

    stand_in = _LoadFailStandIn()
    # Timed out at its own budget, as a slow page does.
    stand_in._started_at = time.monotonic() - 26.0

    HeadlessScraper._finish(stand_in, None, "timeout")

    result, _error = stand_in.finished_with
    assert "classification" not in result


def test_a_page_that_failed_to_load_is_never_a_resume_artifact():
    """Only a timeout can be measured across a suspend; every other failure
    is reported by Chromium at the moment it happens."""
    stand_in = _LoadFailStandIn()
    stand_in._started_at = 0.0

    HeadlessScraper._finish(stand_in, None, "page failed to load")

    result, _error = stand_in.finished_with
    assert "classification" not in result


def test_success_is_never_given_a_failure_context():
    stand_in = _LoadFailStandIn()
    payload = {"body_text": "real data"}
    HeadlessScraper._finish(stand_in, payload, "")

    assert stand_in.finished_with == (payload, "")


def test_a_real_payload_is_not_replaced_by_failure_context():
    # The extractor-exhausted path passes its own payload; it is more useful
    # than load context and must survive.
    stand_in = _LoadFailStandIn()
    payload = {"body_text": "what the page rendered"}
    HeadlessScraper._finish(stand_in, payload, "extractor retry limit exceeded")

    result, _ = stand_in.finished_with
    assert result is payload


@pytest.mark.parametrize("cap", [1, 5, 20])
def test_the_extractor_rerun_budget_is_honoured(cap):
    """Behavioural, because the number is what decides success.

    Claude's settings route resolves i18n, org, feature, memory, MCP and
    marketplace endpoints before usage. The default budget - a 7s wait plus
    five 1.2s reruns - gave up at roughly 13s while body_text was still
    "Loading...", so the page never got the chance to render the rows.
    """
    stand_in = _Stand_in()
    stand_in._max_extractor_reruns = cap
    payload = {"__retry_after_ms": 1200, "body_text": "Loading..."}

    runs = 0
    for _ in range(cap + 5):
        if stand_in._finished:
            break
        runs += 1
        try:
            HeadlessScraper._on_js_result(stand_in, dict(payload))
        except AttributeError:
            stand_in._finished = False  # rerun path needs Qt timers

    assert stand_in.finished_with is not None, "never gave up"
    assert runs == cap + 1, f"gave up after {runs} runs with a cap of {cap}"


def test_claude_asks_for_a_longer_hydration_budget_than_the_default():
    import inspect

    from aigauge.providers import claude

    source = inspect.getsource(claude)
    assert "max_extractor_reruns=" in source, "Claude must raise the default budget"
    default = inspect.signature(HeadlessScraper.__init__).parameters
    assert default["max_extractor_reruns"].default == 5, "default must stay conservative"


class _DeadPageStandIn(_LoadFailStandIn):
    """A scrape whose page Qt has already destroyed under it.

    `purge_profile` calls `deleteLater()` on the cached profile, and Qt
    requires a profile to outlive its pages - so reading the page of a scrape
    that was live at that moment raises the RuntimeError PyQt uses for a
    wrapper whose C++ half is gone.
    """

    def __init__(self):
        super().__init__()

        class _Dead:
            def url(self):
                raise RuntimeError(
                    "wrapped C/C++ object of type QuietWebEnginePage has been deleted"
                )

            def title(self):
                raise RuntimeError(
                    "wrapped C/C++ object of type QuietWebEnginePage has been deleted"
                )

        self._page = _Dead()


def test_a_scrape_whose_page_is_gone_still_reports_back(caplog):
    """`done` is what releases the account's live-scrape guard and hands the
    snapshot back. It used to be the casualty of a diagnostic: the failure
    context and the `scrape fail` line both read `self._page`, and a page
    destroyed under a live scrape made `_finish` raise before its emit -
    after setting `_finished`, so the timeout's own call did nothing either.
    The account then stayed busy for the life of the process."""
    stand_in = _DeadPageStandIn()

    with caplog.at_level(logging.WARNING, logger="aigauge"):
        HeadlessScraper._finish(stand_in, None, "timeout")

    assert stand_in.finished_with is not None, "done was never emitted"
    result, reported = stand_in.finished_with
    assert reported == "timeout"
    assert result["load_failed"] is True and result["failure"] == "timeout", (
        "the payloadless failure lost its shape as well as its detail"
    )
    assert "diagnostics could not be read" in caplog.text


def test_a_successful_scrape_survives_a_page_that_cannot_be_read(caplog):
    """The same for the OK path: the `scrape ok` line reads the page too, and
    a payload that was extracted must not be lost to a log format."""
    stand_in = _DeadPageStandIn()

    with caplog.at_level(logging.WARNING, logger="aigauge"):
        HeadlessScraper._finish(stand_in, {"usage": 42}, "")

    assert stand_in.finished_with == ({"usage": 42}, "")
