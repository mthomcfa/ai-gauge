# iOS / iPhone Feasibility Plan

> Feasibility assessment and phased plan for a personal-use iPhone companion to
> AI Gauge, with **home-screen widgets as the critical-path feature**. Written
> against this fork at `1.0.0+cfa.2`. This is a planning document; nothing in
> the app changes because it exists.

## Executive summary

**Feasible — but only as a native widget, and most cheaply as a thin remote
display of the desktop app's data.**

1. **The "web-emulated" option is eliminated by the widget requirement, not
   merely degraded.** iOS home-screen web apps have no widget API as of
   iOS 26, none is announced, and widgets are a native app-extension point
   with no web-facing surface. A PWA's ceiling is push notifications plus a
   numeric icon badge — a notification system, not a gauge. If widgets are
   critical path (they are, per this plan's brief), the phone-side surface
   must be a native WidgetKit extension.
2. **Do not port the hard providers to the phone; publish to it.** Claude,
   Codex, and OpenCode are embedded-browser scrapers; Codex even requires a
   DOM click. A widget extension cannot run a web view. The desktop app
   already computes every number — the cheap, robust design is a small
   net-new export from the desktop (a "last snapshot" JSON the codebase's own
   notes already want for startup-cache reasons) served over an authenticated
   HTTPS tunnel, with the iPhone widget fetching that one URL directly.
3. **Freshness is bounded but workable.** WidgetKit budgets roughly 40–70
   refreshes/day (~every 15–60 min) — comparable to the desktop app's own
   idle cadence. Three escape hatches fit personal use exactly: WidgetKit
   Developer Mode lifts the budget entirely for dev-signed builds; an
   interactive tap-to-refresh App Intent is unbudgeted and instant; StandBy
   refresh is unbudgeted while charging. Design rule: always render
   *value + age*, never a bare percentage.
4. **A zero-build MVP exists and should be built first.** A Scriptable
   (JavaScript) widget fetching the published JSON delivers a working
   home-screen gauge in about an hour with no Apple account, no Xcode, and no
   Mac — and empirically measures the one number that decides everything
   else: the real refresh cadence on this user's phone.
5. **Distribution is the annoying part, not the blocking part.** The
   developer runs Windows/Linux; Xcode needs macOS somewhere (cloud CI or a
   cheap Mac). A free Apple ID can likely carry a personal widget app but
   forces 7-day re-signing and denies App Groups/push; the US$99/yr program
   (a nonprofit fee waiver may apply) removes far more friction than it
   costs. Cross-platform frameworks buy nothing here — the widget is Swift
   either way.

Recommended path in one line: **Phase 0** desktop snapshot exporter +
Scriptable widget (hours, $0) → **Phase 1** native SwiftUI app + WidgetKit
extension rendering the same JSON (a weekend-scale project) → **Phase 2**
optional on-device direct fetch for Copilot/OpenRouter and, once its JSON
endpoint is finally observed, Claude.

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

## 5. iOS delivery options, judged against the widget requirement

Platform facts below are from Apple's current developer documentation and
2025–2026 primary sources unless flagged; the research pass's confidence
notes are carried into §9.

### 5.1 PWA / home-screen web app — ruled out

There is no widget API for web apps on iOS — not in iOS 26, not in the
iOS 27 betas, not proposed anywhere in WebKit's roadmap. Widgets are `.appex`
bundles rendering archived SwiftUI views in a separate process; a browser
engine has no way in, and even the EU's alternative-engine regime changes
nothing about that. What a home-screen web app *can* do — web push (iOS 16.4+,
home-screen apps only, no silent push), the Badging API (a number on the
icon), generous-but-not-guaranteed storage — adds up to a threshold-alert
system, not a gauge. **A web-emulated AI Gauge cannot meet the widget
requirement. Full stop.**

### 5.2 Native SwiftUI + WidgetKit — the real numbers

- **Refresh budget:** per Apple, a frequently-viewed widget gets roughly
  **40–70 reloads per 24 h — every 15–60 minutes** — with a ~5-minute minimum
  spacing between timeline entries. The budget adapts to how often the widget
  is actually looked at.
- **Widgets may fetch the network themselves.** Apple documents and samples
  network requests inside the timeline provider, recommending a background
  `URLSession` so a halted extension can resume. This is the load-bearing
  fact for the recommended architecture: the widget does not need the
  container app to run at all.
- **Unbudgeted refresh paths that fit this app:** interactive widget buttons
  (iOS 17+ App Intents — a tap-to-refresh control is guaranteed and
  unbudgeted, though inert while the phone is locked); reloads while the
  container app is foregrounded; **StandBy** (phone charging on a stand —
  effectively a desk gauge, refreshed at system rate without touching the
  budget); and **WidgetKit Developer Mode** (`Settings → Developer`), which
  per Apple's WWDC25 session lifts push and reload budgets entirely for
  development-signed builds — exactly what a personally sideloaded app is.
  It does not apply to TestFlight/App Store builds.
- **iOS 26 adds WidgetKit push** (`apns-push-type: widgets`): a self-hosted
  server can nudge the widget to reload when a value actually moves. Still
  budgeted — push spends the budget at the right moments rather than beating
  it. Requires the paid program (push entitlement on the widget target).
- **Live Activities are not a substitute:** capped at 8 h active + 4 h on the
  lock screen, and the extension has no network access. They fit one niche
  here — a deliberate "coding session" meter — not a permanent gauge.
- **Practical constraints:** ~30 MB extension memory (crash-report-derived,
  not documented — trivially fine for numbers, rules out heavy rendering);
  keychain items a background widget reads must use
  `kSecAttrAccessibleAfterFirstUnlock`, or refreshes on a locked phone fail.
- **Staleness honestly stated:** 15–30 min typical when the widget lives on
  page 1 of a frequently-used phone; worse on ignored pages and in Low Power
  Mode; frozen overnight; instant on tap. The widget must always render
  *last-known-good + age* ("73% · 24 min ago") — SwiftUI's relative-date text
  ticks the age live without spending any budget.

The desktop app's own adaptive cadence is 5–60 minutes (§4), so a native
widget's passive freshness is *the same order as the data source it would
display*. The budget is not the bottleneck; the desktop's scrape cadence is.

### 5.3 Cross-platform frameworks — no leverage for a widgets-first app

In React Native/Capacitor/Flutter/.NET MAUI alike, the widget is still a
Swift/SwiftUI WidgetKit extension; the frameworks only wrap the App-Group
plumbing. The one exception that removes Swift — Expo's new `expo-widgets`
(SDK 56) — removes it by removing the timeline provider: its generated
provider only replays a timeline the React Native *app* wrote into App Group
storage, so the widget can never fetch for itself and goes stale whenever the
app isn't run. That is precisely the wrong shape for this use case. For an
app whose only meaningful surface is the widget, plain SwiftUI + WidgetKit is
*smaller* than any cross-platform host. (If RN skills were already in play,
`@bacons/apple-targets` with a hand-written Swift widget is the defensible
variant.)

### 5.4 Low-code widget hosts — the legitimate Phase 0

**Scriptable** (free) renders home- and lock-screen widgets from JavaScript
and can fetch a JSON URL from inside the widget. Same WidgetKit budget
underneath; refresh is a request (`refreshAfterDate`), not a guarantee;
tapping the widget re-runs the script. Caveat: its last confirmed update is
2024-era — verify current iOS compatibility on the App Store before relying
on it. **Widgy** (visual designer, JSON data sources, gauge/arc elements) and
purpose-built "JSON URL → widget" apps are alternatives worth 15 minutes
each. Shortcuts alone cannot render data or force third-party widget
reloads. Any of these turns a published snapshot JSON into a working
personal gauge in an hour, with zero Apple developer footprint — which is
why the phased plan starts there.

## 6. Getting a personal app onto the phone (no Mac on the desk)

The developer's desktops are Windows/Linux; Xcode runs only on macOS. The
realistic ladder:

| Tier | Cost | What it gives / costs |
|---|---|---|
| Free Apple ID ("Personal Team") | $0 | On-device install, 7-day profile expiry (SideStore/AltStore/Sideloadly automate re-signing), ~3 concurrent sideloaded apps, **no entitlements**: no App Groups, no push, no background modes. Widgets making their own network fetches need none of those — but sideloading tools warn extensions "may not work" on free teams, and no Apple statement settles it. **Day-one task: prove a hello-world widget installs on a free team before writing anything else.** |
| Apple Developer Program | US$99/yr | 1-year profiles, all entitlements (App Groups, Keychain Sharing, WidgetKit push), TestFlight (90-day builds), CI-friendly signing. **Fee waivers exist for nonprofits** — potentially applicable here and worth checking before paying. Note: TestFlight builds forfeit WidgetKit Developer Mode's budget lift; keep dev-signed installs for maximum freshness. |
| Build machine | $0–cheap | Mac-less is workable: EAS Build / Codemagic / GitHub Actions macOS runners produce the IPA; SideStore or ad-hoc installs deliver it. It is the fiddliest path (no Simulator, widget layout iterated blind). A used Mac mini pays for itself in Xcode previews if the widget's appearance matters. |

The free tier's entitlement gap has a silver lining: it *forces* the
widget-fetches-directly architecture, which is the better design anyway — no
App Group, no container-app involvement, nothing on the critical path but
the widget and the URL.

## 7. Recommended architecture

**Desktop publishes; phone renders; provider porting is optional and later.**

```
AI Gauge desktop (already running in production)
  └─ NEW, opt-in: write snapshot.json atomically after each refresh cycle
       └─ served via Tailscale Funnel or Cloudflare Tunnel
            └─ https://<private-host>/snapshot.json   [long random bearer token]
                 ├─ Phase 0: Scriptable widget — fetch, draw gauge, show age
                 └─ Phase 1: SwiftUI app + WidgetKit extension
                      ├─ timeline provider: background URLSession fetch,
                      │    entries ~15 min apart, .after(date) reload policy,
                      │    cached last-good rendered with live age text
                      ├─ App Intent "refresh now" button (unbudgeted, instant)
                      ├─ StandBy layout for desk-charging use
                      └─ [paid tier, later] WidgetKit push on >2% movement
```

Why this shape wins, in the codebase's own terms:

- **It reuses the only component that can read Claude/Codex/OpenCode** — the
  desktop's WebEngine scrapers — instead of reimplementing the two providers
  classified "browser-required" (§2.3) on a platform whose widget extensions
  cannot run a browser at all. Every provider the desktop shows, the phone
  shows, including multi-account Claude/Codex, at zero porting cost.
- **No secrets leave the desktop.** The phone holds one bearer token for one
  self-hosted URL — not five providers' cookies and keys. The cookie-staleness
  trap (§3) never opens, because cookies never travel.
- **The export artifact is already half-designed.** `docs/next-session.md`
  independently wants a dedicated "last snapshot" file for startup caching;
  this plan's `snapshot.json` is that file plus per-metric `status`, current
  (not peak) percent, `fetched_at`, display names, and band colors — the
  fields `current.json` lacks (§2.4 of the data-layer analysis; history.py
  drops null-percent rows and stores peaks).
- **Freshness composes honestly.** Desktop cadence (5–60 min adaptive)
  ~matches widget budget (15–60 min); tap-to-refresh and Developer Mode
  close the gap when it matters. The widget shows the *snapshot's*
  `fetched_at` age, so desktop-asleep and tunnel-down states are visible
  truths instead of silent lies.
- **Tunnel choice is deliberate:** Funnel/Cloudflare Tunnel give a publicly
  trusted TLS cert, so no ATS exceptions, and no VPN in the widget's critical
  path — widget-over-Tailscale-VPN has documented failure modes (MagicDNS
  from extension processes being the flakiest link). Raw tailnet access is
  the fallback, by CGNAT IP with an ATS exception in the *widget's*
  Info.plist, accepting intermittent blanks.

**Security posture, addressed rather than hand-waved.** This fork's identity
is "audited, local-only, no covert egress," and the repo has recorded prior
art against opening ports and adding export SDKs (`docs/next-session.md`
declined both a CDP localhost port and OpenTelemetry, calling export "a
separate, opt-in feature — an output channel, not an input strategy"). This
plan is that opt-in output channel, built to the same standard: off by
default; a local file write only (the tunnel is the user's infrastructure,
not the app's); sanitized content (usage numbers, labels, timestamps,
status — no cookies, no tokens, no account emails); and a same-PR update to
`README.md`, `SECURITY.md`, and the datasheet's "no export" claims. The
desktop app itself still opens no listening port under this design.

**Rejected alternatives, for the record:** full on-device reimplementation
(loses Claude/Codex outright — the widget can't run a browser, and the phone
would inherit the Cloudflare/UA-fingerprint fight with a worse hand);
hidden-`WKWebView` scraping in the container iOS app (works only while the
app is foregrounded — the widget would go stale exactly when it matters);
iCloud/CloudKit as transport (server-to-server machinery for one small JSON,
and the Python side would need an ADP container); `BGAppRefreshTask` as the
primary mechanism (community-measured intervals of 15 minutes to 6+ hours to
never); a PWA (§5.1).

## 8. Phased plan

**Phase 0 — prove it end-to-end with zero build (hours, $0).**
1. Desktop: add the opt-in snapshot exporter — a `SnapshotPublisher` that
   serializes each cycle's `UsageSnapshot`s (status, current percents, reset
   times, `fetched_at`, display names, band colors) and atomically writes
   `snapshot.json` beside `current.json`. Small, testable, and independently
   valuable (it is the startup-cache file `next-session.md` already wants).
2. Serve it: `tailscale funnel` (or `cloudflared`) fronting a static file,
   plus a long random bearer token.
3. Phone: a ~50-line Scriptable widget — fetch, pick the worst gauge (the
   same `provider_max_percent` logic, ignoring model-breakdown rows), draw
   percent + reset + age. Evaluate Widgy in the same hour.
4. **Measure the real refresh cadence for 48 hours.** This one number decides
   how much Phase 1 is worth.

**Phase 1 — the native widget (weekend-scale once Phase 0 data is in).**
1. Settle the account tier: check the nonprofit fee waiver; otherwise decide
   free-with-SideStore vs. US$99. Either way, first commit is a hello-world
   widget installed on the actual phone (the free-team widget risk in §6).
2. SwiftUI app + WidgetKit extension per §7: small/medium/lock-screen
   families, tap-to-refresh App Intent, last-good caching, age text,
   severity band colors carried over from the desktop config.
3. Enable WidgetKit Developer Mode; widget on home page 1; tune timeline
   spacing to the desktop's publish cadence.
4. Port the pure logic (`models.py`, `normalize_percent`,
   `_parse_reset_text`, band thresholds) as Swift structs with the Python
   tests' cases carried over.

**Phase 2 — reduce desktop dependence (optional, incremental).**
1. On-device direct fetch for **Copilot and OpenRouter** in the timeline
   provider (§2.1 — line-for-line ports), keys in the iOS Keychain
   (`AfterFirstUnlock`). The widget then survives the desktop being off, for
   the two REST providers.
2. Run the OpenCode cookie-replay experiment (§2.2, the 30-minute `curl`
   test); promote it on-device only if plain HTTPS works.
3. **Claude's JSON endpoint**: perform the observation the repo has been
   waiting for — run the desktop app, Copy diagnostics, read the captured
   `api` block, write the mapper (desktop first, per `next-session.md`'s
   design: JSON primary, DOM fallback, refuse on material disagreement).
   Only then consider Claude-direct-from-phone, with eyes open about UA/TLS
   fingerprinting versus Cloudflare. **Codex stays desktop-fed
   indefinitely** (DOM click requirement).
4. If on the paid tier: WidgetKit push from the desktop publisher on >2%
   movement, making the passive widget effectively event-driven.

**Explicit non-goals:** App Store distribution, Android, alerting/telemetry
beyond the snapshot file, Live Activities beyond an optional session meter,
and any design that copies provider cookies to the phone.

## 9. Risks and open questions

| Risk | Severity | Mitigation |
|---|---|---|
| Widget extensions may not install on a free Personal Team (unconfirmed either way) | Blocks the $0 tier only | Day-one hello-world widget test; fall back to paid program (or fee waiver) |
| WidgetKit Developer Mode's budget lift is WWDC-session lore, not written docs | Freshness ceiling drops to the standard 15–60 min | Architecture already works at standard budget; tap-to-refresh covers urgency |
| Scriptable staleness (last confirmed update 2024) | Phase 0 only | Verify App Store listing first; Widgy / "API widget" apps as substitutes |
| Desktop must be awake for fresh data (Phases 0–1) | Inherent to the publisher model | Age display makes it visible; Phase 2 REST-direct providers cut the dependence; desktop already runs in production all day |
| Publishing endpoint contradicts the fork's "no export" documentation claims | Reputational/consistency, not technical | Opt-in, documented, sanitized; amend README/SECURITY/datasheet in the same PR |
| Tunnel dependency (Funnel/Cloudflare) is third-party infrastructure | Low for personal use | Bearer token + sanitized payload limit blast radius; raw-tailnet fallback exists |
| Claude/Codex page changes still break the source data | Same as today | Unchanged — the phone inherits desktop fixes automatically, which is the point of the publisher model |
| Snapshot JSON becomes a de-facto API frozen by the widget | Low | Version the schema from day one (`"schema": 1`) |

Open questions parked for their phases: exact CAD program price and waiver
eligibility (Phase 1 step 1); OpenCode cookie replay (Phase 2 step 2);
Claude endpoint shape (Phase 2 step 3 — the standing highest-leverage
observation).

## 10. Verdict

An iPhone AI Gauge is **feasible and cheap in its recommended form**: a
desktop-published snapshot rendered by a native widget, reachable in a
weekend of real work after a $0, hours-long Scriptable proof. It is
**infeasible as a pure web/PWA app** (no widgets, hard stop) and
**uneconomical as a full on-device port** (Claude and Codex would have to be
re-fought on a platform that denies widgets a browser). The critical-path
feature — home-screen widgets — is exactly the thing that dictates both the
"native" and the "publisher" halves of that answer.
