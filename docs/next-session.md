# Next session — parked items

State at close of the 2026-08-10 session. `main` is `1.0.0+cfa.2` at PRs #6–#16,
610 tests passing, all five providers reading.

> **Updated 2026-09-15** by the refresh-cadence work (`1.3.0+cfa.5`,
> 1 216 tests). Its residuals are folded into
> [§8.3](#83-known-soft-spots-in-what-was-built) rather than given a section
> of their own, because they are the same scheduler.
>
> **Updated 2026-09-10** by the Microsoft/Azure work (`1.2.0+cfa.4`, 907 tests).
> Its own parked items are in [§8](#8-parked-from-the-microsoftazure-work).
> Before it, `1.1.0+cfa.3` moved the Claude/Codex label definitions out of the
> extractors into a meter catalog (§7) and fixed the Codex polarity defect that
> headed §4. Everything in §§1–6 below is unchanged and still current.

`1.0.0+cfa.1` does not start — it raises `AttributeError` during `App.__init__`.
Fixed in `+cfa.2`; the two carry different version strings on purpose, because
`app_version` in a diagnostics blob has to identify which build produced it.

**Issues are disabled on this repo** (the GitHub API returns `410`), so this
file is the issue tracker. Enable them at *Settings → General → Features →
Issues* if you would rather have real ones; nothing below is blocked on that.

Two issues were written up during the session for filing. **Both were fixed
before Issues was ever enabled** and are not carried forward: the macOS
menu-bar dot counting tagged breakdown rows, and `OpenRouterConfig.daily_budget`
raising and discarding its whole config block. Both landed in PR #6.

---

## 1. Verification — one closed, one still open

### 1.1 Claude's numbers — VERIFIED 2026-08-10, correct

Session and Weekly were compared against `https://claude.ai/settings/usage`
directly and both matched. This is the first time the polarity and attribution
heuristics have been checked against reality rather than against reconstructed
layouts, and they came out right.

**What that does and does not establish.** It confirms the heuristics read
*today's* Claude layout correctly. It does not make them robust: they still
infer polarity from wording near a number and attribution from DOM proximity,
and Claude changed this surface three times in the week before the check. The
API mapper in §3 remains the durable fix — the difference is between "verified
against the current page" and "no longer guessing".

Re-check after any Claude layout change. The original problem statement is
kept below because it explains why the refusal behaviour exists.

---

The gauges read. Whether they read *correctly* was unverified until the check
above.

Session and Weekly percentages come from `readRow` in
`src/aigauge/providers/claude.py`, which infers **polarity** (used vs
remaining) from wording near the number and **attribution** (which meter a
percentage belongs to) from DOM proximity. Both heuristics were rewritten three
times in one week; each rewrite fixed one inversion and introduced another,
caught only by adding tests.

They are now calibrated against nine reconstructed layouts executed in a real
browser, and they refuse rather than guess when a reading cannot be justified.
That is not the same as being right about what Claude actually renders.

**What settles it:** open `https://claude.ai/settings/usage` beside the app and
compare both accounts. Five minutes.

**Why it matters:** a plausible-but-wrong number is the one failure mode that
does not announce itself. Everything else in this app now errors loudly.

### 1.2 The 2026-08-10 security audit is single-source

`SECURITY-AUDIT.md`'s addendum covers the `window.__ag_api` finding — a real
defect, reproduced in a browser, fixed, mutation-verified. But unlike the
original 0.6.3 audit, **no second engine checked it**. A Codex CLI review was
attempted twice; the sandbox denies `api.openai.com` (`403` on CONNECT).

The finding was also both introduced and fixed by the same author in the same
session, which is the weakest possible review posture.

**What settles it:** run `codex review` (or any second engine) locally against
`db89739..HEAD`, focusing on `src/aigauge/webview/api_capture.py`.

---

## 2. Release — two decisions parked, not tasks

> **Both are undecided.** The maintainer has explicitly not committed to either
> and wants to think about them. The facts below are recorded so the thinking
> does not have to start from scratch — they are *not* a recommendation to act.
> Do not do either of these without asking.

### 2.1 Whether to tag a release at all

`v1.0.0+cfa.2` would build artifacts for all three platforms and open a draft
GitHub Release. Merging changes nothing on its own; only a `v*` tag starts the
workflow.

What informs the decision:

- **The repository has no tags at all.** `git ls-remote --tags origin` returns
  nothing, so the release workflow has never run once. A first tag is also the
  pipeline's first live test.
- **The macOS artifact path is the untested part.** It was restored in PR #5,
  including the post-relocation re-sign in `build.sh`, and has never been
  exercised end to end. Windows and Linux paths are older but equally unrun
  under this fork.
- **Nothing depends on a release.** The app is installed from source and works.
  Tagging buys distributable binaries and provenance attestation; it does not
  buy the maintainer anything they do not already have.
- If the pipeline is worth de-risking first, a throwaway tag (`v1.0.0+cfa.0`)
  exercises it, and the resulting draft release can be deleted without
  publishing.

### 2.2 Whether to delete `scratch/release-dryrun`

A stray remote branch. It cannot be deleted from the dev environment — the git
proxy denies branch deletes with `403` — so it needs
`git push origin --delete scratch/release-dryrun` from the maintainer's machine.

What informs the decision, checked 2026-08-10:

- **It holds nothing that `main` does not**, apart from one commit. Diffed
  against `main`, it is 3,135 deletions to 90 insertions — i.e. it is behind,
  not ahead.
- **Its one unique commit is `8d73d15`**, "scratch: release pipeline dry run at
  0.0.1+cfa.1 (throwaway, to be deleted)", which only sets the version string to
  `0.0.1+cfa.1` in four files for a dry run that never happened, because the tag
  push was blocked.
- It reads as unmerged history only because PRs #4 and #5 were **squash**-merged,
  so the branch's originals are not ancestors of `main` even though their
  content is.

On the evidence, deleting it loses nothing. That is an observation, not a
decision — it is still the maintainer's call.

---

## 3. The API mapper — the next real piece of work

The capture landed in PR #9 and is live. The mapper is not written, deliberately:
Claude's field names were never observed, and writing a mapping against imagined
ones is the mistake this whole session was made of.

**How to unblock it:** the next time a Claude tile errors, hit *Copy
diagnostics*. The `api` key names the endpoints the page fetched and the shape
of each response. Write the mapping from that.

**Target design:** read `utilization` and `resets_at` directly; keep the DOM path
as a fallback; refuse when the two disagree materially. That retires both the
polarity and attribution heuristics — they stop being load-bearing rather than
being made cleverer.

**Known limits of the capture, all in `src/aigauge/webview/api_capture.py`:**

- Only same-origin responses are recorded. If usage is fetched from
  `api.claude.ai` rather than `claude.ai`, it is dropped. Loosening to
  `*.claude.ai` widens the capture surface, so it was left strict.
- `MAX_URLS = 12`, first-come-first-served. The settings page resolved eight
  endpoints before usage in the observed run, so there is headroom but not much.
- `window.__ag_api` resets on navigation, so anything captured before a route
  recovery is lost.

If the capture comes back with **no usage endpoint** even on a fully rendered
page, the numbers arrive by RSC streaming or from cache, and the approach needs
rethinking rather than extending.

---

## 4. Known defects, deliberately not fixed

Each was found during the session and left alone with a reason. None is
speculative.

| Where | What | Why it was left |
| --- | --- | --- |
| `webview/verify.py` → Claude check | A `/login` anchor is a hard veto, while `providers/claude.py`'s `isLoggedOut` ANDs it with absent usage text. Verify is stricter than the extractor, in the direction of the reported sign-in loop. | Loosening sign-in semantics without evidence risks the opposite failure: a bad session verifying, then erroring forever. |
| `menubar.py` → `_provider_max_percent` | Short-circuits on a metric labelled `session`, while `gauge.provider_max_percent` takes the worst metric. The two can disagree for the same provider. | Pre-existing and documented in the module. The tag-filter half was fixed in PR #6; this divergence predates it. |
| `webview/scraper.py` → timeout | Wall-clock, so it does not account for system sleep. A laptop resumed after two days reported `elapsed_s: 228477` and fired a stale scrape per provider. | **Half-fixed in 1.3.0+cfa.5**: a `timeout` whose measured elapsed exceeds the whole scrape budget by a wide margin is logged as `classification=resume_artifact` and does not count toward that provider's error retry. The timing itself is unchanged — the scrape still fails on resume; it just no longer buys the app a fast retry it did not earn. **Still half:** the threshold is `timeout_ms x max_attempts x 3` (240 s for Claude, 75 s for the others) while the App watchdog fires at 180 s and 70 s, so on an *awake* machine the watchdog always wins and the label never reaches the scheduler; and on a genuine suspend Claude's `transport_max_attempts=2` takes the retry branch and discards the classification. `_started_at` is also set once in `__init__` and not reset per attempt, so once a scraper has spanned a suspend every later timeout in it is labelled a resume artifact. Comparing against the App's budget rather than the scraper's, and bypassing the retry branch for a classified resume artifact, is the rest of the fix; it is scraper timing rather than scheduler behaviour and was left for its own change. |
| `app.py` → `_error_retry_time` | The fast retry was **cycle-wide, not per-provider**. One permanently-failing provider made every cycle count as failing, so healthy providers got refreshed every minute too until the bound engaged — and once it had (the counter never reset, because no cycle was ever clean), a genuinely transient failure on a *different* provider got no fast retry at all. | **Fixed in 1.3.0+cfa.5.** `_consecutive_error_cycles` / `_error_retry_time` are now a per-provider map `{name: (consecutive_errors, next_due_at)}`: an ERROR schedules that provider's own retry at 1, 2 and 4 minutes and then falls back to the normal cadence, OK or AUTH_REQUIRED clears it, and a wake that is only a retry refreshes **only the due providers** (`reason=error_retry`). The desktop log is what forced it: 124 of 137 cycles carried at least one ERROR or AUTH_REQUIRED, and 32% of cycles started within two minutes of the previous one. |
| `webview/api_capture.py` → `sketch` | Numbers survive redaction verbatim, so a numeric account ID in a response would reach the log. Strings and UUIDs are reduced to length markers. | Deliberate: the quota values *are* numbers. Redacting them would defeat the capture. Local-only, and the user controls the log. |

---

## 5. Performance — scoped, measured, not built

Cold launch takes roughly 40–70s before every tile reports. Measured, not
estimated: a single Claude scrape against an unreachable network took 26.3s.

The cost is not Python. It is three things, in value order:

1. **Nothing is painted from cache at launch.** `history.HistoryStore` already
   persists `peak_pct` and `resets_at` per (provider, label), but nothing reads
   the last values at startup, so every tile starts empty and fills serially.
   A small dedicated "last snapshot" file would be cleaner than bending
   `record_snapshot`, which keys on `resets_at`.
2. ~~**The refresh queue is strictly serial.**~~ **Half-fixed in
   1.3.0+cfa.5.** The REST providers (Copilot, OpenRouter, Azure) are
   dispatched together at cycle start; the browser providers still run one at
   a time, because QtWebEngine is GUI-thread-only and each scrape holds a
   profile. A cycle is now roughly the browser queue, with the cheap tiles
   filled in the first second rather than after it. Fully parallel refresh -
   two `QWebEngineView`s at once - remains a design decision, not a patch:
   concurrent profile locking and renderer memory are the open questions.
   The serial rule is now enforced in two places rather than one: the App
   parks a provider whose dispatch its watchdog abandoned, and
   `ClaudeProvider`, `CodexProvider` and `OpenCodeGoProvider` each refuse a
   re-entrant refresh while `account_is_busy(<account id>)` - a module-level
   registry in `providers/_scrape_runner.py`, keyed by account rather than
   held on the provider object, because a settings save replaces that object.
   (`ScrapeRunner.busy()` is the same question asked through a runner; it has
   no caller in `src/`.) Both would have to be undone deliberately, which is
   the point.
3. **Fixed pre-extractor sleeps.** `wait_ms` is 3000 for Claude, 7000 for Codex,
   5000 for OpenCode — slept unconditionally before the extractor runs, even on
   a page that was ready immediately. The extractor already has a retry protocol
   (`__retry_after_ms`); the fixed wait is dead time.

**A failing Claude cycle can outlast the refresh interval.** Claude runs
`timeout_ms=40000` × `transport_max_attempts=2` = 80s per scraper, and
`build_max_attempts=2` allows two scrapers, so a single account can take 160s
to give up. Two Claude accounts serially is ~5.3 minutes against a 5-minute
active interval. Nothing overlaps — `refresh_now` returns early while a cycle
is in flight — but a persistently failing Claude effectively occupies the
schedule. The budget was raised in PR #11 for a real reason (the page needs it)
and the fast retry from PR #13 partly offsets it, so this is recorded rather
than tuned blind.

`start_at_login` also defaults to `False` (`config.py`). Turning it on makes a
cold start a once-per-boot event rather than something you meet every time you
open the app, which is the cheapest win of the four.

---

## 6. Decisions taken, so they are not relitigated

- **OpenTelemetry was considered and declined.** It collects spans and metrics,
  not response bodies, so it does not address the capture problem; and adding an
  SDK would require rewriting this fork's audited "no telemetry or backend
  service" claim, which appears in `SECURITY.md`, `README.md`,
  `SECURITY-AUDIT.md` and the datasheet. If the goal is ever *exporting* gauge
  values to a dashboard, that is a separate, opt-in feature — an output channel,
  not an input strategy.
- **Chrome DevTools Protocol was considered and declined** for the capture. It
  is the standard way to read response bodies and would not modify the page, but
  it requires opening a localhost debugging port attached to a browser context
  holding live Claude and ChatGPT sessions. For an app whose value rests on
  holding provider credentials safely, that trade is bad.
- **The legacy `/new#settings/usage` route candidate was removed, not kept as a
  fallback.** It is known not to open the dialog, and while the settings page
  was still loading it fired and navigated away from the page that was about to
  succeed. Being on the right route and unhydrated is a reason to wait.

---

## 7. The meter catalog — what `1.1.0+cfa.3` changed, and what it does not

**The problem it addresses.** Both extractors matched hardcoded English labels,
so a relabel broke the read and a new meter never appeared at all. Claude
changed that surface three times in one week (§1.1). The labels now live in
`src/aigauge/providers/meter_catalog/{claude,codex}.json`, overlaid by
`app_data_dir()/meter_catalog/<kind>.json`, and every catalog meter becomes its
own field. Only Session and Weekly stay untagged, so the tray colour is
unchanged. Format documented in README.md.

**What it is not.** It does not retire the polarity and attribution heuristics
— §3's API mapper is still the durable fix, and the DOM path is still what the
gauge reads. What the catalog changes is the *cost* of a relabel: an alias
added to a JSON file instead of a code change and a release.

**Design decisions worth not relitigating:**

- **The primary path is untouched.** `readRow('Current session')` and
  `readRow('All models') || readRow('Weekly')` still run first and still seed
  the catalog scan.
- **An adopted row can cost Session/Weekly their number, and must never give
  them the wrong one.** `ROW_LABELS` is the *rival* set — `readRowText` takes
  the LAST percentage in the container it picked unless a rival label is in
  there too — and this went round twice. Built from every alias, an adopted
  fragment like "Current" or "Opus" made the primary Session row `ambiguous`
  on every refresh; narrowed to bundled specs, an adopted "Cowork sessions"
  sharing a collapsed container with Session made Session report *7% used* as
  an OK snapshot. The second is worse: a refusal is visible and recoverable, a
  plausible number pointing at the wrong meter is neither. So the rival set is
  every spec's aliases again — bundled, discovered and hand-added, enabled or
  not, because the page renders them all — and the fragments are refused at
  *adoption* instead, by `_collides_with_known`, which is the one place that
  can decide it before the entry exists. Display labels stay out of the rival
  set: "Session" is a fragment of "Current session".
- **The usage container is found from a marker that has a number beside it,
  and the richest candidate wins.** A bare mention of the marker phrase is a
  nav item, a heading or prose about limits, and climbing from one let a
  settings nav that also renders "Storage 88%" win on size — it is smaller
  than the panel. Size was the wrong tiebreak in general: furniture that
  carries the marker *and* a percentage ("Plan usage 12% off Max" beside
  "Storage 88% used", a sidebar of chat titles quoting the meter names) is a
  legitimate anchor and still shorter than the panel, so it won and the panel
  was never scanned. Candidates are scored by how much of a usage panel they
  hold — the catalog meters whose wording they render plus the leaf rows whose
  percentage says used or remaining — and length only settles ties. A
  candidate is still refused when it holds a bare marker off the path from its
  anchor, but only once the climb has passed something panel-shaped (an
  ancestor above the anchor with a percentage and no stray of its own): that
  is the evidence the climb left the panel, and it is what stops a panel with
  a single meter promoting the SPA root wrapper (which is not `<body>`, so the
  outright refusal never saw it). Without that qualification the rule refused
  the panel's own heading, period tab strip and footnote — marker wording
  inside the panel, on no path up from a row — so `usageContainer()` answered
  null and discovery was inert on that layout, once a day, forever. The
  "swallowed the page" ratio counts leaf rows only; counting every wrapper
  around a row counted the row once per nesting level.
- **The overlap rule refuses fragments, not vocabulary.** Claude names its
  meters out of a handful of words, so refusing any candidate containing a
  known label refused "Weekly Opus" and "Cowork session" too. Refused now: the
  same wording, a whole-word fragment of a known label, and a known label with
  a count glued on. The accepted cost is that a *relabel* ("Session limit") is
  adopted as an informational meter beside the unreadable primary rather than
  refused; it gets its own history key, and adding the wording to the primary's
  aliases is still the fix.
- **A file we could not read is not a file we may replace.** A failed read
  (`OSError`) and a failed parse (`ValueError`) are different answers, and only
  the second says the contents are worthless — collapsing them had a Windows
  sharing violation quarantine a valid override and replace it. A quarantine
  that fails aborts the write, and a second corruption keeps the *first*
  `.corrupt`: that copy holds the user's own edits, and everything written
  after it was written by the app.
- **A scan that found no panel is stamped short, not left due.** The diagnosis
  is right and the cadence was not: the same line every refresh, forever. It
  re-attempts daily (`CATALOG_NO_CONTAINER_RETRY`). A payload that never
  reached the scan still leaves it due, so a page that failed to render cannot
  spend a scan the user armed by hand.
- **A meter is adopted with no window.** Same reasoning as `polarity` below:
  inferring a period from the wording is a guess, and a wrong window makes an
  active meter read "idle" instead of showing its number. `infer_window` is
  gone rather than unused.
- **A scan is stamped only from a page that produced an OK snapshot and that
  actually had a usage container**, once per refresh. Stamping on any payload
  carrying a `discovered` list meant a logged-out or half-rendered page adopted
  its furniture permanently and burned the week's scan — including a scan the
  user had just armed with "Re-scan meters now" — and stamped twice per
  refresh, because `ScrapeRunner` rebuilds the snapshot after a transient
  error. The extractor returns `discovered: null` when it never found a
  container, so "found nothing" and "could not look" are distinguishable in
  Python and in the log (`classification=discovery_no_container`).
- **Discovery is local and adopts nothing that could matter.** It reads rows
  the page already rendered, adopts only inside the recognised usage container,
  and never sets `primary` — a row this build has never seen cannot take over
  the tray colour. No remote catalog was considered; a downloaded catalog is a
  new egress channel and a new trust boundary in an app whose whole claim is
  that it has neither.
- **`label` is deliberately not the page's wording**, because it is the history
  key. A relabel must not fork `provider::label`.
- **`polarity` exists but ships unset.** Without wording beside the number the
  reading is refused, and the hint is an escape hatch for a user who knows what
  their page renders — not a default.
- **`status` is parsed and honoured but nothing writes anything but `active`.**
  It is the hook for routing discovered rows through a review dialog before they
  take effect; unknown fields on an override entry are preserved for the same
  reason.

**Open, and deliberately not done here:**

- **Adoption is unreviewed.** A row that passes the junk rules becomes a field
  on the next refresh with no user confirmation. The provenance fields
  (`source`, `first_seen`, `account_id`, `evidence`) exist so a review step can
  be added without re-scanning; that step is a separate change.
- **Codex discovery needs two percentages in one element** to identify the
  usage panel, so a page rendering a single card discovers nothing. Harmless
  today (one card means nothing new to find) but it is the reason a first
  extra card can take an extra scan to appear.
- **Only one usage panel is scanned.** `usageContainer()` answers with a single
  element, so a page rendering two panels — a personal one and a team one,
  each with its own meters — has the richer one scanned and the other's meters
  outside the container, where nothing is adopted from them. Pinned as a
  known-limitation test (`test_only_one_of_two_usage_panels_is_scanned`),
  because the failure is a meter that never appears rather than a team
  percentage under a personal label. Scanning both means returning a list of
  containers and merging the scans, which is a change to every caller of the
  discovery payload.
- **The "passed the panel" test is a percentage, not a second anchor.**
  `passedPanel` is set by the first ancestor above the anchor that holds a
  percentage and no bare marker, and a card's own wrapper qualifies. So a panel
  whose cards each sit in a wrapper of their own *and* whose heading is a
  `div`/`section`/`li` reading "Plan usage" is refused as though the climb had
  left it, and discovery is inert on that layout (`discovery_no_container`
  once a day). The mirror case: a single-meter panel whose heading is that bare
  marker never sets `passedPanel`, so the SPA root above it is scored instead
  of refused, and only the page-swallowing ratio stands between its furniture
  and `in_container`. Both are pre-existing shapes of the stray rule narrowed,
  not widened, by the `passedPanel` qualification; neither touches the primary
  reads; and both want a live page before choosing the rule — an ancestor
  bearing a second *anchor* would separate a wrapper from a panel, at the cost
  of never recognising a single-meter panel at all.
- **Nothing has been observed against a live page.** The catalog reproduces the
  labels the extractors already carried, and the discovery scan is exercised
  against reconstructed DOMs in node — the same evidence basis, and the same
  limitation, as §1.1 describes for the row readers.
- **Nothing retires an adopted meter.** A row the page stops rendering keeps
  its entry, and the 24-meter cap is a ratchet: once it is reached, a genuinely
  new meter can never be adopted, because nothing below it is ever released.
  Retiring an entry not seen in N consecutive scans would fix both, and it
  belongs with the review dialog — a meter the user has approved must not be
  retired behind their back.
- **The junk blocklist is a substring list.** `_NON_METER_MARKERS` matches
  anywhere in the normalized label, so a real meter named after one of those
  words is refused ("Account credits" dies on "account"), while furniture
  worded differently gets through. And `_LABEL_RE` is ASCII-only, so a
  localised page adopts nothing at all. Both want the review dialog before
  they want loosening: a refusal is invisible today, and that is what makes
  either one hard to judge.
- **A future bundled key could collide with an adopted one.** Adopted keys are
  derived from the page label (`daily_agent_runs`), and nothing reserves them.
  If a later release ships a bundled meter under a key some user's file already
  holds, `load_catalog` merges the override *onto* the bundled entry and the
  adopted label wins. `_drop_superseded` handles the common shape of this (an
  adopted meter whose aliases the bundled catalog has since learned is dropped
  at load), but not a key collision with different wording. Namespacing
  adopted keys is the fix, and it is a file-format change.

## 8. Parked from the Microsoft/Azure work

Added 2026-09-09 with the Azure month-to-date spend tile (`1.2.0+cfa.4`).

### 8.1 Unverified against a live Azure account — run the probe first

The provider was written against the REST specification
(`Azure/azure-rest-api-specs`) and current Microsoft documentation, not against
a real subscription. Everything below is a *shape* the docs support but a live
account has not confirmed. One command settles all of it:

```bash
python -m aigauge.providers.azure --probe
```

| What | Why it might differ | What the code does |
| --- | --- | --- |
| Cost metric name — `Cost` vs `PreTaxCost` | MCA and EA/pay-as-you-go disagree, and every example in the REST spec uses `PreTaxCost` while the automation docs use `Cost`. | Tries `Cost`, retries once on a 400 with `PreTaxCost`, then remembers which worked. The response column is found by name-candidate list, then by a numeric column whose *name* contains "cost" — never by position. A response with no cost column is an error, not a month with no spend. |
| `ClientType: ai-gauge` request header | Documented in Q&A and SDK behaviour (it maps to the SDK's ApplicationID), not in the REST reference. If it is ignored we share the anonymous rate-limit bucket — a throughput question, not a correctness one. | Sent on every ARM request. |
| `ResourceGroupName` as the filter dimension | The optional resource-group filter uses this name; the docs show `ResourceGroup` in some places. | Only used when the user sets the filter; leaving it blank avoids the question. |
| Whether Marketplace model charges really carry a distinct `ResourceId` | The design assumes they do, and buckets them by id like Foundry. If they share a resource id with something else the row would absorb it. | Off by default; the toggle is the opt-in. |
| Whether a Foundry resource's cost rows carry the *account* id rather than a project child id | Projects are `accounts/projects` children and are documented as billing to the parent. If cost rows name the child, the roll-up under-reports. | The probe prints the resource ids found and the bucket totals, so a mismatch shows up as a Foundry row that is smaller than expected. The Settings field accepts one child `type/name` segment, so a project id can be pinned by hand if the probe shows that shape. |

`api-version`s were all confirmed present in the spec repo: Cost Management
`2025-03-01`, Consumption budgets `2024-08-01`, Cognitive Services `2024-10-01`,
subscriptions `2022-12-01`. Newer stable versions exist for the first three
(`2026-06-01`, `2026-06-01`, `2026-07-01`) and were **not** adopted — there is
no feature here that needs them, and an unnecessary version bump is an
unnecessary source of behaviour change.

### 8.2 Deliberately not built

- **"Pin Foundry to the compact chip."** Offered as optional in the brief and
  skipped. The compact chip shows one number with one colour, and the Foundry
  row's percentage is a *share of spend*, not usage against a limit — the same
  category error that `gauge.provider_max_percent` exists to prevent. Doing it
  properly means deciding what the chip's colour should mean for a share, which
  is a design question, not a wiring one. The plumbing (`COMPACT_DISPLAY_NAMES`
  in `widget.py`) is where it would go.
- **Multi-subscription support.** One subscription per install. Two would need
  either two tiles (and a second throttle budget against a tenant-wide rate
  limit) or a roll-up with a currency-mismatch problem, since subscriptions can
  bill in different currencies. Neither is a small change, and the owner has one
  subscription.
- **A Vercel provider.** Named in the brief as a future provider; nothing was
  started. Worth noting that its shape is closer to OpenRouter's (an API key and
  a spend number) than to Azure's.
- **Per-provider refresh throttling in `app.py`.** §4 records that the error
  fast-retry is cycle-wide, and Azure would have been the provider most hurt by
  it. Rather than fix the shared scheduler, the Azure provider defends itself
  with its own hourly floor. That is the right defence for a tenant-shared rate
  limit either way — a scheduler fix would not remove the need for it — but it
  does mean §4's entry is still open and one more provider now works around it
  rather than through it.

### 8.3 Known soft spots in what was built

- **Throttle state is module-level and keyed by subscription id.** That is what
  makes it survive `App._build_providers()` on every settings save, which is the
  whole point. It also means it is process-global: two `AzureProvider` instances
  for the same subscription share one budget (correct), and the state is not
  written to disk, so a restart is a fresh hour (acceptable — a restart is a
  human action, not a loop; SECURITY.md now says "per app run" rather than
  implying the floor survives a restart).
- **`data as of` is derived, not reported.** Cost Management does not return a
  freshness timestamp, so the tile uses the latest `UsageDate` that carries
  non-zero cost, ignoring any date later than tomorrow. A genuinely zero-cost
  day inside the period reads as "no data yet" for that day. There is no better
  signal available.
- **The forecast row trusts Cost Management's own projection** rather than
  extrapolating locally. When it is unavailable — or when it comes back at or
  below the spend already recorded, which is not a projection — the row is
  simply absent, which is honest but means it silently comes and goes early in
  a period.
- **A refresh has a request budget, and it only guards the loops.** A
  wall-clock deadline (`REFRESH_DEADLINE_SECONDS`) and a request ceiling
  (`MAX_ARM_REQUESTS_PER_REFRESH`) cover the cost-query `nextLink` chain and
  the discovery page loop, because those are what multiply. The fixed handful
  around them — token, subscription, budgets, forecast — is outside it: they
  cannot repeat, and counting them would make the ceiling harder to reason
  about.
- **A subscription that exceeds the page cap every hour stays ungauged.** A
  truncated aggregate is cached like a good one, so the hour that follows is
  "no gauge, with a note" rather than an hour of error backoff — the right
  trade, but permanent for such a subscription. A monthly-granularity retry
  (losing only `data_as_of`) would produce a complete total and was not built.
- **An unmatched Marketplace pair contributes nothing and is not noted.** If
  the marketplace query returns a (resource, service) pair the main query does
  not, its cost does not appear anywhere and no note says so. The alternative —
  adding it — would break the invariant that the buckets sum to the total.
  (A marketplace query that was *cut short* is different and is handled: the
  split is discarded for that refresh and the note says so.)
- **A failed discovery is cached like a successful one.** The offer read and
  the Foundry account list are both taken under one `DISCOVERY_TTL` stamp that
  is written whether or not they succeeded, so a 403 or a 500 on the offer
  read costs the gauge for 24 hours, not for one refresh — the note says
  Reader is what the check needs, but nothing retries it sooner. Stamping only
  a successful read, or retrying discovery at the next window while keeping
  the rest cached, is the fix and was not made here.
- ~~**The App-level in-flight scheduler has no watchdog.**~~ **Fixed in
  1.3.0+cfa.5.** Every dispatch carries an epoch and arms a single-shot timer
  for that provider's own budget plus slack — the browser providers derive it
  from the scraper timeout times the attempts they may make, `AzureProvider`
  reports `REFRESH_WORST_CASE_SECONDS` (its page-loop deadline *plus* the
  fixed calls outside that budget, because `REFRESH_DEADLINE_SECONDS` is a
  floor on its real ceiling, not the ceiling), and a plain REST provider gets
  a flat 60 s. A REST dispatch also gets an explicit allowance for time spent
  queued on the shared `QThreadPool`, since its budget starts running at
  dispatch while its work starts when a pool thread frees up.

  On expiry the App logs it, synthesises an ERROR snapshot for that provider
  and the queue carries on. **The watchdog ends the App's wait, not the
  provider's work**, so the name is then *parked*: no cycle, retry wake,
  manual refresh or settings save dispatches it until its worker reports back
  or twice its budget has passed and the worker can be assumed dead. A
  snapshot that arrives from a dispatch the App gave up on is matched by
  epoch and closes no cycle: it joins no cycle's verdict, clears no
  `_inflight` entry and destroys no watchdog. It *does* repaint **and record**
  its own tile when it is the newest dispatch's answer — otherwise a provider
  that is merely slower than its budget shows "Refresh timed out." forever
  while answering correctly every time, a genuine AUTH_REQUIRED is never
  painted, and (before the recording half) the tile looked healthy above an
  empty ratio history and a blank burn-rate row, because the only thing
  either store had heard about that dispatch was the watchdog's synthetic
  ERROR and both drop anything that is not OK. Recording it is not a
  scheduling side effect: the answer still clears that provider's retry entry
  only when it was OK or AUTH_REQUIRED, and touches no cycle, no `_inflight`
  entry and no watchdog. An older epoch, or a provider the user removed, is
  still dropped whole. The three browser providers refuse a re-entrant
  refresh outright, keyed by **account id** in a module-level registry in
  `providers/_scrape_runner.py` rather than on the provider object, because
  `_build_providers()` replaces that object on every settings save; a
  provider object is also reused when its key and class are unchanged. So
  the one case the assumed-dead ceiling lets through cannot open a second
  `QWebEngineView` on one profile either.

  **A registry entry expires.** It is cleared when the scrape reports back,
  and `HeadlessScraper` arms its own timeout so it always does — but that is
  an invariant of another module, and `_finish` reads `self._page` twice
  before it emits. A page whose C++ half Qt has already deleted (what
  destroying a profile under a live scrape produces) made both reads raise
  after `_finished` was set, so the emit never happened and the account was
  refused for the life of the process, with nothing able to clear it —
  module state is not clearable by a settings save, which is the point of it.
  The diagnostics in `_finish` now give way to the signal, and an entry older
  than the scrape's own worst case (the runner's timeout times the attempts
  it may make, plus a minute) expires with `live scrape guard expired
  account=… age_s=…` in the log. Inside that budget a genuinely live scrape
  is still refused.

  A retry deadline that comes due while its provider is parked is not spent:
  it is kept, and owed no earlier than the next ordinary cadence wake.
  Re-arming it for the instant the park lifts bought a wake of its own, worth
  one extra dispatch an hour for a hung provider (measured 4→5 for a browser
  one, 6→7 for a REST one) and, for the three REST providers, one more worker
  permanently holding a slot of the global thread pool.

  **No entry path opens a cycle with nothing to run.** `_begin_cycle` filters
  out what it cannot dispatch and then returns rather than opening a cycle
  over zero providers — which logged a start and an end for a cycle that
  dispatched nobody and, on a *manual* refresh, re-armed the thirty-minute
  active window and zeroed `unchanged_cycles`, because that re-arm sits after
  the filter and never asked whether anything had survived it.

  A removed account's on-disk profile is deleted by the App rather than by
  the settings dialog, and only once no dispatch of that account is
  outstanding: `purge_profile` releases the cached `QWebEngineProfile` and
  rmtree's its directory, and Qt requires a profile to outlive its pages. The
  stored credential is still cleared by the dialog, immediately. What is
  still owed is recorded in `config.pending_profile_purges` and drained at
  the next start, before any cookie is hydrated and before any provider
  exists, so a quit inside the deferral window no longer leaves a removed
  account's `ForcePersistentCookies` store on disk forever. The drain checks
  membership as well as ordering — an id that is *also* a configured account
  is skipped, logged `purge skipped … reason=reconfigured` and dropped from
  the list — and its opening line names a count with a bounded sample of ids
  rather than the list itself.

  The heartbeat restarts a timer that is not running while nothing is in
  flight, and ends a cycle that is open with nothing in flight, nothing
  queued and no watchdog left.
- **`BrowserAccount.enabled` is parsed and ignored, on purpose.** F13 made
  `_enabled_providers` and `_build_providers` honour it. That was reverted
  before merge: nothing in the app ever *writes* the field except the config
  migration, which stamps `bool(providers.<kind>)` when it inserts a missing
  fixed account - so a config migrated while `providers.claude` was false
  carries `enabled: false` forever, and the Settings checkbox, which flips
  `providers.claude`, could never undo it. Honouring a field nothing writes
  turns that checkbox into a permanent no-op. The field stays in the model so
  an existing `config.json` still loads and a future real per-account toggle
  has somewhere to land; `browser_accounts(..., enabled_only=True)` still
  exists and has no caller in `src/`. Making the writer match the reader -
  syncing the fixed accounts in `SettingsDialog.apply_to` plus a one-time
  repair in `Config._migrate` - is the other half, and belongs with whatever
  actually needs a per-account switch.

- **The per-provider retry does not cover Azure's remembered error.** Inside
  its hourly window Azure re-serves `state.last_error` on every refresh. That
  snapshot is a real failure and carries no `error_class`, so it still earns
  three fast retries — each of which only re-serves the same remembered error,
  at no network cost. Bounded and cheap, so it was left alone; marking a
  *re-served* error would mean mutating the stored snapshot.
- **A partial (retry) cycle does not advance `_unchanged_cycles`.** It cannot:
  a cycle that polled one provider says nothing about whether the app is idle.
  The consequence is that a long run of retry cycles neither advances nor
  resets the idle backoff, so the backoff is decided entirely by full cycles.
  The guard is deliberately **asymmetric**: a partial cycle cannot advance the
  backoff, but a partial cycle whose one tile *changed* still zeroes
  `_unchanged_cycles` and re-arms the 30-minute active window for the whole
  app. A provider that flaps ERROR→OK on its retry cadence — OpenRouter had 22
  such errors in 4.5 days — therefore holds the app in active mode. Symmetry
  would be worse: a real change is a real change, whoever noticed it.
- **A cycle accounts only for what it dispatched.** `_cycle_names` is set at
  `_begin_cycle`; a snapshot from a provider outside it repaints its tile,
  history and ratio but does not join the cycle's progress, its `errors=`
  line or its `changed` verdict.
- **"Three retries then the normal cadence" bounds a run of errors, not a
  provider.** Any non-ERROR status pops the `_error_retry` entry, so a
  provider alternating AUTH_REQUIRED and ERROR — OpenCode's exact pattern in
  the desktop log, 95 auth failures and 38 errors and never a success —
  refills the 1/2/4-minute ladder every time. Measured, that is about one
  extra dispatch per cycle for that provider while the healthy ones fall from
  17 an hour to 10, so it is not an amplification; it is just not the bound
  the sentence sounds like.
- **The legacy `browser_accounts == []` fallback has no `_build_providers`
  counterpart.** `_enabled_providers` returns `claude, codex, copilot`;
  `_build_providers` creates copilot alone, so the tray and the menu-bar item
  would iterate two names that can never have a snapshot. Unreachable through
  `Config.load()` — the migration always re-inserts both fixed accounts — so
  the fallback was left alone and its test renamed to claim only what it
  checks.
- **Only the browser providers refuse a re-entrant refresh; Copilot and
  OpenRouter do not, and that is now a thread-pool question.** The App parks a
  provider whose dispatch its watchdog abandoned, but the assumed-dead ceiling
  has to let it go eventually, and for the browser providers the account-keyed
  live-scrape registry then catches it. Azure is covered by its own
  `state.in_flight` gate. Copilot and OpenRouter have neither, and `requests`'
  `timeout` is per socket operation rather than a total, so a server that
  sends one byte every 14 s against a 15 s timeout holds a worker forever.

  **The bound is the pool, not the request rate.** Copilot, OpenRouter and
  Azure all submit to `QThreadPool.globalInstance()`, so the live socket count
  can never exceed `maxThreadCount`; measured over six fake hours against a
  byte-dripping server, every pool slot ends up stuck (1 of 1, 2 of 2, 4 of 4,
  8 of 8) with an unbounded FIFO of queued runnables growing about four or
  five objects an hour. The request *rate* falls rather than amplifies -
  queued runnables never get a thread, so nothing is sent.

  **The consequence is availability.** All three REST tiles are dead for the
  life of the process, with no recovery path; before the watchdog work the
  same server produced one stuck worker and a stalled app, so this converts a
  one-slot leak into an all-slots leak plus a growing queue. Measured in the
  six-hour fuzz with a double that never answers and never refuses: browser
  providers went from 19 concurrent to **1**, the REST ones to 15 in that
  model, and 9 live workers against the dripping server.

  **Fix options, for the maintainer to pick.** (a) A total-response deadline
  on the REST side - `stream=True` plus an elapsed check while reading - which
  is the only one that actually bounds the socket. (b) A dedicated
  `QThreadPool` per provider, so one wedged provider cannot starve the other
  two; cheapest containment, and it does not free the wedged worker. (c) Keep
  the assumed-dead ceiling for browser providers only, and require a REST
  worker to report back before its park is released. (d) A busy flag on the
  provider - the smallest change, but it means writing a provider attribute
  from a pool thread, which is the one thing this scheduler currently never
  does: every provider `on_done` only emits a queued signal. Any of these
  deserves its own change rather than a tail-end addition here.

  **Every dispatch of a hung REST provider costs a slot, which is why the
  kept retry is folded into the cadence.** An earlier draft of the retry
  deferral re-armed a parked provider's due for the instant the park lifts;
  measured against the round-2 tree with the same seeds that was 4→5 and 6→7
  dispatches an hour and 15→17 worst concurrent REST workers in the 60-seed
  six-hour fuzz. Owing the due no earlier than the next cadence wake puts all
  three numbers back (4, 6, 15/14/15). It does not contain the leak; it just
  stops this feature widening it.
- **On a one-core host every REST watchdog is about six minutes.**
  `_pool_wait_slack` adds `sum(every other REST budget) / maxThreadCount` to a
  dispatch's watchdog, because the cycle hands openrouter, copilot and azure
  to the shared pool in one burst and a budget that starts at dispatch would
  otherwise fire inside a refresh that has not exceeded its own bound. With
  the real numbers (openrouter 60, copilot 60, azure 225) that is 365 s at
  capacity 1, 222 s at 2 and 151 s at 4 for the two REST providers, and
  365/305/275 s for Azure - so on a single-core machine a REST watchdog is
  six minutes (up from 80 s) and the park ceiling twelve. While that cycle is
  open the scheduler timer is stopped, so one wedged REST provider blocks
  every refresh for six minutes rather than eighty seconds. The model is also
  pessimistic by construction: it assumes full serialisation of every other
  budget even at capacity 3, where the real wait is zero. It is still the
  right trade - a watchdog that fires early manufactures the failure it exists
  to catch, and feeds the parking machinery - but the alternative is to scale
  the allowance by pool size (or to have providers report when their work
  actually starts, which is a Provider-API change: the API is one callback).
- **"Clear all browser data" still purges a profile the App may be scraping.**
  Profile deletion moved out of the settings dialog for the *removal* path,
  because only the App knows whether a scrape of that account is still holding
  the directory. `settings_dialog._clear_all_browser_data` still calls
  `purge_profile` synchronously for every configured account, every account on
  disk and the three fixed ids - including one whose scrape is live. That is
  the same `deleteLater()`-then-`rmtree` under a live `QuietWebEnginePage`
  that `_run_profile_purges` exists to prevent, and it is the most reachable
  way to produce the destroyed page that used to strand the live-scrape guard
  (that half is fixed: the guard expires, and `_finish` no longer loses its
  emit to a diagnostic). Left as is because it is an explicit, confirmed user
  action behind a warning dialog, unlike a settings save; the fix is to emit
  the id list to the App the way `removed_profile_ids` now does and let
  `_purge_removed_profiles` defer it, and/or to have both purge paths ask
  `account_is_busy()` - the signal now exists and neither caller consults it.
- **A deferred purge makes the app write `config.json` on its own.**
  `_run_profile_purges` records what is still owed, and it is called from
  `App.__init__` and from the five-minute heartbeat - so while a purge is
  deferred the app rewrites the user's settings file without the user asking,
  which nothing else in it does. It is well guarded: `_persist_pending_profile_purges`
  early-returns when the list is unchanged, so the steady state (an empty
  list) writes nothing and a normal start writes nothing. What a write costs
  is that `Config.save()` serialises the whole model, so a key an older or
  newer build wrote that this one does not model is dropped, and a concurrent
  hand-edit is overwritten. Worth knowing before adding a second such writer;
  not worth a mechanism on its own. (It also means an ad-hoc harness that
  drives `_run_profile_purges` must set `APPDATA` - an override on every OS,
  which `tests/conftest.py` sets for the suite - or it edits the developer's
  real config.)
- **The log summariser's shared budget does not cover `repr()` values or
  large numbers, and `_raw_summary` catches only `TypeError`.** The budget is
  charged for strings, for dict keys and for elided nodes, but the numeric
  branch charges a flat eight characters whatever the magnitude and the
  `repr()` fallback is charged after the fact and never clipped: a 5 MB
  `bytes` value still produces a record 9.5x the rotation, and fifty
  4 200-digit JSON integers produce 210 KB. Separately, three inputs make the
  walk raise something `except TypeError` does not catch - an object whose
  `__repr__` raises, a dict key whose `__str__` raises, and a `dict` subclass
  whose `items()` raises - and that escapes into `_on_snapshot`. None of it is
  reachable today: browser payloads arrive through the QtWebEngine JS bridge
  (no bytes, no integers), `json.loads` itself refuses a number of more than
  4 300 digits, and the two REST providers that keep a verbatim server dict do
  so only on an OK snapshot while `raw_summary=` prints only on
  ERROR/AUTH_REQUIRED. It would bite the first time an extractor or a provider
  returns something that is not plain JSON. Fix:
  `budget[0] -= max(8, len(str(value)))` in the numeric branch, clip the
  `repr()` to `_LOG_VALUE_LIMIT` the way the string branch already does, and
  widen the `except` - a log line must never be able to raise.
- **The dispatch epoch is matched against the name the payload carries.**
  `_dispatch`'s `_emit` forwards the provider's own `snapshot.provider` and
  pairs it with the dispatch's epoch; every gate downstream keys on that name.
  Epochs advance in lockstep across a cycle, so a snapshot mislabelled with a
  *sibling account's* id is accepted as that sibling's live answer - clearing
  its `_inflight` entry, destroying its watchdog, joining the cycle's verdict
  and painting its tile with another account's numbers; the late path now has
  the same reach. Unreachable today, and checked rather than assumed:
  `ScrapeRunner` sets `provider=self._account_id`, the browser builders take
  `account_id=` from the App, and the three REST providers hardcode their
  literal, so no extractor's output reaches the field. The fix belongs in one
  place - `replace(snap, provider=_name)` in `_emit`, so the App's own notion
  of what it dispatched is the only thing that can decide which tile is
  touched.
- **The resume-artifact threshold is still unreachable on an awake machine,
  and two `scraper.py` log lines still carry uncapped page text.** Both
  pre-date this work; `webview/scraper.py` is touched by it only in `_finish`,
  where the diagnostics now give way to the `done` signal. The threshold: the scraper calls a timeout a resume artifact past
  `timeout_ms x max_attempts x RESUME_ARTIFACT_FACTOR`, which for Claude is
  40 x 2 x 3 = 240 s, while the App's watchdog for the same provider is
  160 + 20 s - so the watchdog always wins and the classification only ever
  fires across a real machine suspend, which is what it was written for.
  `self._started_at` is also set once in `__init__` and not reset in
  `_begin_attempt`. The log lines: `title=%r` and `result_keys=%s` print
  `document.title` and the extractor's key names with no length cap, the
  healthy one at INFO - worst case measured at 1.5 MB for a single record,
  2.9x the 512 KiB rotation. The fix is the same one-line clip applied at
  three call sites (`self._page.title()[:200]`, `sorted(result)[:50]` with
  clipped names). Suppressive or diagnostic only, with no request-rate
  consequence, so both are left for a scraper-scoped change.
- **`CopilotProvider` and `OpenRouterProvider` do not declare
  `refresh_budget_seconds`.** Both take the flat 60 s default. Copilot's real
  nominal ceiling is 10 + 15 + 15 s of `requests` timeouts, each per socket
  operation rather than total, so the flat number happens to be about right;
  making it explicit would keep them inside the "read the budget off the
  provider" doctrine and was not done here.
- **The tile and tray tooltips rely on `snapshot.error` being clean at
  source.** `_exception_summary` is what keeps a request URL out of it;
  `widget.py` renders the string as-is, and only `app.py`'s log lines and the
  error dialog redact. A future provider that puts an id in `snapshot.error`
  would put it in a tooltip.
- **History still keys on the display label.** The Azure summary label is
  stable now, so Azure closes periods correctly, but `history._state_key` is
  still `provider::label` and OpenRouter's `Today ($x/$y)` and Copilot's
  `Credits (12.5/1500)` have the original shape. A stable `key` field on
  `UsageMetric`, used by `history` and `ratio`, is the repo-wide fix and was
  out of scope here.
  **Still parked after 1.3.0+cfa.5**, deliberately: the cadence signature
  (`app._snapshot_signature`) hashes `metric.label` for the same reason, so
  OpenRouter's and Copilot's money-bearing labels can still count as "this
  provider changed" and hold the app in its active window. Dropping
  `snapshot.error` from the signature removed the worst leak (Azure's minute
  countdown) with one clamp; the label half is the same repo-wide `key`
  decision and does not belong bundled with a cadence fix.

- **One CI job segfaulted in a native thread, once, and the cause is not
  pinned.** Run 67 on `aab1de9` died with `Fatal Python error: Segmentation
  fault` in the Ubuntu 22.04 / 3.11 job while the other five jobs and every
  local run passed. faulthandler showed the main thread parked in a
  `responses`-mocked request inside `test_the_query_page_loop_is_capped` -
  pure Python - and labelled it `Thread`, not `Current thread`, so the fault
  was in a thread with no Python state: a Qt or Chromium thread left running
  by earlier files (`test_app.py` constructs a real `App()`), or the
  `Release of profile requested but WebEnginePage still not deleted` hazard
  the suite prints at exit. Commits `13f7aec` and `2c2b0e2` removed the one
  thing this PR had added of that class - three tests setting `TZ` and
  calling `time.tzset()` under those threads - and their messages name it as
  the cause. That was overstated: those tests sit at the end of the Azure
  file and had not run when the crash landed. The removal stands as hygiene;
  the segfault is a suite-level soft spot that predates this PR's code and
  belongs with the WebEngine teardown warning, not with Azure.

  **The named cause is now fixed** (`c759bdc`). The real `App()` that
  `test_app.py` constructs was never torn down, and its startup refresh was a
  bare `QTimer.singleShot(500, lambda: ...)` whose lambda kept the App alive:
  500 ms later, inside whatever test was then processing events, a real cycle
  dispatched real providers and started a real scrape. That stray page was the
  `Release of profile requested but WebEnginePage still not deleted` warning,
  and it was also the intermittent
  `TypeError: 'NoneType' object is not callable` that pytest-qt pinned on a
  long-running test in `test_app_logging.py`. The startup refresh is now a
  parented single-shot `QTimer`, `App.shutdown()` stops it along with the
  cadence timer, the heartbeat and every armed watchdog, and the smoke test
  calls it in a `finally`.

  What that closes and what it does not: the teardown warning and the
  misattributed `TypeError` are gone, and the suite no longer leaves a live
  Chromium page running under later tests. The run 67 segfault itself was
  never reproduced, so this removes its most likely cause rather than proving
  it was the cause. If a native-thread crash reappears with the stray page
  gone, this entry is no longer the explanation and the hunt starts fresh.

### 8.4 Decisions taken in review, so they are not relitigated

Three reviews went over this feature before it merged (see the
"Hardening from review" block in `CHANGELOG.md` for the findings). These are
the calls that were made, and why:

- **The throttle fails closed.** Inside the fetch window with nothing cached
  and nothing remembered, `refresh()` returns an error snapshot naming the
  wait. It never falls through to a live fetch. Every fetch outcome — including
  an exception no one anticipated — is recorded in `_State`, because a blank
  state is what switched the throttle off. If a future change needs a "fetch
  now" escape hatch, it must clear `last_fetch_at` explicitly rather than rely
  on the gate letting anything through.
- **The identity check is two checks.** `_identity` is the credential triple
  (tenant, client, secret digest); changing it resets the whole `_State` and
  the token cache, because everything cached describes a different app
  registration. `_query_identity` is what the request asks for (reset day,
  resource group, marketplace toggle, pinned Foundry ids); changing it marks
  the cached aggregate `stale_settings` and keeps `last_fetch_at`,
  `blocked_until` and `consecutive_errors`. A settings save is a one-click
  human action that is easy to loop, and "at most one live fetch an hour" is a
  promise made to the tenant, not to this tile — so no fetch follows the save.
  What the tile shows until the window opens is the old figures with no
  percentage on any row and a note naming the time of the next fetch: those
  amounts are still the last real reading of this subscription, and dropping
  them left the tile in error for up to an hour with nothing saying why. Only
  a fetch that succeeds under the new settings clears the flag.
- **The row count and the allowance are display settings, in neither
  identity.** `build_snapshot` re-reads both on every render, so editing
  either re-renders from the cached aggregate with no API call. That is why
  the aggregate keeps every distinct bucket (`MAX_KEPT_BUCKETS`, with the tail
  pre-folded into Other) rather than only the rows the setting asked for at
  fetch time: the alternative — holding every parsed row, up to
  `MAX_QUERY_ROWS` of them, for the life of the process — is what made
  `top_rows` part of the query identity in the first place, and the cap here
  is on distinct service names instead.
- **A tolerant sub-fetch re-raises `AzureThrottled`.** The five of them
  (`_safe_quota_id`, `_safe_foundry_ids`, `_safe_budget`, `_safe_forecast`, the
  marketplace query) share one shape: re-raise the 429, note a permission
  error, note anything else. A 429 is an answer about the whole tenant, not a
  detail of one sub-fetch, and `_fetch`'s handler is the only place it is
  recorded. A 5xx is raised into that last branch rather than returned as an
  empty answer (`_server_error`), because "the server is failing" and "there
  is nothing here" reached the caller as the same value and the note never
  fired for the failure that actually happens. A 4xx still returns: a forecast
  a new subscription has no history for is a 400, and a note on every refresh
  would be noise.
- **A cost column is found by name, never by position**, and its absence is an
  error routed through `_remember_error`. `row_count == 0` *with* a column is
  still a legitimate empty month, since the data lags 8–72 h.
- **An unreadable offer type shows no gauge, whatever the total.** Cost
  Management Reader without Reader is the likely role split, and an Azure
  Sponsorship subscription reports zero while the credit drains. Round 1 let a
  *positive* total rule that out; round 2 reversed it, because a Sponsorship
  subscription still bills Marketplace and other non-sponsored charges
  normally — so a small positive total is exactly what one looks like while
  the sponsored credit drains unreported. The money is still shown and the
  note says Reader is what the check needs; only the percentage is refused.
- **One `gaugeable` flag governs every percentage on the tile.** An allowance,
  a complete read, one billing currency, a readable offer type, no Sponsorship
  offer, and settings that have not changed since the figures were read. The
  summary percent, the breakdown shares and whether there is a forecast row at
  all follow it together, because a tile that says "no gauge is shown for a
  subtotal" and then prints a projection of that subtotal one row down has
  told the reader nothing. Sponsorship was the last reason to sit outside the
  flag — it suppressed the summary percentage by its own path and left the
  rows below it gauged.
- **A number the tile has disowned is never printed.** More than one billing
  currency shows per-currency subtotals (`CAD 100.00 + JPY 1,000.00`, at most
  three then `+N more`), never their sum. A truncated read shows `incomplete`
  on the row and moves the subtotal into the note, labelled as read so far —
  except when the read stopped on a repeated `nextLink`, where the subtotal is
  too *high* rather than too low and the note says it may count rows more than
  once.
- **`_snapshot_signature` ignores `reset_label`, for every provider.** It is a
  caption — a ticking countdown, or Azure's spend to the cent — and either one
  counted as "this provider changed", which reset the adaptive backoff and
  pushed the whole app back into active-cadence polling.
- **A typed allowance is always the denominator.** Round 1 preferred a
  budget unconditionally and round 2 reversed it: "smallest qualifying budget
  wins" is the right rule between budgets, and the wrong rule against a number
  a person typed, since a 1.00 alert canary or a per-team budget would take
  the tile and the tray dot over. A qualifying budget is reported in the note
  instead. It becomes the denominator only when nothing is typed.
- **A budget is a calendar-month figure.** Even then it qualifies only when
  `reset_day == 1`, only when its currency matches the cost data, and only
  when it measures the same scope: unfiltered when the tile is unfiltered, or
  filtered to exactly the configured resource group when it is. An unfiltered
  budget on a resource-group-filtered tile is refused too: it covers spend the
  tile does not measure, so the gauge would under-report by the ratio between
  them. The smallest qualifying one wins. Budget pagination is deliberately
  not followed.
- **The period boundary is a UTC date**, because that is how Cost Management
  dates usage; `resets_at` converts it to local time for display. Copilot does
  the same, so the two Microsoft tiles agree about the 1st of the month.
- **The summary metric's label is a key, not a caption.** It must stay
  "Spend this month"; the money lives in `reset_label`, which the row renders
  inline. Changing the label back would restart the history-key churn.
- **Error strings name an exception type, never quote one.** `str(exc)` on a
  requests exception carries the request URL and with it the subscription id.
  The redaction in `error_dialog` is the second line of defence, not the
  first.

---

## 9. Delegating to OpenCode — evaluated, guarded, not adopted

`docs/opencode-plugin-evaluation.md` is the full review of the two plugin
families that connect Claude Code and OpenCode, read at the commits named in
it. Two things from it are operational rather than advisory:

- **`opencode-plugin-cc` rewrites OpenCode's global permissions on every server
  start**, setting `bash`, `edit`, `webfetch` and `external_directory` to
  `allow` in `~/.config/opencode/opencode.json`. Reverting it once is not
  enough — the next delegation puts it back. `python tools/egress_guard.py
  posture` is what notices, and it runs as part of every `preflight` and
  `dispatch`.
- **The subscription-bridge plugins are declined, not parked.** OpenCode's own
  provider docs state that Anthropic prohibits using a Claude Pro/Max
  subscription from OpenCode, and the plugins that do it work by impersonating
  Claude Code down to the system prompt and billing-header signature. Nothing
  about that improves with a later version.

**What the guard is not.** `tools/egress_guard.py` is a choke point, not a
sandbox or a DLP product. It sees the payload handed to it and refuses on
credential shapes, denied paths, marked material and unapproved destinations.
It cannot see what a delegated agent fetches on its own once `bash` and
`webfetch` are allowed, which is why the evaluation puts a network allowlist
under it rather than treating the scan as sufficient. Regex detection has false
negatives by construction: a clean scan means nothing known-bad was found.

**Nothing in the app changed.** The guard is developer tooling in `tools/`,
stdlib-only so it runs without the app's dependencies, and no version bump goes
with it.

### What the security review of the guard found, and what is still open

A review by execution of `tools/egress_guard.py` produced nine blocking
findings. All nine are fixed on this branch, each with a driver run before and
after: the destination check was a string prefix and a payload demonstrably
left the machine to an off-box address while `posture` said `ok`; the
unrecognised-credential catch-all defaulted to `warn`, and `warn` neither
blocked, redacted nor printed; `\b` in the `secret-assignment` rule never fires
after `_`, so `DATABASE_PASSWORD=` produced no finding; the path scanner was
quadratic (22 s at 64 000 characters, and it did not finish at the tool's own
400 000-byte cap) and the cap did not gate the scan; `--include-diff` executed
the workspace's `diff.external`; `cmd_dispatch` had no test and three gutting
mutations survived; `hook` failed open on every error path; the documented exit
codes held on no fault path and the audit line was written after the POST.

Four things are deliberately **not** fixed, and are limits of the design rather
than defects to patch:

- **Every posture input is settable by the thing being checked.**
  `opencode_config_path()` builds its path from `$XDG_CONFIG_HOME`. Measured:
  with that variable pointed elsewhere, a real drifted config
  (`bash`+`webfetch`+`external_directory` = `allow`) is invisible and `posture`
  reports ok. A missing config file and a config path that is a directory both
  read as clean, and the object form `{"bash": {"*": "allow"}}` is not
  recognised, because the check is an exact string test. Malformed JSON shapes
  no longer crash out of `posture()` — they are faults now, which is what the
  exit-code contract requires — but the gap itself is not staleness and not
  parsing: it is that the check consults a path the delegated agent can move.
  Closing it needs the config to be read from a location the agent cannot
  redirect, which the guard cannot arrange on its own.

- **The policy that governs the guard is a workspace file the delegated agent
  can write.** Candidates are `--policy`, `$AIGAUGE_EGRESS_POLICY`, then
  `<workspace>/.egress-policy.json`. Measured: a planted `.egress-policy.json`
  with every detector `off`, `destinations.allow: ["*"]` and `audit.path`
  `/dev/null` dispatches a GitHub token and a path to `~/.aws/credentials` with
  exit 0. The control and its configuration share a trust boundary with the
  thing being controlled. `dispatch` now prints and records `policy_source`, so
  the substitution is at least visible after the fact, but **`--policy` or the
  environment variable is the only supported source for anything unattended** —
  a workspace file is a convenience for interactive use.

- **`fnmatch` globs cross `/`.** `openrouter/*` matches `openrouter/a/b/c/d`.
  Pin exact ids when the depth matters.

- **Regex detection has false negatives by construction**, as this section
  already says. The review confirmed it: every base64, URL-encoded, split-line
  and zero-width variant of every credential in its corpus walks through. The
  guard is a choke point for the plain forms, not a DLP product.

Two smaller items were left as they are and are recorded here rather than
fixed: an over-cap payload is hashed and counted in full in the audit line but
only scanned up to the cap, and the line says so in its `refusals` field rather
than in the hash itself; and `_scan_paths` still strips `a/` and `b/` diff
prefixes from every path-shaped token, so a real top-level directory called `a`
or `b` is matched by its suffix.

### What the second review found, and what that round left open

The second review by execution took the nine fixes apart again and found two of
them had been made at the instance rather than at the class, plus six smaller
findings. All of them are addressed on this branch — the scanner is linear in
the size of the payload and in the number of findings it produces, on fillers
that match nothing and on fillers that match almost everywhere, and a test fails
the build on any unbounded quantifier in any pattern; `--include-diff` diffs commit trees, runs no workspace-planted
command on twelve vectors, and no longer runs `git status`; a `block` can no
longer be silenced by an overlapping `redact`; a redirect off the pinned host is
refused rather than followed with the server password attached; `scan` refuses a
payload it could not finish reading; the path scanner has no window and
therefore no band; the hook refuses a direct POST to the agent's own server; and
the two rules that fired on every mention of a credential need a credential.

What that round leaves open, deliberately:

- **A regex is not a shell parser.** The hook now refuses the `curl`/`wget`/
  `http`/`python -c` family aimed at the agent's own endpoint, which was the
  gap that mattered, because that endpoint is the guard's own dispatch path.
  The forms that still slip are shell quote-splitting (`o''pencode`,
  `opencod\e`, a line continuation inside the binary's name), an `alias`
  defined earlier in the session, and a Cyrillic look-alike. Closing those
  needs a shell parser, and a hook that tried to be one would be a larger
  attack surface than the one it guards.

- **A denied path followed immediately by sentence punctuation is not
  recognised.** The scanner takes whole whitespace- or quote-delimited tokens
  and requires every character to be a path character, which is what stops it
  blocking on every mention of a store path in prose and documentation. The
  cost is that `read /home/u/.aws/credentials,` is not matched. A trailing `.`
  was already missed before this change. The same trade-off in the other
  direction — stripping punctuation — puts the repository's own docstrings back
  on the blocked list. (The third round narrowed this: a trailing bracket, a
  `?query` and a `:line`/`#fragment` citation *are* now taken off the token, so
  `(/home/u/.aws/credentials)` and `…/credentials:12` are matched. Sentence
  punctuation still is not.)

- **The deny list blocks this repository's own documentation of its own
  store.** `SECURITY.md` names `%APPDATA%/ai-gauge/secrets.dat` in a table, so
  `git diff | scan --stdin` over the two most recent diffs that touch it exits
  2 on one `denied-path` finding each. That is the rule doing exactly what
  `paths.deny` asks of it, and the answer for this repository is its own
  `paths.deny` in a `--policy` file rather than a narrower default that would
  stop denying the store. The repository's *source* no longer blocks: handing
  `src/aigauge/secret_storage.py` to a delegate went from one blocking finding
  and four redactions to none.

- **`opaque-token: redact` keeps its false positives.** On this repository's
  source it costs 13 placeholders on identifier runs such as
  `transport_max_attempts=SCRAPE_TRANSPORT_ATTEMPTS`. An unnamed high-entropy
  blob is the case where taking it out of the payload is most obviously right,
  and a placeholder is not a block, so the default stays.

- **`--include-diff` no longer sees uncommitted work.** It diffs `<base>...HEAD`
  because a clean filter runs whenever git has to turn a worktree file into a
  blob, and a filter driver can be called anything, so pinning the names cannot
  be complete. Commit first, or pass the text with `--task`/`--stdin`. A path
  buried in the middle of a single token longer than 4 096 characters is
  likewise not matched; tokens that long are scanned in overlapping windows,
  which catches the path at the end of one but not a path with another 4 KB of
  path characters after it.

- **The audit file's mode is POSIX-only**, as `SECURITY.md` says of the app's
  own stores, and the guard's doc repeats. The directory chain is now `0700` at
  every level on POSIX; on Windows the file inherits the parent ACL.

### What the third review found, and what this branch leaves open

The third review by execution closed all fifteen round-2 findings on their own
terms and found one High plus three Lows and six nits. The High was the same
class of defect for the third time, at a third site: round 1 bounded the path
scanner, round 2 bounded every other pattern, and the loop that resolves
*overlapping findings* — added by round 2's own fix — was still quadratic, this
time in the number of findings rather than in the bytes. It checked each
candidate span against a list of every claim already made, so 400 KB of `a@b.co `
— a contact list, not an attack — is 57 143 findings and took 58.0 s in `scan()`
and 58.4 s in the hook, against the hook's own documented 10 s timeout, and a
hook killed at its timeout blocks nothing. A CSV of addresses was 9.1 s and a
400 KB `.env`-shaped block 7.9 s. The claims are now a byte per character of the
payload instead of a list of spans, which is O(1) per claim, and `redact()`
builds the redacted text in one pass instead of rebuilding the whole string once
per finding. The same payloads are 0.44 s, 0.25 s and 0.22 s, and `redact()` on
57 143 spans went from 13.5 s to 0.02 s. The reason the suite could not see any
of it is recorded in the tests: eighteen of its nineteen linearity fillers
produced zero findings and the nineteenth produced 1 482, so the byte axis was
tested exhaustively and the finding-density axis not at all. Four fillers that
are almost all findings, and one that fills both severity bands with overlapping
blocks and redactions, are now in the same parametrised tests.

Also taken in that round: an ambiguous or wrong `--base` is a fault rather than a
silently empty diff (git's own "refname is ambiguous" warning was being thrown
away, so `--base amb` diffed whichever ref git picked and reported it clean);
the shipped `tools/egress-policy.example.json` carries the built-in deny list in
full, because a policy file *replaces* `paths.deny` rather than adding to it and
the example was quietly four globs weaker than the defaults; a denied path
followed by `:12`, `#L4`, `?x=1` or a closing bracket is matched; `nc`/`socat`
conversations with the agent's own port are refused; a quoted secret of six
characters or more is redacted, where the floor used to be eight; `posture`
prints the policy source the other commands already print; and the hook's
literal-endpoint branch — the only thing that catches a POST to the pinned
server on a path other than `/session` — finally has a test.

What this round leaves open, deliberately:

- **Two overlapping redactions still resolve to one of them, not to their
  union.** `api_key=aaaaaaa1@example.com` is dispatched as
  `[redacted:secret-assignment]@example.com`: the credential-shaped part is
  removed and the domain of the address survives. Merging the spans would be
  strictly better, but every way of doing it inside five lines changes which
  rule is *reported* for an overlap — a longer `opaque-token` run would start
  displacing the named rule beside it — and that is a worse report for a
  cosmetic gain. Left for a round that can measure the report quality.

- **The bypass regex is still not a shell parser**, and `deno run -A
  opencode.ts` and a port held in a shell variable (`S=4096; curl …:$S/session`)
  join the list above. The netcat pair was worth one more alternative because
  `nc` was already in the client list; the rest are the same trade as before.

- **A denied path inside a markdown link is not matched.** `[creds](/home/u/
  .aws/credentials)` is one token whose middle is `](`, and neither character is
  a path character, so the token is dropped before any glob sees it. Trimming
  brackets off the *ends* of a token, which is what this round added, does not
  reach it.

- **An over-cap payload is still hashed in full and scanned only to the cap**,
  and the `a/`/`b/` diff-prefix strip is still redundant given that `fnmatch`'s
  `*` crosses `/`. Both were recorded in the round-2 list above and neither
  moved.
