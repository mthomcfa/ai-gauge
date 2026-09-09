from __future__ import annotations

import logging
from typing import Any, Callable
from urllib.parse import urlparse

from PyQt6.QtCore import QObject

from ..config import Config
from ..models import SnapshotStatus, UsageMetric, UsageSnapshot
from ._common import (
    has_usage_page_signal,
    idle_session_weekly_metrics,
    is_security_verification_page,
    page_text,
)
from ._scrape_runner import ScrapeRunner
from .catalog import (
    MeterCatalog,
    adopt_rows,
    bundled_catalog,
    extractor_source,
    load_catalog,
    metric_for_spec,
    record_no_container_scan,
    record_scan,
    scan_due,
    uncapped_catalog,
    unreadable_reason,
)
from .codex import _parse_reset_text  # reuse the same heuristic parser
from .base import Provider
from .diagnostics import log_page_diagnosis

CLAUDE_USAGE_URL = "https://claude.ai/settings/usage"
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
EXTRACTOR_TEMPLATE = r"""
(() => {
  // Every meter either layout is known to render, injected from the meter
  // catalog (providers/meter_catalog/claude.json plus the app-data override):
  // bundled, discovered and hand-added alike, because the page renders them
  // all. This list is not decoration: findRowByLabel penalises a container
  // that holds a rival label, and readRow refuses a container that holds a
  // rival label *and* more than one percentage. A meter missing from here is a
  // meter whose number can be silently reported as another meter's.
  const ROW_LABELS = __AG_ROW_LABELS__;
  // [{key, label, aliases, boundaries, primary}] - one entry per meter the
  // catalog defines. Each becomes its own field on the snapshot.
  const CATALOG = __AG_CATALOG__;
  // Weekly self-scan: also hand back every candidate row the usage container
  // renders, so Python can adopt meters this build has never heard of.
  const DISCOVER = __AG_DISCOVER__;

  function norm(el) {
    return ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  }

  // One DOM walk per extractor run, not one per label. The catalog turned a
  // two-label read into a dozen, and querySelectorAll + innerText on every
  // element is the expensive part of this extractor.
  let rowCandidateCache = null;
  function rowCandidates() {
    if (rowCandidateCache) return rowCandidateCache;
    rowCandidateCache = Array.from(document.querySelectorAll('div, section, li'))
      .map(el => {
        const text = norm(el);
        return { el: el, text: text, lower: text.toLowerCase() };
      });
    return rowCandidateCache;
  }

  function findRowByLabel(label) {
    const lowerLabel = label.toLowerCase();
    let best = null;
    let bestScore = Infinity;
    for (const candidate of rowCandidates()) {
      const t = candidate.text;
      if (!candidate.lower.includes(lowerLabel)) continue;
      if (!/%/.test(t)) continue;
      let score = t.length;
      for (const other of ROW_LABELS) {
        if (other !== label && candidate.lower.includes(other.toLowerCase())) {
          score += 10000;
        }
      }
      // Prefer actual row-ish containers over large sections or page wrappers.
      const rect = candidate.el.getBoundingClientRect();
      if (rect.height > 140) score += 5000;
      if (score < bestScore) {
        best = candidate.el;
        bestScore = score;
      }
    }
    return best;
  }

  function readRow(label) {
    const row = findRowByLabel(label);
    if (!row) return null;
    return readRowText(norm(row), label);
  }

  // Split out of readRow so the discovery scan below reads an unrecognized row
  // through exactly the same attribution and polarity rules.
  function readRowText(text, label) {
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
      // Scan forward from the percentage, but stop at anything that starts
      // reset/countdown text: a digit, a time unit, or the word reset/renew
      // itself. That reaches the wording through prose - "64% of your session
      // limit used" is a real phrasing and an earlier, stricter rule rejected
      // it - while refusing to cross into a countdown, where "left" means a
      // clock rather than a quota and would invert the gauge.
      //
      // The reset/renew tokens are not redundant with the digit and time-unit
      // ones: "Resets tomorrow, some left" contains neither, so without them
      // the scan walked into that "left" and reported a bare percentage as
      // 58% used.
      const tail = text.slice(end, end + 60);
      const stop = tail.search(
        /\d|\b(?:reset|renew|sec|second|min|minute|hr|hour|day|week|month)s?\b/i);
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

  // One field per catalog meter. `seed` carries the two primary rows already
  // read above; the rest are resolved by trying each catalog alias in order,
  // which is also how a primary meter survives being relabelled - adding the
  // new wording to the override file is enough.
  function readCatalogRows(seed) {
    // Object.create(null), not {}: a plain object inherits constructor,
    // toString and hasOwnProperty, all truthy, so a catalog key of
    // "constructor" read as already-filled and its meter was never read.
    const out = Object.create(null);
    for (const key of Object.keys(seed || {})) {
      if (seed[key]) out[key] = seed[key];
    }
    for (const entry of CATALOG) {
      if (out[entry.key]) continue;
      for (const alias of entry.aliases) {
        const row = readRow(alias);
        if (row) { out[entry.key] = row; break; }
      }
    }
    return out;
  }

  function pctCount(text) {
    return (text.match(/\d+(?:\.\d+)?\s*%/g) || []).length;
  }

  // Wording that names the usage panel. "Plan usage" alone was both too narrow
  // and too broad: the settings nav carries a "Plan usage" item, while a panel
  // that renames its heading stops being findable at all. This is the same
  // alternation the sibling checks below already use, plus every primary alias
  // the catalog carries - so teaching the app a relabelled meter also teaches
  // it where the panel is.
  const PANEL_MARKER = /plan usage|current session|all models|weekly/i;
  function marksUsagePanel(text) {
    if (PANEL_MARKER.test(text)) return true;
    const lower = text.toLowerCase();
    for (const entry of CATALOG) {
      if (!entry.primary) continue;
      for (const alias of entry.aliases) {
        if (alias && lower.includes(alias.toLowerCase())) return true;
      }
    }
    return false;
  }

  // The rows themselves, as opposed to the wrappers around them: an element
  // carrying one percentage and no percentage-bearing descendant of its own.
  // Every wrapper between a row and the panel reports that row's percentage
  // too, so counting all of them multiplied the rows total by the nesting
  // depth - and six layers of wrapper was enough to let an element holding
  // the whole page pass the ratio below.
  let leafRowCache = null;
  function leafRows() {
    if (leafRowCache) return leafRowCache;
    const bearing = rowCandidates().filter(c => pctCount(c.text) >= 1);
    leafRowCache = bearing.filter(
      c => pctCount(c.text) === 1 &&
        !bearing.some(other => other !== c && c.el.contains(other.el)));
    return leafRowCache;
  }

  // A container that reached past the panel is worse than no container at all:
  // `in_container` is the whole difference between a meter and page furniture,
  // and every percentage on the page sits inside one of these. A panel is
  // mostly its rows; an element several times longer than the rows it holds
  // swallowed the page around them.
  function swallowedThePage(el, text) {
    let rowsLen = 0;
    for (const row of leafRows()) {
      if (row.el === el || !el.contains(row.el)) continue;
      rowsLen += row.text.length;
    }
    return rowsLen > 0 && text.length > rowsLen * 4;
  }

  // How much of a usage panel a candidate holds: the catalog meters whose
  // wording it renders, plus the rows whose percentage carries used/remaining
  // wording. Size was the whole tiebreak, and page furniture is always
  // smaller than the panel - a nav reading "Plan usage 12% off Max" beside
  // "Storage 88% used" won on length, "Storage" was adopted, and the real
  // panel was never scanned. A sidebar of chat titles ("Current session
  // speedup 40% faster") does it with the marker wording alone. Richness
  // answers what a panel IS rather than how long it is; length only settles
  // ties.
  function richness(el, text) {
    const lower = text.toLowerCase();
    let score = 0;
    for (const entry of CATALOG) {
      // Once per meter, not once per alias: one meter's aliases overlap
      // ("All models" and "Weekly"), and the same meter twice is not two.
      for (const alias of entry.aliases) {
        if (alias && lower.includes(alias.toLowerCase())) { score++; break; }
      }
    }
    for (const row of leafRows()) {
      if (row.el !== el && !el.contains(row.el)) continue;
      // The discovery reader's own polarity verdict, so what is counted here
      // is what could be adopted. The label it takes does not matter: a leaf
      // row carries exactly one percentage, so nothing is ambiguous.
      if (readRowText(row.text, '').kind !== 'unknown') score++;
    }
    return score;
  }

  // The element holding the usage panel, found by walking UP from the wording
  // that names it. Discovery is confined to it: a percentage in the sidebar or
  // a chat title is not a meter, and the only way to tell is where it sits.
  //
  // Taking the smallest element holding the marker and two percentages picked
  // <body> whenever the marker also appeared outside the panel, which is the
  // ordinary case - and inside <body>, "Storage 88%" and "Save 20%" are meters.
  // Anchoring on the innermost elements that carry the marker and climbing to
  // the first ancestor holding two percentages keeps the answer inside the
  // panel. <body> is refused outright.
  function usageContainer() {
    const marked = rowCandidates().filter(c => marksUsagePanel(c.text));
    // Innermost first, then: an anchor has to carry a percentage of its own.
    // A bare mention of the marker is a nav item, a heading or prose about
    // limits, not a panel, and climbing from one let a settings nav holding
    // "Plan usage" beside "Storage 88%" become the container - a SMALLER
    // element than the panel, so it won, and the real panel was never
    // scanned.
    const anchors = marked.filter(
      c => !marked.some(other => other !== c && c.el.contains(other.el)) &&
        pctCount(c.text) >= 1);
    // Those bare markers again, this time as evidence of overreach: one of
    // them inside a candidate container but not on the path up from the
    // anchor means the climb left the panel and took a slice of the page
    // with it. That is how a panel rendering fewer than two percentages
    // reached the SPA's root wrapper, where every piece of page furniture
    // counts as `in_container`.
    //
    // Only once the climb has passed the panel, though: the panel's own
    // heading, a tab strip ("Daily | Weekly | Monthly") or a footnote about
    // limits is a bare marker sitting INSIDE the panel and on no path up from
    // a row, so counting it refused the real panel outright - leaving
    // discovery permanently inert on that layout and saying so once a day as
    // `discovery_no_container`.
    const bare = marked.filter(c => pctCount(c.text) === 0);
    let best = null;
    let bestLen = Infinity;
    let bestScore = -1;
    for (const anchor of anchors) {
      let el = anchor.el;
      // Panel-shaped: an ancestor above the anchor holding a percentage and
      // no stray marker. A stray above THAT one is the climb leaving the
      // panel; a stray at or below it is the panel's own furniture.
      let passedPanel = false;
      for (let depth = 0; depth < 12 && el; depth++) {
        if (el === document.body || el === document.documentElement) break;
        const text = norm(el);
        const holdsStray = bare.some(
          c => el.contains(c.el) && !c.el.contains(anchor.el));
        if (pctCount(text) >= 2) {
          const stray = passedPanel && holdsStray;
          if (!stray && !swallowedThePage(el, text)) {
            const score = richness(el, text);
            if (score > bestScore ||
                (score === bestScore && text.length < bestLen)) {
              best = el;
              bestLen = text.length;
              bestScore = score;
            }
          }
          break;
        }
        if (depth > 0 && !holdsStray && pctCount(text) >= 1) passedPanel = true;
        el = el.parentElement;
      }
    }
    return best;
  }

  // Every row inside the usage container that looks like "<label> ... N% ...".
  // Deliberately permissive about wording and strict about shape; Python
  // applies the label rules before anything is adopted.
  function discoverRows() {
    const container = usageContainer();
    // null, not []: "the panel showed nothing new" is a completed scan and
    // "there was no panel to look in" is not one. Python cannot tell those
    // apart from an empty list, and it stamps the weekly scan on the answer.
    if (!container || !container.contains) return null;
    const out = [];
    // Object.create(null) for the same reason as readCatalogRows's map: with a
    // plain object, seen["constructor"] is truthy before anything is seen, so
    // a row labelled "Constructor" was dropped from every scan.
    const seen = Object.create(null);
    for (const candidate of rowCandidates()) {
      if (out.length >= 40) break;
      const text = candidate.text;
      if (!text || text.length > 200) continue;
      if (candidate.el !== container && !container.contains(candidate.el)) continue;
      const percentages = text.match(/\d+(?:\.\d+)?\s*%/g) || [];
      // Exactly one percentage: two means this element wraps several meters,
      // and its own child will be picked up on its own.
      if (percentages.length !== 1) continue;
      // Stop at the percentage or at reset wording, whichever comes first.
      // Stopping at any digit would empty the label of a card whose name
      // starts with one ("5 hour usage limit").
      const labelMatch = /^(.*?)(?=\s*(?:Resets?\b|\d+(?:\.\d+)?\s*%))/i.exec(text);
      if (!labelMatch) continue;
      const label = labelMatch[1].trim();
      if (!label || label.length > 60) continue;
      const key = label.toLowerCase();
      if (seen[key]) continue;
      seen[key] = true;
      const row = readRowText(text, label);
      if (!row || row.percent === null) continue;
      row.label = label;
      row.in_container = true;
      out.push(row);
    }
    // Nested wrappers: a parent that puts a heading in front of one row
    // renders that row's number under a longer label, and both are
    // candidates. Same percentage, same reset text, and one label ending in
    // the other means one row described twice - keep the child.
    const deduped = [];
    for (const row of out) {
      const lower = row.label.toLowerCase();
      let merged = false;
      for (let i = 0; i < deduped.length; i++) {
        const other = deduped[i];
        const otherLower = other.label.toLowerCase();
        if (other.percent !== row.percent) continue;
        if ((other.reset_text || '') !== (row.reset_text || '')) continue;
        if (!lower.endsWith(otherLower) && !otherLower.endsWith(lower)) continue;
        if (row.label.length < other.label.length) deduped[i] = row;
        merged = true;
        break;
      }
      if (!merged) deduped.push(row);
    }
    return deduped;
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

  // The two rows above stay the primary path, unchanged. Everything else the
  // catalog knows about is read alongside them, and each becomes its own field.
  const rows = readCatalogRows({ session: session, weekly_all: weeklyAll });

  // Rendered evidence that the usage surface is actually on screen. Declared
  // here, above ensureUsageRoute, because that function's decision must be
  // based on what the page SHOWS rather than on what the URL says.
  const usagePanelSignals = /Plan usage|Current session|All models/i.test(bodyText);

  function onUsageRoute() {
    return /\/settings\/usage/.test(location.pathname) ||
      /settings\/usage/i.test(location.hash);
  }

  // One candidate, deliberately. The hash form was kept as a fallback and
  // turned out to be actively harmful: on a settings page that is merely slow
  // (body_text "Loading..." while eight other endpoints resolve first), no
  // usage panel has rendered yet, so the fallback fired and navigated away
  // from the correct route to one we have direct evidence does not open the
  // dialog at all - discarding the load that was about to succeed, and with
  // it the recorded API capture. Being on the right route and unhydrated is a
  // reason to wait, not to re-route.
  const ROUTE_CANDIDATES = ['/settings/usage'];

  function routeTries() {
    try { return parseInt(sessionStorage.getItem('__ag_route_tries') || '0', 10) || 0; }
    catch (e) { return 0; }
  }

  function ensureUsageRoute() {
    if (location.hostname !== 'claude.ai') return null;
    // Gate on rendered evidence, NOT on onUsageRoute(). The app navigates to a
    // usage URL itself, so a URL-shape test is true from the very first poll
    // and this recovery path can never run - which is precisely how a moved
    // usage surface turned into an endless "usage dialog not ready" retry
    // while the page sat on the signed-in home screen.
    if (usagePanelSignals) return null;
    const here = location.pathname + location.hash;
    let tries = routeTries();
    // Never re-navigate to where we already are; that is a no-op that would
    // burn an attempt and, on a route that bounces, spin.
    while (tries < ROUTE_CANDIDATES.length && ROUTE_CANDIDATES[tries] === here) {
      tries += 1;
    }
    if (tries >= ROUTE_CANDIDATES.length) return null;
    try { sessionStorage.setItem('__ag_route_tries', String(tries + 1)); } catch (e) {}
    location.href = ROUTE_CANDIDATES[tries];
    return 'opening usage page ' + ROUTE_CANDIDATES[tries];
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
      api: (window.__ag_api || null),
    };
  }

  // Percent text elsewhere in the shell is not enough; wait for the
  // Session/Weekly rows or for the explicit idle-zero usage panel before
  // handing data to Python. usagePanelSignals is declared above, next to
  // ensureUsageRoute, which needs the same evidence.
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
      api: (window.__ag_api || null),
    };
  }

  return {
    logged_out: isLoggedOut,
    session: session,
    weekly_all: weeklyAll,
    rows: rows,
    discovered: DISCOVER ? discoverRows() : null,
    url: location.href,
    title: document.title,
    body_text: bodyText.slice(0, 8000),
    api: (window.__ag_api || null),
  };
})();
"""

# The module-level constant is the catalog as shipped, with discovery off. The
# provider rebuilds it per refresh so an override file (and a due scan) take
# effect without a restart.
EXTRACTOR_JS = extractor_source(
    EXTRACTOR_TEMPLATE, bundled_catalog("claude"), discover=False
)


def _is_claude_usage_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != "claude.ai":
        return False
    return parsed.path == "/settings/usage" or "settings/usage" in parsed.fragment


def _payload_rows(payload: dict[str, Any]) -> dict[str, Any]:
    """Per-meter rows from the payload, keyed by catalog key.

    ``rows`` is what the catalog-aware extractor returns. The two top-level
    keys are the primary path and also the shape of every payload recorded
    before the catalog existed (cached snapshots, hand-built test payloads),
    so they are still honoured.
    """
    rows = payload.get("rows")
    merged: dict[str, Any] = dict(rows) if isinstance(rows, dict) else {}
    for key in _EXPECTED_ROWS:
        card = payload.get(key)
        if isinstance(card, dict) and not merged.get(key):
            merged[key] = card
    return {key: card for key, card in merged.items() if isinstance(card, dict)}


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
    catalog: MeterCatalog | None = None,
    log_skipped: bool = True,
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

    rows = _payload_rows(payload)
    catalog = catalog or load_catalog("claude")
    metrics: list[UsageMetric] = []
    unreadable: list[str] = []
    skipped: list[str] = []
    for spec in catalog.enabled_specs:
        card = rows.get(spec.key)
        if not card:
            continue
        reason = unreadable_reason(card, spec)
        if reason:
            # Only a primary meter's unreadability is worth failing the whole
            # snapshot for. A breakdown row the page renders oddly must not
            # take Session and Weekly down with it.
            if spec.primary:
                unreadable.append(f"{spec.label} ({reason})")
            else:
                skipped.append(f"{spec.label} ({reason})")
            continue
        metric = metric_for_spec(
            spec, card, resets_at=_parse_reset_text(card.get("reset_text"))
        )
        if metric is not None:
            metrics.append(metric)

    # An informational row is dropped rather than failing the snapshot, but a
    # silent drop is invisible: the tile just shows one gauge fewer than the
    # page does, and nothing says which meter went or why.
    if skipped and log_skipped:
        log.info(
            "provider skipped unreadable rows provider=%s rows=%s",
            account_id,
            "; ".join(skipped),
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

    # Something untagged has to have been read. A page that yields only
    # informational rows - a relabelled Session, a Weekly row the extractor no
    # longer finds - would otherwise render an OK tile with no gauge on it and
    # no error to explain why. Codex has had this guard since its
    # partial-render bug; Claude was missing it.
    #
    # Unless the catalog has no primary meter enabled: then a tile of
    # informational rows is what the user asked for by switching Session and
    # Weekly off, and "the layout may have changed" is a permanent error about
    # their own setting. Codex reaches the same answer through its
    # expected_primary set, which is empty in that case.
    expects_primary = any(spec.primary for spec in catalog.enabled_specs)
    if expects_primary and metrics and not any(
        metric.tag is None and metric.percent_used is not None for metric in metrics
    ):
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
        config: Config | None = None,
    ):
        self._parent = parent
        self._account_id = account_id
        self._config = config
        self._runner: ScrapeRunner | None = None  # held to prevent GC

    def refresh(self, on_done: Callable[[UsageSnapshot], None]) -> None:
        catalog = load_catalog("claude")
        # The scan is per provider *kind*, not per account: the meters are a
        # property of Claude's page, so the first account to refresh after the
        # interval does the scan and the others read the result.
        discover = scan_due(self._config, "claude")

        # ScrapeRunner rebuilds the snapshot after a transient error, so this
        # closure runs more than once per refresh. A scan is a once-per-refresh
        # event: without the flag the second attempt adopts again and saves the
        # config again.
        scanned = False

        def _build(payload: dict[str, Any]) -> UsageSnapshot:
            nonlocal scanned
            # Classify first. A logged-out, challenged or half-rendered page
            # still carries rows, and adopting from one writes page furniture
            # into the catalog permanently while burning the weekly scan (and
            # the "Re-scan meters now" button) on a page we could not read.
            snapshot = _build_snapshot(
                payload, account_id=self._account_id, catalog=catalog
            )
            if not discover or scanned or snapshot.status != SnapshotStatus.OK:
                return snapshot
            discovered = payload.get("discovered")
            if not isinstance(discovered, list):
                # The usage panel was never located, so the scan did not look.
                # Distinguishable in the log from a scan that found nothing.
                log_page_diagnosis(
                    log,
                    provider=self._account_id,
                    classification="discovery_no_container",
                    payload=payload,
                    expected_rows=_EXPECTED_ROWS,
                )
                if "discovered" in payload:
                    # The extractor ran the scan and answered null: there is
                    # no panel it can find. Re-attempt daily rather than on
                    # every refresh. A payload with no "discovered" key at all
                    # is an extractor that never got that far, and that must
                    # not spend the scan.
                    scanned = True
                    record_no_container_scan(self._config, "claude")
                return snapshot
            scanned = True
            if adopt_rows("claude", discovered, account_id=self._account_id):
                snapshot = _build_snapshot(
                    payload,
                    account_id=self._account_id,
                    catalog=load_catalog("claude"),
                    # The rows this refresh could not read were named in the
                    # log by the build above, off the same payload. Adoption
                    # adds meters, never makes one unreadable, so repeating
                    # the line here only doubled it.
                    log_skipped=False,
                )
            record_scan(self._config, "claude")
            return snapshot

        self._runner = ScrapeRunner(
            account_id=self._account_id,
            url=CLAUDE_USAGE_URL,
            extractor_js=extractor_source(
                EXTRACTOR_TEMPLATE,
                catalog,
                discover=discover,
                # The rival set is the whole file, not the capped read: a
                # meter past the cap is still on the page, and one missing
                # from ROW_LABELS is one whose number can be reported as
                # another meter's.
                rivals=uncapped_catalog("claude"),
            ),
            build=_build,
            log=log,
            # The settings route resolves i18n, org, feature, memory, MCP and
            # marketplace endpoints before usage. Poll sooner but for much
            # longer: the old 7s wait plus five 1.2s reruns gave up at ~13s,
            # while the page was still showing "Loading...".
            wait_ms=3000,
            max_extractor_reruns=20,
            timeout_ms=40000,
            transport_max_attempts=2,
            build_max_attempts=2,
            # Discovery, not yet load-bearing: the gauge still reads the DOM.
            # This records the SHAPE of the JSON the page fetches so a field
            # mapping can be written from a real account without guessing, and
            # without any response body leaving the browser context.
            capture_api=True,
            parent=self._parent,
        )
        self._runner.run(on_done)
