# iOS / iPhone Feasibility Plan

> Feasibility assessment and phased plan for a personal-use iPhone companion to
> AI Gauge, with **home-screen widgets as the critical-path feature**. Written
> against this fork at `1.0.0+cfa.2`. This is a planning document; nothing in
> the app changes because it exists.

## Executive summary

<!-- filled after platform research -->

## 1. What the desktop app actually is (and why it matters for mobile)

AI Gauge is a local-only PyQt6 desktop app. There is no backend, no server, no
IPC, no export channel, and no sync mechanism anywhere in the codebase — an
exhaustive search for server frameworks, sockets, and localhost listeners
confirms the datasheet's "no backend" claim. The only machine-readable outputs
are the app-data files (`current.json`, `history.jsonl`, `ratio_state.json`,
`ratios.json`), written for the app's own bookkeeping.

Data acquisition splits into exactly two families:

| Family | Providers | Mechanism |
|---|---|---|
| Plain HTTPS + token | GitHub Copilot, OpenRouter | `requests` GETs with a Bearer PAT/API key; pure JSON parsing |
| Embedded-browser scraping | Claude, Codex, OpenCode | Offscreen `QWebEngineView` loads the provider's logged-in web page, injected JavaScript reads the rendered DOM |

That split is the entire feasibility question in miniature: the first family
ports to anything; the second is welded to a real browser engine.

## 2. Per-provider portability verdicts

### 2.1 Trivially portable — plain REST + key

**GitHub Copilot** (`src/aigauge/providers/copilot.py`). Five plain GETs
against `api.github.com` with `Authorization: Bearer <PAT>` headers
(`copilot.py:23-28`, `:57-122`). Parsing is pure dict traversal. The only Qt in
the file is the thread-pool wrapper, which becomes `URLSession` + async/await
on iOS. One catch: the monthly credit quota denominator is local config
(`config.py:362`), not an API value, so it must travel to the phone too.
**Runs fine inside a WidgetKit timeline provider.**

**OpenRouter** (`src/aigauge/providers/openrouter.py`). Three GETs against
`openrouter.ai/api/v1` (`/credits`, `/key`, `/activity`) with Bearer keys
(`openrouter.py:36-134`). Two keys (inference + management) both fit in the
iOS Keychain. **Runs fine inside a widget extension.**

### 2.2 Portable-with-risk — cookie replay might work, unproven

**OpenCode** (`src/aigauge/providers/opencode_go.py`). Still scraped via
WebEngine today, but two things make it the best candidate among the scraped
providers: the extractor reads structured `data-slot` attributes rather than
English prose (`opencode_go.py:29-32`), and there is a complete pure-regex
fallback over flat page text (`_parse_body_usage`, `:126-145`) that needs no
DOM at all. The session cookie is deliberately not HttpOnly
(`webview/cookies.py:175-181`), so it is easy to export from a desktop
browser. If the page (or an underlying endpoint) serves usable content to a
plain HTTPS GET with that cookie, an iOS client could fetch it directly.
**Worth a 30-minute `curl` experiment before committing either way.**

### 2.3 Not portable without new work — browser-dependent

**Claude** (`src/aigauge/providers/claude.py`). The extractor reads
`innerText` of a hydrated React SPA at `claude.ai/settings/usage`; a raw GET
returns an app shell with no numbers. It needs a 3 s settle plus up to 20
in-page re-runs, can navigate itself back to the usage route, and must survive
Cloudflare interstitials — which the desktop app does partly by spoofing a
Chrome-on-Windows user agent (`webview/profile.py:18-22`). A JSON path exists
in principle: the API-shape capture (`webview/api_capture.py`) records the
shape of the JSON the usage page fetches, and `five_hour` / `seven_day` are
known-real key names read from Claude's shipped bundle (`claude.py:164-168`).
But the mapping was deliberately never written — the endpoint URL, envelope,
and headers have not been observed (`docs/next-session.md:130-157`).

**The single highest-leverage unblocking step for the whole iOS effort:** run
the desktop app against a live account, use Copy diagnostics, and read the
captured `api` block. That observation converts Claude from
"browser-required" to "cookie + JSON endpoint," for the desktop app and any
iOS client alike.

**Codex** (`src/aigauge/providers/codex.py`). Everything above, plus a hard
blocker Claude does not have: the extractor must **click** the
"Personal usage" tab in the DOM to reveal the data (`codex.py:50-73`). No HTTP
client can do that. API capture was never enabled for Codex, so nothing is
known about its underlying JSON. Hardest of the five to port.

### 2.4 What ports unchanged

`models.py` (the entire data model), `_common.normalize_percent`,
`codex._parse_reset_text` (the shared reset-time parser),
`idle.idle_session_weekly_metrics`, and the gauge severity-band logic are pure
functions and dataclasses. They translate to Swift line-for-line and define
the widget's rendering contract.

## 3. Secrets and the cookie-staleness trap

Secrets per provider: a GitHub PAT and two OpenRouter keys (OS credential
store), and per-account cookie blobs for Claude/Codex/OpenCode. The stored
cookie is **the raw string the user originally pasted** — the app injects
cookies into its Chromium profiles but never reads them back out
(`webview/cookies.py` has a set path and no get path). The live, rotating
session cookie exists only inside the Chromium `Cookies` SQLite file per
profile and is never exported.

Consequence for any phone sync: what is in the keyring drifts from what is
valid. Syncing "the stored cookie" to a phone hands the phone a token that may
already be stale. A real cookie-sync feature would require adding a
cookie-extraction path to the desktop app (`QWebEngineCookieStore` exposes
`cookieAdded`), which is net-new work with real security-posture implications
for a fork whose identity is "audited, no covert egress."

## 4. Refresh cadence facts that bound widget freshness

The desktop app refreshes serially, one provider at a time, adaptively between
5 min (active) and 60 min (idle, exponential backoff on unchanged cycles),
with reset-aware pull-forward and 1-minute error retries (`app.py:59-66`,
`:117-128`, `:466-577`). A cold start takes 40–70 s before all tiles report;
scraped providers cost tens of seconds each. Any architecture that puts
scraping on the phone inherits those costs on a device that suspends apps
aggressively; any architecture that reuses the desktop's data inherits its
cadence as the freshness floor.

<!-- Sections 5+ (platform options, widget architecture, recommendation,
     phased plan) filled after platform research. -->
