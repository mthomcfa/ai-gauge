import logging
import time

import pytest

from aigauge.webview.scraper import HeadlessScraper

# How far back a stand-in's clock is pushed to stand for "the lid was closed
# mid-scrape". It is an offset from `time.monotonic()`, never an absolute
# value: `_is_resume_artifact` compares `monotonic() - _started_at` against
# three times the scrape budget, so a literal 0.0 measures the *host's
# uptime*. A CI runner 74.9 s into its first boot put that below the 75 s
# threshold and the classification never happened, while every long-lived
# runner passed.
_A_SUSPEND_AGO = 300_000.0


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
    _on_load_failed = HeadlessScraper._on_load_failed
    _load_was_superseded = HeadlessScraper._load_was_superseded
    _on_loading_changed = HeadlessScraper._on_loading_changed
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
        # What the real scraper does at construction, so elapsed is ~0 and
        # nothing is a resume artifact unless a test asks for one.
        self._started_at = time.monotonic()
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
        self._soft_ready = _Timer()
        self._extracting = False
        self._last_failure_aborted = False
        self._expect_abort = False


class _LoadInfo:
    """What Qt's loadingChanged hands over, as the handler reads it."""

    def __init__(self, status, code=0, string="", url="https://claude.ai/new"):
        self._status = type("S", (), {"name": status})()
        self._code, self._string, self._url = code, string, url

    def status(self):
        return self._status

    def url(self):
        return self._url

    def errorCode(self):  # noqa: N802 - Qt's name
        return self._code

    def errorDomain(self):  # noqa: N802
        return type("D", (), {"name": "InternalErrorDomain"})()

    def errorString(self):  # noqa: N802
        return self._string

    def isErrorPage(self):  # noqa: N802
        return False


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
    # A load the network failed. ERR_ABORTED, the stand-in's default, is a
    # load something replaced, and that one is waited out - see below.
    stand_in._last_load_status = "LoadFailedStatus"
    stand_in._last_load_error_code = -101
    stand_in._last_load_error_domain = "ConnectionErrorDomain"
    stand_in._last_load_error_string = "net::ERR_CONNECTION_RESET"
    scheduled = _run_deferred(monkeypatch)
    HeadlessScraper._on_load_finished(stand_in, False)
    for _ms, callback in scheduled:
        callback()

    assert stand_in.finished_with is not None
    result, error = stand_in.finished_with
    assert error == "page failed to load"
    assert isinstance(result, dict), "the load-failure detail must reach the snapshot"
    assert result["load_error_string"] == "net::ERR_CONNECTION_RESET"


@pytest.mark.parametrize(
    "code,string",
    [(-3, "net::ERR_ABORTED"), ("-3", ""), (0, "net::ERR_ABORTED")],
    ids=["code-and-string", "code-only", "string-only"],
)
def test_the_abort_of_the_load_the_retry_stopped_is_waited_out(
    monkeypatch, caplog, code, string
):
    """Observed on 2026-10-02, on every Claude retry: the retry stops the
    attempt that timed out, and that load's ERR_ABORTED arrived in the new
    attempt and failed it as "page failed to load" before it had loaded
    anything. In the order Chromium reports it: the abort, then the new
    load's start - which overwrites the error fields - then the deferred
    check. The abort is recorded when it arrives, so the check still sees it.
    """
    stand_in = _LoadFailStandIn()
    stand_in._expect_abort = True  # set by the retry that stopped the load
    scheduled = _run_deferred(monkeypatch)

    with caplog.at_level(logging.INFO, logger="aigauge"):
        stand_in._on_loading_changed(_LoadInfo("LoadStoppedStatus", code, string))
        HeadlessScraper._on_load_finished(stand_in, False)
        stand_in._on_loading_changed(_LoadInfo("LoadStartedStatus"))
        for _ms, callback in scheduled:
            callback()

    assert stand_in.finished_with is None, "the retry failed on the load it replaced"
    assert stand_in._expect_abort is False, "the expected abort was not consumed"
    assert "scrape load superseded" in caplog.text


def test_an_abort_the_retry_did_not_cause_fails_at_once(monkeypatch):
    """A page calling window.stop(), say. Nothing replaces that load, so
    waiting for one only costs the whole timeout; it fails at once, as it did
    before the retry learned to expect its own abort."""
    stand_in = _LoadFailStandIn()
    scheduled = _run_deferred(monkeypatch)
    stand_in._on_loading_changed(_LoadInfo("LoadStoppedStatus", -3, "net::ERR_ABORTED"))
    HeadlessScraper._on_load_finished(stand_in, False)
    for _ms, callback in scheduled:
        callback()

    assert stand_in.finished_with is not None
    assert stand_in.finished_with[1] == "page failed to load"


def test_only_one_abort_is_expected(monkeypatch):
    stand_in = _LoadFailStandIn()
    stand_in._expect_abort = True
    scheduled = _run_deferred(monkeypatch)
    for _ in range(2):
        stand_in._on_loading_changed(_LoadInfo("LoadStoppedStatus", -3, "net::ERR_ABORTED"))
        HeadlessScraper._on_load_finished(stand_in, False)
    for _ms, callback in scheduled:
        callback()

    assert stand_in.finished_with is not None, "a second abort was waited out too"


@pytest.mark.parametrize(
    "code,string",
    [(-102, "net::ERR_CONNECTION_REFUSED"), (-105, "net::ERR_NAME_NOT_RESOLVED"),
     ("", ""), (-30, "net::ERR_ABORTED_ELSEWHERE")],
    ids=["refused", "dns", "no-detail", "lookalike"],
)
def test_a_load_the_network_failed_still_fails_at_once(monkeypatch, code, string):
    """Even while the retry expects its abort: what is waited out is
    ERR_ABORTED alone - code -3 or Chromium's exact string. A refused
    connection, a name that does not resolve, a failure Chromium gave no
    detail for, or a string that merely contains the token ends the attempt
    immediately rather than waiting out the timeout."""
    stand_in = _LoadFailStandIn()
    stand_in._expect_abort = True
    scheduled = _run_deferred(monkeypatch)
    stand_in._on_loading_changed(_LoadInfo("LoadFailedStatus", code, string))
    HeadlessScraper._on_load_finished(stand_in, False)
    for _ms, callback in scheduled:
        callback()

    assert stand_in.finished_with is not None
    assert stand_in.finished_with[1] == "page failed to load"


@pytest.mark.parametrize(
    "status,expected",
    [("LoadStartedStatus", True), ("LoadSucceededStatus", False),
     ("LoadFailedStatus", False), ("", False)],
)
def test_the_retry_expects_an_abort_only_from_a_load_still_in_progress(status, expected):
    """Stopping a load that already ended causes no abort; expecting one
    anyway would let a later real failure be waited out."""
    stand_in = _LoadFailStandIn()
    stand_in._RETRYABLE_ERRORS = ("timeout",)
    stand_in._max_attempts = 2
    stand_in._last_load_status = status
    calls: list = []
    stand_in._view = type("V", (), {"stop": lambda self: calls.append("stop")})()
    stand_in._begin_attempt = lambda: calls.append("begin")

    HeadlessScraper._finish(stand_in, None, "timeout")

    assert calls == ["stop", "begin"]
    assert stand_in._expect_abort is expected
    assert stand_in.finished_with is None


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
    stand_in._started_at = time.monotonic() - _A_SUSPEND_AGO  # ~3.5 days

    with caplog.at_level(logging.WARNING, logger="aigauge.webview.scraper"):
        HeadlessScraper._finish(stand_in, None, "timeout")

    result, _error = stand_in.finished_with
    assert result["classification"] == "resume_artifact"
    assert "classification=resume_artifact" in caplog.text


def test_an_ordinary_timeout_is_not_called_a_resume_artifact():
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
    stand_in._started_at = time.monotonic() - _A_SUSPEND_AGO

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


def _stand_in_with_title(title: str):
    stand_in = _LoadFailStandIn()

    class _Page:
        def url(self):
            return "https://claude.ai/usage"

        def title(self):
            return title

    stand_in._page = _Page()  # noqa: SLF001
    return stand_in


@pytest.mark.parametrize("error", ["", "timeout"])
def test_a_page_cannot_write_a_megabyte_into_one_log_record(
    monkeypatch, caplog, error
):
    """`document.title` and the extractor's key names are chosen by the page.

    Neither was capped, and the healthy line is at INFO, so one scrape of a
    hostile or merely broken page overwrote the rotating 512 KiB x 3 log -
    the file the error dialog asks the user to attach. Measured on the tree
    before this cap, with a 1 MB title and 10 000 keys of 1 000 characters:
    11 079 134 characters for `scrape ok` and 1 000 367 for `scrape fail`,
    21x and 1.9x the whole rotation.
    """
    monkeypatch.setattr(
        "aigauge.webview.scraper.QTimer.singleShot", lambda ms, cb: None
    )
    stand_in = _stand_in_with_title("T" * 1_000_000)
    result = {("K" * 1_000) + str(index): 1 for index in range(10_000)}

    with caplog.at_level(logging.INFO, logger="aigauge.scraper"):
        caplog.clear()
        HeadlessScraper._finish(stand_in, result if not error else None, error)

    assert stand_in.finished_with is not None, "the scrape never reported"
    worst = max(len(record.getMessage()) for record in caplog.records)
    assert worst < 10_000, f"one record was {worst} characters"
    line = "\n".join(record.getMessage() for record in caplog.records)
    assert "TTTT" in line, "the title was dropped instead of clipped"
    if not error:
        assert "more" in line, "the key list was truncated without saying so"


def test_the_scrapers_key_walk_cannot_raise_and_its_class_name_is_bounded():
    """`_result_keys_for_log` reaches the result's own keys.

    `_clip(key, ...)` ran the key's `__bool__` and `__str__` unguarded, where
    app.py deliberately routes the same walk through a guard - so one key
    that refuses to be printed cost the whole `scrape ok` record, which
    `_finish` then replaces with "the diagnostics could not be read". The
    non-dict branch printed `type(result).__name__` whole, and a class name
    is not bounded by anything: measured 1 000 000 characters, now 60.
    """
    from aigauge.webview.scraper import _result_keys_for_log

    class _KeyStrRaises:
        def __str__(self):
            raise ValueError("this key refuses to be printed")

        def __hash__(self):
            return 11

    # One key that refuses to print costs that key its name and nothing
    # else: the outer guard added beside this one would otherwise mask a
    # missing per-key guard by throwing the whole list away.
    line = _result_keys_for_log({_KeyStrRaises(): 1, "usage": 2, "limit": 3})
    assert "'usage'" in line and "'limit'" in line, line
    assert "'<key>'" in line, line

    huge = type("N" * 1_000_000, (), {})()
    assert len(_result_keys_for_log(huge)) == 60


def test_the_scrapers_key_walk_survives_a_result_that_refuses_to_be_walked(
    caplog,
):
    """Guarded end to end, the way app.py's twin is.

    `_key_text` covers a key that refuses to be printed; the `for key in
    result` walk itself runs the payload's `__iter__`, and a `dict` subclass
    can refuse that too - so the same refusal still escaped, `_finish`
    caught it, and the whole `scrape ok` record of a scrape that had worked
    was replaced by "the diagnostics could not be read". app.py's
    `_raw_keys_for_log` answers `[]` for the identical payload.
    """
    from aigauge.webview.scraper import _result_keys_for_log

    class _IterRaises(dict):
        def __iter__(self):
            raise RuntimeError("this result refuses to be iterated")

    assert _result_keys_for_log(_IterRaises(a=1)) == "[]"

    # And through the real `_finish`, which is where it cost the record.
    stand_in = _stand_in_with_title("short")
    with caplog.at_level(logging.INFO, logger="aigauge.scraper"):
        caplog.clear()
        HeadlessScraper._finish(stand_in, _IterRaises(a=1), "")

    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("scrape ok") for message in messages), messages


def test_the_scrapers_load_error_string_is_bounded(caplog):
    """Chromium's `errorString` is a Qt string-table message - 49 characters
    for an HTTP failure against a real QtWebEngine, not the server's reason
    phrase - so this closes an assumption rather than a hole. It is still
    the largest argument on `scrape fail` with no cap of its own: forced to
    500 000 characters it produced a 500 353-character record, 0.95x the
    whole rotation."""
    stand_in = _stand_in_with_title("short")
    stand_in._last_load_error_string = "E" * 500_000  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="aigauge.scraper"):
        caplog.clear()
        HeadlessScraper._finish(stand_in, None, "page failed to load")

    assert caplog.records, "the failure logged nothing at all"
    worst = max(len(record.getMessage()) for record in caplog.records)
    assert worst < 2_000, f"one record was {worst} characters"


def test_the_scrapers_url_field_is_bounded_too():
    """`_safe_url` is the other page-controlled field on those lines."""
    from aigauge.webview.scraper import _safe_url

    assert len(_safe_url("https://claude.ai/" + "p" * 100_000)) <= 300
    assert len(_safe_url("not a url at all " + "q" * 100_000)) <= 300


class _SoftReadStandIn:
    """Carries what the early read, the load event and a retry touch.

    The methods are the production ones, so these tests exercise the real
    state machine; only Qt (page, timers) is replaced.
    """

    _on_soft_ready = HeadlessScraper._on_soft_ready
    _on_ready_probe = HeadlessScraper._on_ready_probe
    _run_extractor = HeadlessScraper._run_extractor
    _on_js_result = HeadlessScraper._on_js_result
    _on_load_finished = HeadlessScraper._on_load_finished
    _schedule_extractor = HeadlessScraper._schedule_extractor
    _begin_attempt = HeadlessScraper._begin_attempt
    _reload_for = HeadlessScraper._reload_for
    _on_committed = HeadlessScraper._on_committed

    def __init__(
        self,
        url="https://claude.ai/new#settings/usage",
        page_url="about:blank",
        committed=None,
    ):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtWebEngineCore import QWebEnginePage

        self._finished = False
        self._extracting = False
        self._attempt = 1
        self._provider = "claude"
        self._max_progress = 80
        self._last_load_status = "LoadStartedStatus"
        self._last_load_is_error_page = False
        self._extractor_js = "EXTRACT"
        self._wait_ms = 3000
        self._url = url
        self._timeout_ms = 40000
        self._soft_ready_ms = 13333
        self._extractor_reruns = 0
        self._max_extractor_reruns = 5
        self.js_runs: list = []
        self.finished_with = None
        outer = self

        class _Page:
            WebAction = QWebEnginePage.WebAction

            def __init__(page):
                page.loaded = []
                page.actions = []
                page.current = QUrl(page_url)
                page.committed = None if committed is None else QUrl(committed)

            def history(page):
                entry = page.committed

                class _History:
                    def count(self):
                        return 0 if entry is None else 1

                    def currentItem(self):  # noqa: N802 - Qt's name
                        return type("I", (), {"url": lambda self: entry})()

                return _History()

            def url(page):
                return page.current

            def title(page):
                return "Claude"

            def runJavaScript(page, js, callback):
                outer.js_runs.append((js, callback))

            def load(page, target):
                page.loaded.append(target.toString())

            def triggerAction(page, action):
                page.actions.append(action)

        class _Timer:
            def __init__(timer):
                timer.starts = []

            def stop(timer):
                pass

            def start(timer, ms):
                timer.starts.append(ms)

        self._page = _Page()
        self._timeout = _Timer()
        self._soft_ready = _Timer()

    def _finish(self, result, error):
        self.finished_with = (result, error)
        self._finished = True


def test_a_page_still_loading_is_read_once_its_document_is_there(monkeypatch):
    """The 2026-10-02 failure: claude.ai rendered the usage and one request on
    the page never settled, so Chromium's load-finished never fired and the
    extractor never ran. The early read asks the page whether its document
    is parsed, and reads it."""
    from aigauge.webview.scraper import _READY_PROBE_JS

    stand_in = _SoftReadStandIn()
    stand_in._on_soft_ready()

    assert [js for js, _cb in stand_in.js_runs] == [_READY_PROBE_JS]
    stand_in.js_runs[0][1](["interactive", "https://"])

    assert stand_in._extracting is True
    assert [js for js, _cb in stand_in.js_runs][1] == "EXTRACT"
    stand_in.js_runs[1][1]({"session": {"percent": 8}})
    assert stand_in.finished_with == ({"session": {"percent": 8}}, "")


@pytest.mark.parametrize(
    "probe",
    [["loading", "https://"], ["complete", "about://"], ["interactive", "data://"],
     None, "interactive", ["interactive"], [1, 2], {"0": "complete"}],
    ids=["still-parsing", "about-blank", "data-url", "null", "string", "short",
         "wrong-types", "dict"],
)
def test_the_early_read_waits_for_a_real_document(probe):
    """An extractor run on a blank page finds no usage on it, and that reads as
    a layout change rather than the slow load it is. So it waits, and asks
    again a second later; the timeout still bounds the wait."""
    stand_in = _SoftReadStandIn()
    stand_in._on_soft_ready()
    stand_in.js_runs[0][1](probe)

    assert stand_in._extracting is False
    assert len(stand_in.js_runs) == 1, "the extractor ran on a page with no document"
    assert stand_in._soft_ready.starts == [1000]


def test_the_load_event_and_the_early_read_start_one_reader_between_them(monkeypatch):
    scheduled: list = []
    monkeypatch.setattr(
        "aigauge.webview.scraper.QTimer.singleShot",
        lambda ms, cb: scheduled.append((ms, cb)),
    )
    early = _SoftReadStandIn()
    early._on_soft_ready()
    early.js_runs[0][1](["interactive", "https://"])
    early._on_load_finished(True)
    assert scheduled == [], "the load event started a second reader"

    late = _SoftReadStandIn()
    late._on_load_finished(True)
    assert len(scheduled) == 1 and scheduled[0][0] == late._wait_ms
    late._on_soft_ready()
    assert late.js_runs == [], "the early read started a second reader"


def test_a_previous_attempts_reads_do_not_run_in_the_next():
    """A retry starts its own reader. The timed-out attempt's pending reruns,
    probes and results belong to the page it gave up on."""
    stand_in = _SoftReadStandIn()
    stand_in._attempt = 2

    stand_in._run_extractor(1)
    assert stand_in.js_runs == []
    stand_in._on_js_result({"session": {"percent": 8}}, 1)
    assert stand_in.finished_with is None
    stand_in._on_ready_probe(["interactive", "https://"], 1)
    assert stand_in._extracting is False


def test_a_retry_on_the_page_it_already_shows_reloads_rather_than_scrolls(monkeypatch):
    """claude.ai/new#settings/usage. load() of the URL a page already shows,
    fragment and all, is a same-document navigation: it scrolls and does not
    reload, so the retry re-read the page that had just failed."""
    from PyQt6.QtWebEngineCore import QWebEnginePage

    scheduled: list = []
    monkeypatch.setattr(
        "aigauge.webview.scraper.QTimer.singleShot",
        lambda ms, cb: scheduled.append((ms, cb)),
    )
    target = "https://claude.ai/new#settings/usage"
    stand_in = _SoftReadStandIn(page_url=target, committed=target)
    stand_in._begin_attempt()  # the retry: attempt 2
    assert stand_in._page.loaded == []
    assert [ms for ms, _cb in scheduled] == [0]
    scheduled.pop()[1]()
    assert stand_in._page.actions == [QWebEnginePage.WebAction.Reload]


@pytest.mark.parametrize(
    "here,committed",
    [
        ("https://claude.ai/new#settings/usage", None),
        ("https://claude.ai/new", "https://claude.ai/new"),
    ],
    ids=["document-never-committed", "page-dropped-the-fragment"],
)
def test_a_retry_anywhere_else_loads_the_url(here, committed):
    """While a document is still being fetched, page.url() is the URL being
    loaded and nothing is committed: a reload there sends no request at all,
    so the retry was dead for its whole timeout. And from /new, a page that
    dropped the fragment, load() is a fragment change - the step that opens
    Claude's view - where a reload would only reload /new."""
    target = "https://claude.ai/new#settings/usage"
    stand_in = _SoftReadStandIn(page_url=here, committed=committed)
    stand_in._begin_attempt()
    assert stand_in._page.loaded == [target]
    assert stand_in._page.actions == []


@pytest.mark.parametrize(
    "here,url",
    [
        ("about:blank", "https://claude.ai/new#settings/usage"),
        ("https://claude.ai/login", "https://claude.ai/new#settings/usage"),
        ("https://chatgpt.com/codex", "https://chatgpt.com/codex/cloud/settings/analytics#personal-usage"),
        ("https://claude.ai/settings/usage", "https://claude.ai/settings/usage"),
    ],
    ids=["first-load", "another-page", "another-path", "no-fragment"],
)
def test_every_other_attempt_loads_the_url(here, url):
    stand_in = _SoftReadStandIn(url=url, page_url=here)
    stand_in._begin_attempt()
    assert stand_in._page.loaded == [url]
    assert stand_in._page.actions == []
    assert stand_in._soft_ready.starts == [stand_in._soft_ready_ms]


def test_each_attempt_starts_without_a_reader(monkeypatch):
    """A retry after an early read must be able to start its own; with the
    flag carried over, no reader ever started again and every retry was dead."""
    stand_in = _SoftReadStandIn()
    stand_in._extracting = True
    stand_in._begin_attempt()
    assert stand_in._extracting is False


def test_a_rerun_scheduled_in_one_attempt_does_not_run_in_the_next(monkeypatch):
    scheduled: list = []
    monkeypatch.setattr(
        "aigauge.webview.scraper.QTimer.singleShot",
        lambda ms, cb: scheduled.append((ms, cb)),
    )
    stand_in = _SoftReadStandIn()
    stand_in._schedule_extractor(500)
    stand_in._attempt = 2  # the retry starts before the rerun fires
    scheduled.pop()[1]()

    assert stand_in.js_runs == [], "attempt 1's rerun read attempt 2's page"


def test_without_the_early_read_an_attempt_waits_for_the_load_event():
    stand_in = _SoftReadStandIn()
    stand_in._soft_ready_ms = None
    stand_in._begin_attempt()

    assert stand_in._soft_ready.starts == []
    assert stand_in._page.loaded == [stand_in._url]


def test_the_early_read_leaves_claude_room_to_poll_inside_its_timeout():
    from aigauge.providers.claude import SCRAPE_TIMEOUT_MS
    from aigauge.webview.scraper import default_soft_ready_ms

    # Claude polls 20 times at up to 1.2 s (providers/claude.py).
    assert default_soft_ready_ms(3000, SCRAPE_TIMEOUT_MS) + 20 * 1200 <= SCRAPE_TIMEOUT_MS


@pytest.mark.parametrize(
    "wait_ms,timeout_ms,expected",
    [(3000, 40000, 13333), (7000, 25000, 8333), (4000, 25000, 8333),
     (20000, 25000, 20000), (60000, 25000, 25000), (0, 0, 0)],
)
def test_the_early_read_default(wait_ms, timeout_ms, expected):
    from aigauge.webview.scraper import default_soft_ready_ms

    assert default_soft_ready_ms(wait_ms, timeout_ms) == expected
