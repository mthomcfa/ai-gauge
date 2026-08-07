from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from PyQt6.QtCore import QObject

from ..models import SnapshotStatus, UsageMetric, UsageSnapshot
from ._common import (
    has_usage_page_signal,
    idle_session_weekly_metrics,
    is_security_verification_page,
    normalize_percent,
    page_text,
)
from ._scrape_runner import ScrapeRunner
from .codex import _parse_reset_text  # reuse the same heuristic parser
from .base import Provider
from .diagnostics import log_page_diagnosis
from .idle import idle_reset_state

CLAUDE_USAGE_URL = "https://claude.ai/new#settings/usage"
_EXPECTED_ROWS = ("session", "weekly_all")
log = logging.getLogger("aigauge.providers.claude")

# Claude's usage dialog renders rows like:
#   "Current session  Resets in 2 hr 59 min  [bar]  64% used"
#   "All models       Resets in 6 hr 29 min  [bar]  30% used"
# We locate each row by its label text, then read the % and reset string.
#
# If Claude lands in the signed-in app shell before the usage dialog has
# hydrated, the extractor asks the scraper to poll again in-page rather than
# failing on sidebar/chat text. It waits for the Session/Weekly rows, not just
# any percent text elsewhere in the shell.
EXTRACTOR_JS = r"""
(() => {
  // Every meter either layout is known to render. This list is not
  // decoration: findRowByLabel penalises a container that holds a rival
  // label, and readRow refuses a container that holds a rival label *and*
  // more than one percentage. A meter missing from here is a meter whose
  // number can be silently reported as another meter's.
  const ROW_LABELS = [
    'Current session',
    'All models',
    'Weekly',
    'Opus only',
    'Sonnet only',
    'Cowork only',
    'Claude Design',
    'Daily included routine runs'
  ];

  function norm(el) {
    return ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  }

  function findRowByLabel(label) {
    const candidates = Array.from(document.querySelectorAll('div, section, li'));
    let best = null;
    let bestScore = Infinity;
    for (const el of candidates) {
      const t = norm(el);
      if (!t.toLowerCase().includes(label.toLowerCase())) continue;
      if (!/%/.test(t)) continue;
      let score = t.length;
      for (const other of ROW_LABELS) {
        if (other !== label && t.toLowerCase().includes(other.toLowerCase())) {
          score += 10000;
        }
      }
      // Prefer actual row-ish containers over large sections or page wrappers.
      const rect = el.getBoundingClientRect();
      if (rect.height > 140) score += 5000;
      if (score < bestScore) {
        best = el;
        bestScore = score;
      }
    }
    return best;
  }

  function readRow(label) {
    const row = findRowByLabel(label);
    if (!row) return null;
    const text = norm(row);
    const lower = text.toLowerCase();
    const pctMatches = Array.from(text.matchAll(/(\d+(?:\.\d+)?)\s*%/g));

    // ATTRIBUTION. This reader takes the LAST percentage in whichever element
    // it picked. When the DOM offers no element isolating one meter, that is
    // a different meter's number: a container reading "Weekly 12% used Opus
    // only 91% used Sonnet only 44% used" reported 44 as the weekly figure.
    // A rival label plus a rival number means the percentage cannot be
    // attributed, so hand back no number at all. The full row text still
    // travels in `raw`, which is what makes the layout fixable in one round.
    const rivalLabel = ROW_LABELS.some(other =>
      other.toLowerCase() !== label.toLowerCase() &&
      lower.includes(other.toLowerCase()));
    const ambiguous = pctMatches.length > 1 && rivalLabel;
    const pctMatch = ambiguous ? null : pctMatches[pctMatches.length - 1];

    // POLARITY. normalize_percent treats an unknown kind as *used*, so a row
    // meaning "42% left" was reported as 42% consumed - a plausible number
    // pointing the wrong way, which is the worst output a quota monitor can
    // produce. The wording must sit against the percentage: scanning the
    // whole row would read the "left" in "2 hr left" as a quota direction and
    // invert the gauge. No wording adjacent to the number means no polarity,
    // and no polarity means no metric (see _build_snapshot).
    let kind = 'unknown';
    if (pctMatch) {
      const start = pctMatch.index;
      const end = start + pctMatch[0].length;
      // Scan forward from the percentage, but stop at a digit or a time unit.
      // That reaches the wording through prose - "64% of your session limit
      // used" is a real phrasing and an earlier, stricter rule rejected it -
      // while still refusing to cross into a countdown: in "42% Resets in 3
      // days left" the scan halts at "3", so the clock's "left" can never be
      // read as a quota direction and invert the gauge.
      const tail = text.slice(end, end + 60);
      const stop = tail.search(/\d|\b(?:sec|second|min|minute|hr|hour|day|week|month)s?\b/i);
      const forward = stop === -1 ? tail : tail.slice(0, stop);
      // Leading wording takes consumption words only: a countdown reads
      // "2 hr left" and "2 hr 30 min remaining", so accepting those before a
      // number would invert the gauge. "used 42%" has no such twin.
      const word = /\b(remaining|left|used|consumed)\b/i.exec(forward)
        || /\b(used|consumed)\W*$/i.exec(text.slice(Math.max(0, start - 16), start));
      if (word) {
        kind = /^(remaining|left)$/i.test(word[1]) ? 'remaining' : 'used';
      }
    }

    const resetMatch = text.match(/Resets?\s+(?:in\s+)?(.+?)(?=\s*$|\s+(?:Daily|Weekly|All|Current|Claude|You)\b|\s*\d+%)/i);
    return {
      raw: text.slice(0, 400),
      percent: pctMatch ? parseFloat(pctMatch[1]) : null,
      kind: kind,
      ambiguous: ambiguous,
      reset_text: resetMatch ? resetMatch[1].trim() : null,
    };
  }

  // innerText, not textContent: textContent concatenates the source of any
  // <style> element in the body, and Claude inlines them. That flooded
  // bodyText with CSS - and since CSS is full of "width:100%", the two idle
  // checks that require the ABSENCE of a percent sign (idleUsagePanel below,
  // and _looks_like_empty_signed_in_usage in Python) could never fire.
  // innerText is rendering-aware and omits style/script content. Codex and
  // webview/verify.py already read text this way; Claude did not.
  const bodyText = ((document.body && (document.body.innerText || document.body.textContent)) || '')
    .replace(/\s+/g, ' ').trim();
  const isLoggedOut =
    !!document.querySelector('a[href*="/login"]') &&
    !/Plan usage/i.test(bodyText);

  const session = readRow('Current session');
  // Claude ships two usage layouts behind a flag. The older one labels the
  // seven-day meter "All models"; the newer gauge/bar one labels it "Weekly"
  // (see its es[] meter table: five_hour -> "Current session", seven_day ->
  // "Weekly", plus Opus only / Sonnet only / Cowork only / Claude Design).
  // Requiring "All models" made the newer layout permanently unreadable.
  const weeklyAll = readRow('All models') || readRow('Weekly');

  function onUsageRoute() {
    return /\/settings\/usage/.test(location.pathname) ||
      /settings\/usage/i.test(location.hash);
  }

  function ensureUsageRoute() {
    if (onUsageRoute()) return null;
    if (location.hostname !== 'claude.ai') return null;
    if (/Plan usage|Current session|All models/i.test(bodyText)) return null;
    location.href = '/new#settings/usage';
    return 'opened usage dialog';
  }

  const routeReason = !isLoggedOut ? ensureUsageRoute() : null;
  if (routeReason) {
    return {
      __retry_after_ms: 1200,
      __retry_reason: routeReason,
      logged_out: false,
      session: null,
      weekly_all: null,
      url: location.href,
      title: document.title,
      body_text: bodyText.slice(0, 8000),
    };
  }

  // The current Claude UI opens usage as a shell/dialog route. Percent text
  // elsewhere in the shell is not enough; wait for the Session/Weekly rows
  // or for the explicit idle-zero usage panel before handing data to Python.
  const usagePanelSignals = /Plan usage|Current session|All models/i.test(bodyText);
  const idleUsagePanel = /Plan usage/i.test(bodyText) &&
    /Current session/i.test(bodyText) &&
    /All models|Weekly/i.test(bodyText) &&
    !/%/.test(bodyText);
  const requiredRowsReady = !!session && !!weeklyAll;
  if (!isLoggedOut && (onUsageRoute() || usagePanelSignals) && !requiredRowsReady && !idleUsagePanel) {
    return {
      __retry_after_ms: 1200,
      __retry_reason: 'usage dialog not ready',
      logged_out: false,
      session: null,
      weekly_all: null,
      url: location.href,
      title: document.title,
      body_text: bodyText.slice(0, 8000),
    };
  }

  return {
    logged_out: isLoggedOut,
    session: session,
    weekly_all: weeklyAll,
    url: location.href,
    title: document.title,
    body_text: bodyText.slice(0, 8000),
  };
})();
"""


def _is_claude_usage_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != "claude.ai":
        return False
    return parsed.path == "/settings/usage" or "settings/usage" in parsed.fragment


def _unreadable_reason(card: dict[str, Any]) -> str | None:
    """Why this row cannot be turned into a gauge, or None if it can.

    Both cases produce a plausible wrong number rather than an error if left
    unchecked, which for a quota monitor is the worst available outcome:

    * ``ambiguous`` - the extractor found the label and several percentages in
      one container and could not say which belonged to this meter.
    * unknown ``kind`` - no "used"/"remaining" wording sat against the
      percentage, and ``normalize_percent`` resolves an unknown kind to
      *used*. A row meaning "42% left" would be shown as 42% consumed.

    A row carrying no percentage at all is not unreadable; it is simply
    absent, and the idle/empty-panel paths below handle that.
    """
    if card.get("ambiguous"):
        return "several meters share one container"
    if card.get("percent") is None:
        return None
    if card.get("kind") not in ("used", "remaining"):
        return "no used/remaining wording beside the percentage"
    return None


def _looks_like_empty_signed_in_usage(payload: dict[str, Any]) -> bool:
    if not _is_claude_usage_url(str(payload.get("url") or "")):
        return False
    title = str(payload.get("title") or "").strip().lower()
    if title != "claude":
        return False
    body = str(payload.get("body_text") or "").lower()
    # Require positive evidence the usage panel actually rendered. Without
    # this, a partially-loaded page (sidebar only, main pane still fetching)
    # gets misclassified as idle and shown as 0/0.
    if "plan usage" not in body:
        return False
    # If percent text is on the page but the row extractor missed it, that's
    # a layout change — not idle.
    if "%" in body:
        return False
    return True


def _is_logged_out_payload(payload: dict[str, Any]) -> bool:
    url = str(payload.get("url") or "").lower()
    return bool(payload.get("logged_out")) or "/logout" in url or "/login" in url


def _is_load_failed_payload(payload: dict[str, Any]) -> bool:
    if has_usage_page_signal(payload):
        return False
    text = page_text(payload)
    return (
        "can't reach claude" in text
        or "check your connection" in text
        or ("try again" in text and "claude" in text)
    )


def _build_snapshot(
    payload: dict[str, Any],
    *,
    account_id: str = "claude",
) -> UsageSnapshot:
    if _is_logged_out_payload(payload):
        log_page_diagnosis(
            log,
            provider=account_id,
            classification="logged_out",
            payload=payload,
            expected_rows=_EXPECTED_ROWS,
        )
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Not signed in to Claude.",
            raw=payload,
        )
    if _is_load_failed_payload(payload):
        log_page_diagnosis(
            log,
            provider=account_id,
            classification="load_failed",
            payload=payload,
            expected_rows=_EXPECTED_ROWS,
            level=logging.WARNING,
        )
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.ERROR,
            error="Claude page load failed. Check your connection and try again.",
            raw=payload,
        )
    if is_security_verification_page(payload):
        log_page_diagnosis(
            log,
            provider=account_id,
            classification="security_verification",
            payload=payload,
            expected_rows=_EXPECTED_ROWS,
        )
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.AUTH_REQUIRED,
            error="Claude security verification required. Click Connect and complete the browser check.",
            raw=payload,
        )

    rows = (
        ("session", "Session", timedelta(hours=5)),
        ("weekly_all", "Weekly", timedelta(days=7)),
    )
    metrics: list[UsageMetric] = []
    unreadable: list[str] = []
    for key, label, reset_window in rows:
        card = payload.get(key)
        if not card:
            continue
        reason = _unreadable_reason(card)
        if reason:
            unreadable.append(f"{label} ({reason})")
            continue
        percent = normalize_percent(card.get("percent"), card.get("kind", ""))
        if percent is None:
            continue
        resets_at = _parse_reset_text(card.get("reset_text"))
        resets_at, reset_label, idle_note = idle_reset_state(
            percent=percent,
            resets_at=resets_at,
            window=reset_window,
        )
        metrics.append(
            UsageMetric(
                label=label,
                percent_used=percent,
                resets_at=resets_at,
                reset_label=reset_label,
                note=idle_note or card.get("reset_text"),
                window=reset_window,
            )
        )

    # A row we could not read is reported, never quietly dropped. Dropping it
    # would leave a tile showing one gauge as though that were the whole
    # picture, or - worse, before this guard - a number belonging to a
    # different meter. The payload rides along, so one error report contains
    # the row text needed to teach the extractor the new wording.
    if unreadable:
        log_page_diagnosis(
            log,
            provider=account_id,
            classification="unreadable_usage_row",
            payload=payload,
            expected_rows=_EXPECTED_ROWS,
            level=logging.WARNING,
        )
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.ERROR,
            error=(
                "Claude's usage layout changed: could not read "
                + ", ".join(unreadable)
                + ". Use Copy diagnostics to report it."
            ),
            raw=payload,
        )

    if not metrics:
        if _looks_like_empty_signed_in_usage(payload):
            log_page_diagnosis(
                log,
                provider=account_id,
                classification="empty_signed_in_usage",
                payload=payload,
                expected_rows=_EXPECTED_ROWS,
            )
            metrics = idle_session_weekly_metrics()
        else:
            log_page_diagnosis(
                log,
                provider=account_id,
                classification="layout_changed",
                payload=payload,
                expected_rows=_EXPECTED_ROWS,
                level=logging.WARNING,
            )
            return UsageSnapshot(
                provider=account_id,
                status=SnapshotStatus.ERROR,
                error="Could not read usage from page (layout may have changed).",
                raw=payload,
            )

    return UsageSnapshot(
        provider=account_id,
        status=SnapshotStatus.OK,
        metrics=metrics,
        raw=payload,
    )


class ClaudeProvider(Provider):
    name = "claude"
    display_name = "Claude"

    def __init__(
        self,
        parent: QObject | None = None,
        account_id: str = "claude",
    ):
        self._parent = parent
        self._account_id = account_id
        self._runner: ScrapeRunner | None = None  # held to prevent GC

    def refresh(self, on_done: Callable[[UsageSnapshot], None]) -> None:
        def _build(payload: dict[str, Any]) -> UsageSnapshot:
            return _build_snapshot(payload, account_id=self._account_id)

        self._runner = ScrapeRunner(
            account_id=self._account_id,
            url=CLAUDE_USAGE_URL,
            extractor_js=EXTRACTOR_JS,
            build=_build,
            log=log,
            wait_ms=7000,
            transport_max_attempts=2,
            build_max_attempts=2,
            parent=self._parent,
        )
        self._runner.run(on_done)
