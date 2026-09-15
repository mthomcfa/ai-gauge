import logging
from types import SimpleNamespace
from typing import Any

import pytest

from aigauge.models import SnapshotStatus, UsageSnapshot
from aigauge.providers import _scrape_runner as runner_module
from aigauge.providers._scrape_runner import ScrapeRunner


class _FakeDoneSignal:
    def __init__(self):
        self._slot = None

    def connect(self, slot):
        self._slot = slot

    def emit(self, result, error):
        assert self._slot is not None, "no slot connected"
        self._slot(result, error)


class _FakeScraper:
    """Stand-in for HeadlessScraper that fires `done` synchronously on demand."""

    instances: list["_FakeScraper"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.done = _FakeDoneSignal()
        _FakeScraper.instances.append(self)


@pytest.fixture
def fake_scraper(monkeypatch):
    _FakeScraper.instances.clear()
    # The live-scrape registry is module state keyed by account id, so a test
    # that leaves an entry behind would make the next one refuse.
    runner_module._ACTIVE_ACCOUNTS.clear()  # noqa: SLF001
    monkeypatch.setattr(runner_module, "HeadlessScraper", _FakeScraper)
    yield _FakeScraper
    _FakeScraper.instances.clear()
    runner_module._ACTIVE_ACCOUNTS.clear()  # noqa: SLF001


def _ok_snapshot() -> UsageSnapshot:
    return UsageSnapshot(provider="x", status=SnapshotStatus.OK)


def _err_snapshot(reason: str = "layout") -> UsageSnapshot:
    return UsageSnapshot(provider="x", status=SnapshotStatus.ERROR, error=reason)


def test_scrape_runner_passes_ok_snapshot_through(fake_scraper):
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="x",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        build_max_attempts=2,
    )
    rn.run(received.append)

    assert len(fake_scraper.instances) == 1
    fake_scraper.instances[0].done.emit({"any": "payload"}, "")

    assert len(received) == 1
    assert received[0].status == SnapshotStatus.OK
    # No retry should have been scheduled.
    assert len(fake_scraper.instances) == 1


def test_a_resume_artifact_reaches_the_snapshot_as_one(fake_scraper):
    """The scraper names a timeout that was measured across a machine
    suspend; the snapshot has to carry the label, or the scheduler above
    cannot tell it from a provider that genuinely failed."""
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="claude",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
    )
    rn.run(received.append)

    fake_scraper.instances[0].done.emit(
        {"load_failed": True, "classification": "resume_artifact"}, "timeout"
    )

    assert received[0].status == SnapshotStatus.ERROR
    assert received[0].error_class == "resume_artifact"


def test_an_ordinary_failure_carries_no_error_class(fake_scraper):
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="claude",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
    )
    rn.run(received.append)

    fake_scraper.instances[0].done.emit({"load_failed": True}, "timeout")

    assert received[0].error_class is None


def test_scrape_runner_retries_on_build_error(fake_scraper):
    received: list[UsageSnapshot] = []
    calls: list[dict[str, Any]] = []

    def _build(payload):
        calls.append(payload)
        # First call fails, second succeeds — mirrors the partial-render
        # scenario that motivated the runner.
        return _err_snapshot() if len(calls) == 1 else _ok_snapshot()

    rn = ScrapeRunner(
        account_id="x",
        url="http://example",
        extractor_js="",
        build=_build,
        log=logging.getLogger("test"),
        build_max_attempts=2,
    )
    rn.run(received.append)

    fake_scraper.instances[0].done.emit({"first": True}, "")
    assert received == [], "first ERROR should trigger retry, not deliver"
    assert len(fake_scraper.instances) == 2, "second scrape should have started"

    fake_scraper.instances[1].done.emit({"second": True}, "")
    assert len(received) == 1
    assert received[0].status == SnapshotStatus.OK


def test_scrape_runner_stops_retrying_after_limit(fake_scraper):
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="x",
        url="http://example",
        extractor_js="",
        build=lambda payload: _err_snapshot("layout"),
        log=logging.getLogger("test"),
        build_max_attempts=2,
    )
    rn.run(received.append)

    fake_scraper.instances[0].done.emit({}, "")
    fake_scraper.instances[1].done.emit({}, "")

    assert len(fake_scraper.instances) == 2, "should not retry past build_max_attempts"
    assert len(received) == 1
    assert received[0].status == SnapshotStatus.ERROR


def test_scrape_runner_surfaces_transport_error_without_build(fake_scraper):
    received: list[UsageSnapshot] = []
    build_calls = []
    rn = ScrapeRunner(
        account_id="x",
        url="http://example",
        extractor_js="",
        build=lambda payload: (build_calls.append(payload), _ok_snapshot())[1],
        log=logging.getLogger("test"),
        build_max_attempts=2,
    )
    rn.run(received.append)

    fake_scraper.instances[0].done.emit(None, "timeout")

    assert build_calls == [], "build should be skipped on transport error"
    assert len(received) == 1
    assert received[0].status == SnapshotStatus.ERROR
    assert received[0].error == "timeout"


def test_error_snapshot_keeps_the_payload_for_diagnosis(fake_scraper):
    """A failed scrape must still carry what the extractor saw.

    When the extractor exhausted its reruns the scraper passed None, so the
    snapshot reached the log and "Copy diagnostics" with raw={} - and a
    provider layout change became impossible to diagnose without rebuilding
    the app with extra logging. That is exactly what happened to Claude.
    """
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="claude",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        build_max_attempts=1,
    )
    rn.run(received.append)

    payload = {
        "body_text": "Plan usage limits ... whatever Claude renders now",
        "url": "https://claude.ai/new",
        "logged_out": False,
    }
    fake_scraper.instances[0].done.emit(payload, "extractor retry limit exceeded")

    assert len(received) == 1
    snap = received[0]
    assert snap.status == SnapshotStatus.ERROR
    assert snap.error == "extractor retry limit exceeded"
    assert snap.raw == payload, "the page text must survive for diagnostics"


def test_error_without_a_payload_still_yields_an_empty_dict(fake_scraper):
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="claude",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        build_max_attempts=1,
    )
    rn.run(received.append)
    fake_scraper.instances[0].done.emit(None, "timeout")

    assert received[0].raw == {}


def test_a_runner_with_a_live_scraper_reports_itself_busy(fake_scraper):
    """`busy()` is what stops a second `QWebEngineView` being opened on the
    one cached `QWebEngineProfile` for an account. It must be true for exactly
    as long as a scraper is loading a page, and false again the moment the
    scraper answers - including across a build retry."""
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="x",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        build_max_attempts=2,
    )
    assert rn.busy() is False, "busy before anything ran"

    rn.run(received.append)
    assert rn.busy() is True, "a live scrape is not reported"

    fake_scraper.instances[-1].done.emit({"payload": 1}, "")
    assert received and rn.busy() is False


def test_a_build_retry_keeps_the_runner_busy(fake_scraper):
    received: list[UsageSnapshot] = []
    attempts: list[int] = []

    def _build(payload: Any) -> UsageSnapshot:
        attempts.append(1)
        return _ok_snapshot() if len(attempts) > 1 else _err_snapshot()

    rn = ScrapeRunner(
        account_id="x",
        url="http://example",
        extractor_js="",
        build=_build,
        log=logging.getLogger("test"),
        build_max_attempts=2,
    )
    rn.run(received.append)
    fake_scraper.instances[-1].done.emit({"payload": 1}, "")

    assert not received, "the retry never started"
    assert rn.busy() is True, "the second attempt is still a live scrape"

    fake_scraper.instances[-1].done.emit({"payload": 2}, "")
    assert received and rn.busy() is False


def test_a_page_cannot_name_its_own_error_class(fake_scraper):
    """`error_class` is a scheduler input, and on the "extractor retry limit
    exceeded" branch `result` is the extractor's own return dict - a value
    that came out of the provider page. No shipped extractor emits
    `classification`, and the scheduler only tests the value for membership
    in a two-element tuple, so this is not reachable today. The allowlist is
    what keeps it unreachable."""
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="x",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
    )
    rn.run(received.append)

    fake_scraper.instances[-1].done.emit(
        {"classification": "throttled", "body_text": "..."},
        "extractor retry limit exceeded",
    )

    assert received[0].error_class is None, "a page set a scheduler input"


def test_a_scraper_may_still_name_a_resume_artifact(fake_scraper):
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="x",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
    )
    rn.run(received.append)

    fake_scraper.instances[-1].done.emit(
        {"classification": "resume_artifact"}, "timeout"
    )

    assert received[0].error_class == "resume_artifact"


def test_a_rebuilt_runner_still_knows_the_account_is_scraping(fake_scraper):
    """The guard is a property of the *account*, not of the object holding it.

    `App._build_providers()` runs on every settings save - a colour-only one
    included - and replaces the provider object, whose fresh `ScrapeRunner`
    is `None`. The App's park expires at twice the watchdog budget, so past
    that ceiling nothing refused and the account's single cached
    `QWebEngineProfile` got a second `QWebEngineView` on it: measured at nine
    concurrent views on one profile over six hours of settings saves, against
    one and eleven refusals without them.
    """
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="acct",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        build_max_attempts=1,
    )
    assert runner_module.account_is_busy("acct") is False
    rn.run(received.append)
    assert runner_module.account_is_busy("acct") is True

    rebuilt = ScrapeRunner(
        account_id="acct",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        build_max_attempts=1,
    )
    assert rebuilt.busy() is True, "a rebuilt provider forgot the live scrape"

    fake_scraper.instances[-1].done.emit({"payload": 1}, "")
    assert received
    assert runner_module.account_is_busy("acct") is False
    assert rebuilt.busy() is False


def test_one_accounts_scrape_does_not_make_another_busy(fake_scraper):
    received: list[UsageSnapshot] = []
    rn = ScrapeRunner(
        account_id="claude-aaaa1111",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        build_max_attempts=1,
    )
    rn.run(received.append)

    assert runner_module.account_is_busy("claude-aaaa1111") is True
    assert runner_module.account_is_busy("claude-bbbb2222") is False


def test_a_scrape_whose_done_is_lost_does_not_park_the_account_forever(
    fake_scraper, monkeypatch, caplog
):
    """The guard rests on an invariant in another module: `HeadlessScraper`
    arms its own timeout, so `done` is emitted whatever the page does. A
    `_finish` that raises before its emit - a page whose C++ half Qt has
    already deleted, which is what purging a profile under a live page
    produces - breaks it, and module state does not heal at a settings save
    the way the old per-instance flag did. So the entry expires."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        runner_module, "time", SimpleNamespace(monotonic=lambda: clock["t"])
    )
    rn = ScrapeRunner(
        account_id="claude-ab12cd34",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        timeout_ms=40000,
        transport_max_attempts=2,
        build_max_attempts=2,
    )
    rn.run([].append)
    assert runner_module.account_is_busy("claude-ab12cd34") is True

    # `done` never arrives. Inside the budget the account is still refused:
    # the scrape may genuinely still be loading a page on that profile.
    budget = 40.0 * 2 * 2
    clock["t"] += budget
    assert runner_module.account_is_busy("claude-ab12cd34") is True, (
        "a live scrape inside its own budget was called dead"
    )

    clock["t"] += runner_module._ACTIVE_GUARD_SLACK_SECONDS + 1  # noqa: SLF001
    with caplog.at_level(logging.WARNING, logger="aigauge"):
        caplog.clear()
        assert runner_module.account_is_busy("claude-ab12cd34") is False

    assert "live scrape guard expired account=claude-ab12cd34" in caplog.text
    assert "age_s=221" in caplog.text
    # Logged once: the entry is gone, so the next ask is silent.
    caplog.clear()
    assert runner_module.account_is_busy("claude-ab12cd34") is False
    assert caplog.text == ""


def test_the_guard_expires_on_the_budget_the_scrape_really_enforces():
    """Derived from the runner's own timeout and attempts - the same figure
    the browser providers publish and the App arms its watchdog from - so the
    expiry cannot drift away from the bound the scrape enforces."""
    from aigauge.providers.claude import ClaudeProvider

    rn = ScrapeRunner(
        account_id="claude",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        timeout_ms=40000,
        transport_max_attempts=2,
        build_max_attempts=2,
    )
    assert rn._scrape_budget_seconds() == ClaudeProvider.refresh_budget_seconds  # noqa: SLF001

    unusable = ScrapeRunner(
        account_id="claude",
        url="http://example",
        extractor_js="",
        build=lambda payload: _ok_snapshot(),
        log=logging.getLogger("test"),
        timeout_ms="not a number",  # type: ignore[arg-type]
    )
    assert unusable._scrape_budget_seconds() == 240.0, (  # noqa: SLF001
        "an unusable timeout must not make the guard expire immediately"
    )
