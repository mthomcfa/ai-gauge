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

    def __init__(self):
        self._max_progress = 70
        self._last_load_status = "LoadStoppedStatus"
        self._last_load_error_code = -3
        self._last_load_error_domain = "InternalErrorDomain"
        self._last_load_error_string = "net::ERR_ABORTED"
        self._last_load_is_error_page = False
        self._url_change_count = 1
        self._provider = "claude"

        class _Page:
            def url(self):
                return "https://claude.ai/new?session=SECRET#frag"

            def title(self):
                return "New chat - Claude"

        self._page = _Page()
        self._finished = False
        self.finished_with: tuple[object, str] | None = None

    def _finish(self, result, error):
        self.finished_with = (result, error)
        self._finished = True


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


def test_failed_load_actually_delivers_the_context_to_the_caller():
    """Guards the wiring, not just the helper.

    Asserting _load_failure_context() in isolation passes even if
    _on_load_finished still hands back None - which is exactly the bug.
    """
    stand_in = _LoadFailStandIn()
    HeadlessScraper._on_load_finished(stand_in, False)

    assert stand_in.finished_with is not None
    result, error = stand_in.finished_with
    assert error == "page failed to load"
    assert isinstance(result, dict), "the load-failure detail must reach the snapshot"
    assert result["load_error_string"] == "net::ERR_ABORTED"
