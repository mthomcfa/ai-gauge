from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable

from PyQt6.QtCore import QObject

from ..config import Config
from ..models import SnapshotStatus, UsageMetric, UsageSnapshot
from ._common import (
    has_usage_page_signal,
    is_security_verification_page,
)
from ._scrape_runner import ScrapeRunner
from .base import Provider
from .catalog import (
    MeterCatalog,
    MeterSpec,
    adopt_rows,
    bundled_catalog,
    extractor_source,
    load_catalog,
    metric_for_spec,
    record_no_container_scan,
    record_scan,
    scan_due,
    unreadable_reason,
)
from .diagnostics import log_page_diagnosis

CODEX_ANALYTICS_URL = "https://chatgpt.com/codex/cloud/settings/analytics"
CODEX_USAGE_URL = f"{CODEX_ANALYTICS_URL}#personal-usage"
_EXPECTED_ROWS = ("session", "weekly")
log = logging.getLogger("aigauge.providers.codex")

# Walks the rendered analytics page, finds the available "Balance" cards by
# their headings, and reads the percentage + reset text out of each. The weekly
# card is always required; the five-hour card is optional because Codex may
# temporarily expose only the shared weekly agentic limit. Returns raw text
# fragments so Python can do the unit-aware normalization.
EXTRACTOR_TEMPLATE = r"""
(() => {
  // [{key, label, aliases, boundaries, primary}] from the meter catalog
  // (providers/meter_catalog/codex.json plus the app-data override). Each
  // entry becomes its own field on the snapshot.
  const CATALOG = __AG_CATALOG__;
  // Weekly self-scan: also return every candidate card the usage container
  // renders, so Python can adopt cards this build has never heard of.
  const DISCOVER = __AG_DISCOVER__;

  function visibleText(el) {
    return ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  }

  function windowTextAfterLabel(label, nextLabels) {
    const bodyText = visibleText(document.body);
    const lowerText = bodyText.toLowerCase();
    const start = lowerText.indexOf(label.toLowerCase());
    if (start < 0) return '';
    let end = bodyText.length;
    for (const nextLabel of nextLabels) {
      const candidate = lowerText.indexOf(nextLabel.toLowerCase(), start + label.length);
      if (candidate >= 0 && candidate < end) end = candidate;
    }
    return bodyText.slice(start, end).trim();
  }

  function maybeSelectPersonalUsageTab(bodyText) {
    if (/Weekly usage limit/i.test(bodyText) && /\d+(?:\.\d+)?\s*%/.test(bodyText)) {
      return null;
    }

    // Only interactive elements are tabs. The current analytics page uses
    // "Personal usage" as a heading inside a plain div; clicking that wrapper
    // forever would exhaust the extractor's retry budget.
    const labels = Array.from(document.querySelectorAll('button,a,[role="tab"],[role="button"]'));
    const label = labels.find(el => visibleText(el).toLowerCase() === 'personal usage');
    if (!label) return null;

    // `labels` is already filtered to interactive elements, so the element is
    // its own click target (a .closest() hop here would be a no-op).
    const target = label;
    const selected =
      target.getAttribute('aria-selected') === 'true' ||
      target.getAttribute('data-state') === 'active' ||
      /\bactive\b|\bselected\b/.test(String(target.className || ''));
    if (selected) return 'waiting for personal usage cards';

    target.click();
    return 'selected personal usage tab';
  }

  // One DOM walk per extractor run, not one per label. The catalog turned a
  // two-card read into as many cards as the page renders, and querySelectorAll
  // + innerText over every element is the expensive part of this extractor.
  let cardCandidateCache = null;
  function cardCandidates() {
    if (cardCandidateCache) return cardCandidateCache;
    cardCandidateCache = Array.from(
      document.querySelectorAll('article,section,[role="group"],div,li')
    ).map(el => ({ el, text: visibleText(el) }));
    return cardCandidateCache;
  }

  function findCardByLabel(label) {
    const candidates = cardCandidates()
      .filter(({ text }) => {
        const lower = text.toLowerCase();
        return lower.includes(label.toLowerCase()) && /%/.test(text) && text.length < 1600;
      })
      .sort((a, b) => {
        const score = item =>
          (item.el.tagName.toLowerCase() === 'article' ? -1000 : 0) +
          (/reset/i.test(item.text) ? -100 : 0) +
          item.text.length;
        return score(a) - score(b);
      });
    if (candidates.length) return candidates[0].el;

    const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span,p'));
    const heading = headings.find(el => {
      const t = visibleText(el);
      return t === label || t.toLowerCase() === label.toLowerCase();
    });
    if (!heading) return null;
    let card = heading;
    for (let i = 0; i < 6 && card.parentElement; i++) {
      card = card.parentElement;
      const txt = visibleText(card);
      if (/%/.test(txt)) return card;
    }
    return null;
  }

  // POLARITY. The same rule as Claude's readRow, and for the same reason:
  // normalize_percent resolves an unknown kind to *used*, so a card meaning
  // "42% left" was reported as 42% consumed - a plausible number pointing the
  // wrong way. Testing the whole card for "used"/"remaining" did exactly that
  // with a bare percentage, and the text-window fallback could take the
  // wording out of the NEXT card. The wording has to sit against the
  // percentage, and the forward scan stops at a digit, a time unit or a
  // reset/renew word so a countdown's "left" ("2 hr left") is never read as a
  // quota direction. No wording beside the number means no polarity, and no
  // polarity means no metric (see _build_snapshot).
  function polarityFor(text, pctMatch) {
    if (!pctMatch) return 'unknown';
    const start = pctMatch.index;
    const end = start + pctMatch[0].length;
    const tail = text.slice(end, end + 60);
    const stop = tail.search(
      /\d|\b(?:reset|renew|sec|second|min|minute|hr|hour|day|week|month)s?\b/i);
    const forward = stop === -1 ? tail : tail.slice(0, stop);
    const word = /\b(remaining|left|used|consumed)\b/i.exec(forward)
      || /\b(used|consumed)\W*$/i.exec(text.slice(Math.max(0, start - 16), start));
    if (!word) return 'unknown';
    return /^(remaining|left)$/i.test(word[1]) ? 'remaining' : 'used';
  }

  function readCardText(text) {
    if (!text) return null;
    // The FIRST percentage, unlike Claude's readRow: a Codex card renders its
    // own number before any neighbouring text can intrude.
    const pctMatch = text.match(/(\d+(?:\.\d+)?)\s*%/);
    const resetMatch = text.match(/Resets?\s+(?:(?:at|on|in)\s+)?(.+?)(?=\s*$|\s+(?:Daily|Weekly|All|Current|Personal|Team|5 hour)\b|\s+\d+(?:\.\d+)?\s*%)/i);
    return {
      raw: text.slice(0, 400),
      percent: pctMatch ? parseFloat(pctMatch[1]) : null,
      kind: polarityFor(text, pctMatch),
      reset_text: resetMatch ? resetMatch[1].trim() : null,
    };
  }

  function readCard(label, nextLabels) {
    const card = findCardByLabel(label);
    return readCardText(
      card ? visibleText(card) : windowTextAfterLabel(label, nextLabels));
  }

  // One field per catalog meter. `seed` carries the two primary cards already
  // read above; the rest are resolved by trying each catalog alias in order,
  // which is also how a primary card survives being relabelled - adding the
  // new wording to the override file is enough.
  function readCatalogCards(seed) {
    const out = {};
    for (const key of Object.keys(seed || {})) {
      if (seed[key]) out[key] = seed[key];
    }
    for (const entry of CATALOG) {
      if (out[entry.key]) continue;
      for (const alias of entry.aliases) {
        const card = readCard(alias, entry.boundaries || []);
        if (card) { out[entry.key] = card; break; }
      }
    }
    return out;
  }

  function pctCount(text) {
    return (text.match(/\d+(?:\.\d+)?\s*%/g) || []).length;
  }

  // Wording that names the usage panel. "usage limit" alone was both too
  // narrow and too broad: the analytics page carries prose about usage limits
  // above the cards, while the workspace-credit layout never says "usage
  // limit" at all. Plus every primary alias the catalog carries, so teaching
  // the app a relabelled card also teaches it where the panel is.
  const PANEL_MARKER = /usage limit|credit limit/i;
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

  // The cards themselves, as opposed to the wrappers around them: an element
  // carrying one percentage and no percentage-bearing descendant of its own.
  // Every wrapper between a card and the panel reports that card's percentage
  // too, so counting all of them multiplied the cards total by the nesting
  // depth - and six layers of wrapper was enough to let an element holding
  // the whole page pass the ratio below.
  let leafCardCache = null;
  function leafCards() {
    if (leafCardCache) return leafCardCache;
    const bearing = cardCandidates().filter(c => pctCount(c.text) >= 1);
    leafCardCache = bearing.filter(
      c => pctCount(c.text) === 1 &&
        !bearing.some(other => other !== c && c.el.contains(other.el)));
    return leafCardCache;
  }

  // A container that reached past the panel is worse than no container at all:
  // `in_container` is the whole difference between a meter and page furniture,
  // and every percentage on the page sits inside one of these. A panel is
  // mostly its cards; an element several times longer than the cards it holds
  // swallowed the page around them.
  function swallowedThePage(el, text) {
    let rowsLen = 0;
    for (const card of leafCards()) {
      if (card.el === el || !el.contains(card.el)) continue;
      rowsLen += card.text.length;
    }
    return rowsLen > 0 && text.length > rowsLen * 4;
  }

  // The element holding the usage panel, found by walking UP from the wording
  // that names it. Discovery is confined to it: a percentage in the task rail
  // is not a meter, and where it sits is the only way to tell.
  //
  // Taking the smallest element holding the marker and two percentages picked
  // <body> whenever the marker also appeared outside the panel, which is the
  // ordinary case - and inside <body>, a task's "90% done" is a meter.
  // Anchoring on the innermost elements that carry the marker and climbing to
  // the first ancestor holding two percentages keeps the answer inside the
  // panel. <body> is refused outright.
  function usageContainer() {
    const marked = cardCandidates().filter(c => marksUsagePanel(c.text));
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
    const bare = marked.filter(c => pctCount(c.text) === 0);
    let best = null;
    let bestLen = Infinity;
    for (const anchor of anchors) {
      let el = anchor.el;
      for (let depth = 0; depth < 12 && el; depth++) {
        if (el === document.body || el === document.documentElement) break;
        const text = visibleText(el);
        if (pctCount(text) >= 2) {
          const stray = bare.some(
            c => el.contains(c.el) && !c.el.contains(anchor.el));
          if (text.length < bestLen && !stray && !swallowedThePage(el, text)) {
            best = el;
            bestLen = text.length;
          }
          break;
        }
        el = el.parentElement;
      }
    }
    return best;
  }

  // Every card inside the usage container shaped like "<label> ... N% ...".
  // Deliberately permissive about wording and strict about shape; Python
  // applies the label rules before anything is adopted.
  function discoverCards() {
    const container = usageContainer();
    // null, not []: "the panel showed nothing new" is a completed scan and
    // "there was no panel to look in" is not one. Python cannot tell those
    // apart from an empty list, and it stamps the weekly scan on the answer.
    if (!container || !container.contains) return null;
    const out = [];
    const seen = {};
    for (const candidate of cardCandidates()) {
      if (out.length >= 40) break;
      const text = candidate.text;
      if (!text || text.length > 200) continue;
      if (candidate.el !== container && !container.contains(candidate.el)) continue;
      const percentages = text.match(/\d+(?:\.\d+)?\s*%/g) || [];
      // Exactly one percentage: two means this element wraps several cards,
      // and each of those is a candidate in its own right.
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
      const card = readCardText(text);
      if (!card || card.percent === null) continue;
      card.label = label;
      card.in_container = true;
      out.push(card);
    }
    // Nested wrappers: a parent that puts a heading in front of one card
    // renders that card's number under a longer label, and both are
    // candidates. Same percentage, same reset text, and one label ending in
    // the other means one card described twice - keep the child.
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

  const bodyText = visibleText(document.body);
  const personalTabReason = maybeSelectPersonalUsageTab(bodyText);
  if (personalTabReason) {
    return {
      __retry_after_ms: 1200,
      __retry_reason: personalTabReason,
      logged_out: false,
      session: null,
      weekly: null,
      url: location.href,
      title: document.title,
      body_text: bodyText.slice(0, 2000),
    };
  }

  const lowerText = bodyText.toLowerCase();
  const isLoggedOut =
    !!document.querySelector('a[href*="/auth/login"], a[href*="/login"]') ||
    location.pathname.includes('/auth/login') ||
    location.pathname === '/login' ||
    document.title.toLowerCase().includes('login') ||
    (/log in|sign in/.test(lowerText) && !/usage limit/i.test(bodyText));
  const session = readCard('5 hour usage limit', ['Weekly usage limit']);
  const weekly = readCard('Weekly usage limit', ['Personal usage', 'Team usage']);
  return {
    logged_out: isLoggedOut,
    session: session,
    weekly: weekly,
    // The two cards above stay the primary path, unchanged. Everything else
    // the catalog knows about is read alongside them.
    rows: readCatalogCards({ session: session, weekly: weekly }),
    discovered: DISCOVER ? discoverCards() : null,
    url: location.href,
    title: document.title,
    has_percent_text: /%/.test(bodyText),
    has_usage_text: /usage limit/i.test(bodyText),
    // Computed over the FULL body text. body_text below is truncated to 2000
    // chars, and the Codex page's task rail can push the analytics panel past
    // that cut — so deciding the weekly-only layout from the truncated copy
    // produced false rejects (a permanent 'error - stale' tile).
    // STRONG evidence: wording that only the shared-limit layout renders.
    // OpenAI has shipped several phrasings - "shared agentic usage limit"
    // (older) and "Codex and Work share the same usage limit" / "Workspace
    // monthly credit limit" (current). Matching only the first made a real
    // account read as a partial render.
    has_shared_agentic_text:
      /shared agentic usage limit|shares? the same usage limit|workspace monthly credit limit/i
        .test(bodyText),
    has_usage_summary_text:
      /credits remaining|usage breakdown/i.test(bodyText),
    body_text: bodyText.slice(0, 2000),
  };
})();
"""

# The module-level constant is the catalog as shipped, with discovery off. The
# provider rebuilds it per refresh so an override file (and a due scan) take
# effect without a restart.
EXTRACTOR_JS = extractor_source(
    EXTRACTOR_TEMPLATE, bundled_catalog("codex"), discover=False
)


_WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def _parse_reset_text(text: str | None) -> datetime | None:
    """Best-effort parse of strings like 'Mon 6:00 PM', '1:55 PM', or '2h 59m'."""
    if not text:
        return None
    text = text.strip().rstrip(".")
    text = re.sub(r"^at\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+at\s+", " ", text, count=1, flags=re.IGNORECASE)
    now = datetime.now()

    # Relative: "in 2 hr 59 min", "2h 59m", "6 hr 29 min"
    rel = re.match(
        r"(?:in\s+)?(?:(\d+)\s*(?:hr|h|hour)s?)?\s*(?:(\d+)\s*(?:min|m|minute)s?)?",
        text,
        re.IGNORECASE,
    )
    if rel and (rel.group(1) or rel.group(2)):
        hours = int(rel.group(1) or 0)
        minutes = int(rel.group(2) or 0)
        if hours or minutes:
            return now + timedelta(hours=hours, minutes=minutes)

    # Absolute date+time: "Apr 29, 2026 8:53 AM"
    for fmt in ("%b %d, %Y %I:%M %p", "%b %d %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    # Weekday + time: "Mon 6:00 PM", "Monday 18:00" → next matching weekday.
    weekday_match = re.match(r"([A-Za-z]+)\s+(.+)$", text)
    if weekday_match:
        weekday = _WEEKDAYS.get(weekday_match.group(1).lower())
        time_text = weekday_match.group(2).strip()
        if weekday is not None:
            for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
                try:
                    t = datetime.strptime(time_text, fmt).time()
                    days_ahead = (weekday - now.weekday()) % 7
                    candidate = datetime.combine(
                        now.date() + timedelta(days=days_ahead),
                        t,
                    )
                    if candidate <= now:
                        candidate += timedelta(days=7)
                    return candidate
                except ValueError:
                    continue

    # Time-of-day only: "1:55 PM" → today (or tomorrow if past)
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            t = datetime.strptime(text, fmt).time()
            candidate = datetime.combine(now.date(), t)
            if candidate < now:
                candidate += timedelta(days=1)
            return candidate
        except ValueError:
            continue

    return None


def _is_codex_analytics_url(url: str) -> bool:
    normalized = url.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    return normalized == CODEX_ANALYTICS_URL


def _payload_has_usage_signal(payload: dict[str, Any]) -> bool:
    if bool(payload.get("has_percent_text")) or bool(payload.get("has_usage_text")):
        return True
    page_text = f"{payload.get('title', '')} {payload.get('body_text', '')}".lower()
    return "%" in page_text or "usage limit" in page_text or "usage" in page_text


_POLARITY_STOP_RE = re.compile(
    r"\d|\b(?:reset|renew|sec|second|min|minute|hr|hour|day|week|month)s?\b",
    re.IGNORECASE,
)


def _polarity_near_percent(text: str, match: re.Match[str]) -> str:
    """Used/remaining wording sitting against the percentage.

    The Python twin of ``polarityFor`` in the extractor, kept in step with it
    for the same reason: this fallback reads a *text window* that can run into
    the next card, so testing the whole window for "used"/"remaining" resolved
    a bare percentage - and sometimes a neighbour's wording - to *used*.
    """
    tail = text[match.end() : match.end() + 60]
    stop = _POLARITY_STOP_RE.search(tail)
    forward = tail[: stop.start()] if stop else tail
    word = re.search(r"\b(remaining|left|used|consumed)\b", forward, re.IGNORECASE)
    if word is None:
        # Leading wording takes consumption words only: a countdown reads
        # "2 hr left", so accepting that before a number would invert the gauge.
        word = re.search(
            r"\b(used|consumed)\W*$",
            text[max(0, match.start() - 16) : match.start()],
            re.IGNORECASE,
        )
    if word is None:
        return "unknown"
    return "remaining" if word.group(1).lower() in ("remaining", "left") else "used"


def _parse_body_card(
    body_text: str,
    label: str,
    next_labels: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    text = re.sub(r"\s+", " ", body_text or "").strip()
    lower_text = text.lower()
    start = lower_text.find(label.lower())
    if start < 0:
        return None

    end = len(text)
    for next_label in next_labels:
        candidate = lower_text.find(next_label.lower(), start + len(label))
        if candidate >= 0:
            end = min(end, candidate)
    window = text[start:end].strip()
    pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", window)
    if not pct_match:
        return None

    reset_match = re.search(
        r"Resets?\s+(?:(?:at|on|in)\s+)?(.+?)(?=\s*$|\s+(?:Daily|Weekly|All|Current|Personal|Team|5 hour)\b|\s+\d+(?:\.\d+)?\s*%)",
        window,
        re.IGNORECASE,
    )
    return {
        "raw": window[:400],
        "percent": float(pct_match.group(1)),
        "kind": _polarity_near_percent(window, pct_match),
        "reset_text": reset_match.group(1).strip() if reset_match else None,
    }


def _body_card_for_spec(
    spec: MeterSpec,
    body_text: str,
    catalog: MeterCatalog,
) -> dict[str, Any] | None:
    """Plain-text fallback for one catalog meter, tried alias by alias."""
    boundaries = spec.boundaries or tuple(
        alias for alias in catalog.aliases() if not spec.matches(alias)
    )
    for alias in spec.aliases:
        card = _parse_body_card(body_text, alias, boundaries)
        if card:
            return card
    return None


def _looks_like_empty_signed_in_usage(payload: dict[str, Any]) -> bool:
    url = str(payload.get("url") or "")
    if not _is_codex_analytics_url(url):
        return False
    if _payload_has_usage_signal(payload):
        return False
    body_text = str(payload.get("body_text") or "").lower()
    return any(marker in body_text for marker in ("codex", "chatgpt", "tasks", "cloud"))


def _is_weekly_only_usage_layout(payload: dict[str, Any]) -> bool:
    """Return whether Codex rendered the newer shared weekly-limit layout.

    Prefers the booleans the extractor computes over the *full* page text.
    ``body_text`` is truncated to 2000 characters and the analytics panel can
    sit past that cut (the page carries a task rail first), so deciding this
    from the truncated copy produced false rejects — a permanently stale tile.
    The truncated text is still consulted as a fallback for payloads that
    predate those booleans (e.g. cached snapshots or hand-built test payloads).
    """
    return _weekly_only_layout_evidence(payload) is not None


_STRONG_WEEKLY_LAYOUT_MARKERS = (
    "shared agentic usage limit",
    "share the same usage limit",
    "shares the same usage limit",
    "workspace monthly credit limit",
)
_WEAK_WEEKLY_LAYOUT_MARKERS = ("credits remaining", "usage breakdown")


def _weekly_only_layout_evidence(payload: dict[str, Any]) -> str | None:
    """Classify how confidently the page identifies the shared weekly layout.

    Returns ``"strong"``, ``"weak"``, or ``None``.

    The distinction is load-bearing. A lone Weekly card reading 0% used looks
    identical to a half-rendered old two-card layout, so the caller keeps
    retrying on *weak* evidence. But when the page positively names the shared
    layout, a 0% reading is simply an untouched quota and must be believed -
    otherwise an idle account errors forever, which is exactly what shipped.
    """
    text = re.sub(r"\s+", " ", str(payload.get("body_text") or "")).lower()

    if payload.get("has_shared_agentic_text") or any(
        marker in text for marker in _STRONG_WEEKLY_LAYOUT_MARKERS
    ):
        return "strong"

    # Generic settings-page vocabulary that also appears beside a partially
    # rendered old two-card layout. Accepted, but not trusted enough to
    # believe a 0% reading.
    if payload.get("has_usage_summary_text") or any(
        marker in text for marker in _WEAK_WEEKLY_LAYOUT_MARKERS
    ):
        return "weak"
    return None


def _is_logged_out_payload(payload: dict[str, Any]) -> bool:
    url = str(payload.get("url") or "").lower()
    if "/auth/login" in url or "/login" in url or "/logout" in url:
        return True
    if bool(payload.get("logged_out")):
        return not (
            has_usage_page_signal(payload) or _looks_like_empty_signed_in_usage(payload)
        )
    return False


def _payload_rows(payload: dict[str, Any]) -> dict[str, Any]:
    """Per-meter cards from the payload, keyed by catalog key.

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


def _build_snapshot(
    payload: dict[str, Any],
    *,
    account_id: str = "codex",
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
            error="Not signed in to ChatGPT.",
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
            error="ChatGPT security verification required. Click Connect and complete the browser check.",
            raw=payload,
        )

    metrics: list[UsageMetric] = []
    unreadable: list[str] = []
    skipped: list[str] = []
    body_text = str(payload.get("body_text") or "")
    catalog = catalog or load_catalog("codex")
    rows = _payload_rows(payload)
    for spec in catalog.enabled_specs:
        card = rows.get(spec.key) or _body_card_for_spec(spec, body_text, catalog)
        if not card:
            continue
        reason = unreadable_reason(card, spec)
        if reason:
            # Only a primary card's unreadability is worth failing the whole
            # snapshot for; a breakdown card the page renders oddly must not
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

    # An informational card is dropped rather than failing the snapshot, but a
    # silent drop is invisible: the tile just shows one gauge fewer than the
    # page does, and nothing says which meter went or why.
    if skipped and log_skipped:
        log.info(
            "provider skipped unreadable rows provider=%s rows=%s",
            account_id,
            "; ".join(skipped),
        )

    # A card we could not read is reported, never quietly dropped: a bare
    # percentage has no polarity, and normalize_percent would resolve it to
    # *used*. The payload rides along so one error report carries the card text
    # needed to teach the extractor the new wording.
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
                "Codex's usage layout changed: could not read "
                + ", ".join(unreadable)
                + ". Use Copy diagnostics to report it."
            ),
            raw=payload,
        )

    # Primary cards only. Breakdown metrics are informational, so a page that
    # renders an extra meter must not read as a partial render of the two that
    # matter.
    labels = {metric.label.lower(): metric for metric in metrics if metric.tag is None}
    expected_primary = {
        spec.label.lower() for spec in catalog.enabled_specs if spec.primary
    }
    # Accept a lone Weekly card only when the surrounding page identifies the
    # new shared-agentic layout. This preserves transient-error retries for a
    # genuinely partial render of the older Session + Weekly layout.
    layout_evidence = (
        _weekly_only_layout_evidence(payload) if set(labels) == {"weekly"} else None
    )
    weekly_only_layout = layout_evidence is not None
    if weekly_only_layout and layout_evidence == "weak":
        weekly_metric = labels["weekly"]
        # On WEAK evidence only, a lone Weekly card reading 0% with an idle
        # countdown is indistinguishable from a mid-hydration render, so keep
        # retrying. On strong evidence the page has named the shared layout
        # outright and 0% means an untouched quota - believe it. Applying this
        # guard unconditionally made a genuinely idle account error forever.
        if (weekly_metric.percent_used or 0) <= 0 and weekly_metric.reset_label == "idle":
            weekly_only_layout = False
    if metrics and set(labels) != expected_primary and not weekly_only_layout:
        log_page_diagnosis(
            log,
            provider=account_id,
            classification="partial_usage_rows",
            payload=payload,
            expected_rows=_EXPECTED_ROWS,
            level=logging.WARNING,
        )
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.ERROR,
            error="Codex usage page only rendered part of the usage cards; retrying.",
            raw=payload,
        )

    session_metric = labels.get("session")
    weekly_metric = labels.get("weekly")
    if (
        session_metric is not None
        and weekly_metric is not None
        and (session_metric.percent_used or 0) > 0
        and weekly_metric.percent_used == 0
        and weekly_metric.reset_label == "idle"
    ):
        log_page_diagnosis(
            log,
            provider=account_id,
            classification="mixed_session_weekly_idle",
            payload=payload,
            expected_rows=_EXPECTED_ROWS,
            level=logging.WARNING,
        )
        return UsageSnapshot(
            provider=account_id,
            status=SnapshotStatus.ERROR,
            error="Codex usage page rendered an active session with an idle weekly card; retrying.",
            raw=payload,
        )
    if not metrics or all(m.percent_used is None for m in metrics):
        if _looks_like_empty_signed_in_usage(payload):
            log_page_diagnosis(
                log,
                provider=account_id,
                classification="empty_signed_in_usage",
                payload=payload,
                expected_rows=_EXPECTED_ROWS,
                level=logging.WARNING,
            )
            return UsageSnapshot(
                provider=account_id,
                status=SnapshotStatus.ERROR,
                error="Codex analytics loaded without usage cards; retrying.",
                raw=payload,
            )
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


class CodexProvider(Provider):
    name = "codex"
    display_name = "Codex"

    def __init__(
        self,
        parent: QObject | None = None,
        account_id: str = "codex",
        config: Config | None = None,
    ):
        self._parent = parent
        self._account_id = account_id
        self._config = config
        self._runner: ScrapeRunner | None = None  # held to prevent GC

    def refresh(self, on_done: Callable[[UsageSnapshot], None]) -> None:
        catalog = load_catalog("codex")
        # The scan is per provider *kind*, not per account: the cards are a
        # property of Codex's page, so the first account to refresh after the
        # interval does the scan and the others read the result.
        discover = scan_due(self._config, "codex")

        # ScrapeRunner rebuilds the snapshot after a transient error, so this
        # closure runs more than once per refresh. A scan is a once-per-refresh
        # event: without the flag the second attempt adopts again and saves the
        # config again.
        scanned = False

        def _build(payload: dict[str, Any]) -> UsageSnapshot:
            nonlocal scanned
            # Classify first. A logged-out, challenged or half-rendered page
            # still carries cards, and adopting from one writes page furniture
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
                    record_no_container_scan(self._config, "codex")
                return snapshot
            scanned = True
            if adopt_rows("codex", discovered, account_id=self._account_id):
                snapshot = _build_snapshot(
                    payload,
                    account_id=self._account_id,
                    catalog=load_catalog("codex"),
                    # The cards this refresh could not read were named in the
                    # log by the build above, off the same payload. Adoption
                    # adds meters, never makes one unreadable, so repeating
                    # the line here only doubled it.
                    log_skipped=False,
                )
            record_scan(self._config, "codex")
            return snapshot

        cache_buster = int(datetime.now().timestamp())
        self._runner = ScrapeRunner(
            account_id=self._account_id,
            url=f"{CODEX_ANALYTICS_URL}?aigauge_ts={cache_buster}#personal-usage",
            extractor_js=extractor_source(
                EXTRACTOR_TEMPLATE, catalog, discover=discover
            ),
            build=_build,
            log=log,
            wait_ms=7000,
            transport_max_attempts=1,
            build_max_attempts=2,
            parent=self._parent,
        )
        self._runner.run(on_done)
