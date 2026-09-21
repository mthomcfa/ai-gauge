# Next session — parked items

State at close of the 2026-08-10 session. `main` is `1.0.0+cfa.2` at PRs #6–#16,
610 tests passing, all five providers reading.

> **Updated 2026-09-18** by the naming release (`1.4.1+cfa.9`, 2 109 tests):
> every provider surface now names the company behind the product, from one
> table (`src/aigauge/naming.py`) instead of the nine copies that had drifted
> apart - `Anthropic · Claude`, `OpenAI · ChatGPT + Codex`, `Microsoft · Azure`,
> `GitHub · Copilot`, `OpenRouter`, `OpenCode` - with the product name alone
> where there is no room for a company. Copilot moved out of the Microsoft
> Settings tab into a GitHub tab of its own, because the credential is a GitHub
> PAT and the host is `api.github.com`. Tile headers elide rather than widen the
> 260 px floor, Azure's component rows carry their amounts on the row, and the
> meter catalog's label bound is one number shared with the extractors.
>
> **It closes two items from 1.4.0+cfa.8's residual list** - the unbounded
> metric label in the tray tooltip (now `models.bounded_label`; no escaping,
> because a `QSystemTrayIcon` tooltip is plain text) and the unbounded
> API-supplied model id in OpenRouter's note (now `bounded_note`). It closes
> nothing in [§4](#4-known-defects-deliberately-not-fixed) or
> [§8.3](#83-known-soft-spots-in-what-was-built): "History still keys on the
> display label" (§8.3) is untouched and still correct - provider *display*
> names never reach `history._state_key`, which keys on `provider::metric-label`,
> and no metric label was renamed here. It does add one soft spot of its own:
> the collision relaxation for a renamed per-model row trusts the labels a
> single scan saw, so a scan that runs while the page is half-rendered can
> adopt a bare model name beside a longer one that simply had not painted yet.
> The cost is one extra informational meter against the 24-meter ratchet, which
> is the failure this direction was chosen for.
>
> **And four things it looked at and left as they are:**
>
> * **The relaxation is wider than "a renamed model row", and it is the page
>   that decides.** The shield only holds while the longer label is among the
>   labels the scan saw, so a page that simply omits it defeats the shield -
>   and against the bundled Claude catalog that makes `Design`, `Claude` and
>   `Daily` adoptable as well, each being a whole word inside a bundled label.
>   A meter row labelled `Claude` can therefore appear inside a tile headed
>   `Anthropic · Claude`. Bounded by `MAX_ADOPTED_METERS` (24) and by
>   `primary=False`, so an adopted row can never take the tray colour.
>
>   What the shield *does* cover is every decoration: the longer label counts
>   as seen when it sits inside a label the scan saw, as whole words, so
>   `Opus only:`, `Opus only;`, `(Opus only)`, `Opus only |`, a trailing
>   zero-width space and the `Opus only…` an elided row ends in are all still
>   shielded. Omitting the longer label entirely is the one route left, and it
>   is the route this relaxation exists to allow. Two shapes it does not
>   cover, both pre-existing: a page that renders `Opus only.` gets that
>   adopted as a meter in its own right, duplicating the bundled one, because
>   the full stop misses equality against `known_labels()` by one character
>   (`primary=False`, one of the 24 slots); and a label carrying a word of its
>   own is a different label, which is the rule, not a hole.
> * **A named extra account's collapsed chip shows the bare name.** A Claude
>   account the user called "Microsoft · Azure" gets a chip reading exactly
>   `Microsoft · Azure`, while the tile header, the tray line and the tooltip
>   all keep the qualifier. Deliberate since 1.4.0: it is what stops a chip
>   from wrapping and costing the collapsed panel a whole row. Composing
>   `compact (name)` for every id instead would widen every chip, and the
>   chip-row width rule is unchanged from 1.4.0, so it stays as it is until
>   something else moves that rule.
> * **Two named Codex accounts are indistinguishable on the panel at its
>   260 px minimum.** `OpenAI · ChatGPT + Codex` and
>   `OpenAI · ChatGPT + Codex (Work)` both paint as `OpenAI · ChatGPT + Co…`
>   in the room the header gets; the tooltip and the collapsed chip tell them
>   apart. `ElideRight` is what the header was specified with.
>   `Qt.TextElideMode.ElideMiddle` would keep the bracketed identifier - the
>   part the user chose - at the cost of the company prefix, which is the part
>   this release added. An open question for the next release, not a change to
>   make inside it.
> * **`widget._session_summary_for` is dead code.** No caller anywhere in
>   `src/` or `tests/`. Pre-existing, correct (it reads from
>   `display_name_for_account`), and left alone to keep this diff to the
>   release's subject.
>
> **Updated 2026-09-17** by the UI release (`1.4.0+cfa.8`, 2 024 tests): the
> panel is resizable and remembers its size, every Settings tab scrolls, the
> app has an icon, and three things the user's own desktop turned up were
> fixed. **It closes nothing in [§4](#4-known-defects-deliberately-not-fixed)
> or [§8.3](#83-known-soft-spots-in-what-was-built)** - checked, and none of
> the six items had an entry there. That is not a gap in the tracker: every
> one of them came from using the app on a real desktop rather than from a
> review of what was built, which is a source this file has no section for.
> The one place it touches is a name: `WINDOW_MAX_HEIGHT` is now
> `WINDOW_AUTOFIT_MAX_HEIGHT`, and
> [`docs/ui-scale-widget-only-plan.md`](ui-scale-widget-only-plan.md) - a
> proposal that has not been started - still cites the old one. It also adds
> [§10](#10-upstream-contribution--after-the-next-batch).
>
> **Updated 2026-09-16** by the REST-deadline follow-up (`1.3.2+cfa.7`,
> 1 859 tests), which closed the three residuals that release left in
> [§8.3](#83-known-soft-spots-in-what-was-built): the unbounded REST socket,
> the non-atomic `Config.save()`, and what `SECURITY.md` did not say about
> "Clear all browser data". It also gave the egress guard's glob-side case
> fold the test that mutation S6a walked through ([§9](#9-delegating-to-opencode--evaluated-guarded-not-adopted)).
>
> **Updated 2026-09-15** by the hardening follow-up (`1.3.1+cfa.6`,
> 1 277 tests), which closed most of what the refresh-cadence work left in
> [§8.3](#83-known-soft-spots-in-what-was-built): the REST park, the
> "Clear all browser data" purge, the dispatch epoch's name, the log
> summariser and the scraper's uncapped log lines. What is still open there
> is marked as such.
>
> **Updated 2026-09-15** by the refresh-cadence work (`1.3.0+cfa.5`,
> 1 216 tests). Its residuals were folded into
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
- **An account dropped by the reserved-id rule leaves its profile and its
  keyring entry behind.** `Config._migrate` drops a `BrowserAccount` whose id
  is one of `copilot`, `openrouter`, `opencode_go` or `azure` - the provider
  keys `_build_providers` creates that are not accounts - so the account is
  gone from the model and no removal path will ever queue its
  `profiles/<id>` directory or its `ai-gauge` keyring cookie. Settings'
  "Clear all browser data" is what reaches them, because it sweeps every
  directory in `profiles/` as well as the configured accounts. Acceptable as
  it stands: a config the app itself wrote cannot carry such an id
  (generated ids are `<kind>-<uuid4>` and the fixed ones are
  `claude`/`codex`), so reaching this needs a hand-edited or hostile
  `config.json`, and whoever can write that can write the profile directory
  too.
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
- **A hung REST worker no longer accumulates siblings, but the socket
  itself is still unbounded.** Copilot and OpenRouter refuse nothing of
  their own (Azure has its `state.in_flight` gate), and `requests`' `timeout`
  is per socket operation rather than a total, so a server that sends one
  byte every 14 s against a 15 s timeout holds a worker forever. The
  assumed-dead ceiling used to hand that endpoint a fresh worker every time
  it expired.

  **Option (c) was taken in 1.3.1+cfa.6.** A provider whose `uses_browser`
  is False stays parked until its worker reports back - any snapshot for
  that name, live or late - or until `_REST_PARK_BACKSTOP_SECONDS` (one
  hour), whichever is first; the browser providers keep the 2x ceiling,
  because the account-keyed live-scrape registry catches the one case it
  lets through. Six fake hours against a wedged REST worker with the app in
  its active five-minute cadence: **50 dispatches before, 6 after**, no two
  closer than 3 680 s, with the browser sibling unchanged at 26 and 28. An
  idle app is 11 and 6 over the same six hours, and 28 and 24 over a day,
  because its own backoff already spaces the cycles out. The `abandoned` log line names the rule that
  applied (`ceiling=browser_2x` / `ceiling=rest_backstop`). This was safe to
  do only because all three REST providers always call `on_done` unless
  `work()` never returns - Copilot's and OpenRouter's `_run_async` wrap
  `work()` in try/except, Azure's does the same and its `work()` has a
  `finally`.

  **The bound was the pool, and the pool still fills if the socket never
  closes.** Copilot, OpenRouter and Azure all submit to
  `QThreadPool.globalInstance()`, so the live socket count can never exceed
  `maxThreadCount`; measured over six fake hours against a byte-dripping
  server, every pool slot ends up stuck (1 of 1, 2 of 2, 4 of 4, 8 of 8)
  with an unbounded FIFO of queued runnables growing about four or five
  objects an hour, after which all three REST tiles are dead for the life of
  the process. What the park change removes is the *supply* of new stuck
  workers - one an hour per provider instead of one every few minutes - not
  the wedged worker itself.

  **Option (a) was taken in 1.3.2+cfa.7: a total-response deadline on the
  REST side** - `providers/_http.py`, `stream=True` plus an elapsed check
  and a byte count while reading, *and* a `threading.Timer` armed with the
  same deadline that shuts the connection's socket down from another thread.
  It is the only one of the four that bounds the socket, and the only one
  that frees a worker already stuck. The bound covers **connect, headers and
  body** - everything from the socket onwards, name resolution excepted (see
  below): the in-band check cannot see inside one read, and two phases live
  there - the header block (every arriving byte resets the per-socket
  timeout, and `bounded_request`'s own clock is not reached until the headers
  are complete) and a `Content-Encoding` body that decodes to nothing
  (urllib3's `read1` loops internally until the decoder yields). Both were
  found by the round-1 reviews after the first cut of this release shipped
  with the check only, and both are the same failure the release exists to
  remove, one protocol phase earlier. The socket shutdown is the only thing
  that reaches a thread blocked in `recv`; urllib3's `Timeout(total=…)` does
  not (it clamps the value of the per-read timeout, which every byte resets -
  measured, a 30 s header drip returned at 30.01 s against a 3 s total). The
  connection is learned by mounting a small `HTTPAdapter` subclass on a
  `Session` built for the one call, which also keeps the property
  `requests.request` had: no pool, cookie jar or connection across refreshes.

  One call now returns or raises by `max(connect, total_seconds) + read`,
  which is 45 s at a 15 s timeout and 40 s at a 10 s one; a whole refresh is
  130 s for Copilot, 135 s for OpenRouter and 495 s for Azure, and each
  provider tells the App that number. The formula did not move when the timer
  landed - `total_seconds` is the larger term at every timeout here, so the
  bound is `total_seconds + read`, and the `+ read` is the one read the cut
  socket can leave in flight. A timer that fires before the socket exists
  cannot cut anything, so it **re-arms every 0.25 s until the call ends**
  rather than giving up: the first cut of this release gave up there, and a
  resolver slower than the deadline then left the status line, the header
  block and the body bounded by nothing again (measured at the scaled
  constants: 40 s and 70 s against a 4.0 s bound, ended by the harness rather
  than by the app, and 3.75 s once the timer looks again). The third review
  found the same hole one layer along, on the path every production host
  uses: for the whole of a **TLS handshake** the object urllib3 keeps in
  `conn.sock` is the plain socket, which `ssl.wrap_socket` detaches before
  the handshake runs, so `shutdown()` on it raises EBADF - and that branch
  swallowed the error and did not look again, which gave the deadline away
  for the rest of the call. Measured over real TLS against a 12.0 s bound: a
  deadline landing inside the handshake 23.0 s, a re-arm landing there after
  a slow resolve 22.5 s, and one dripped header line unbounded - still inside
  the call at the harness's give-up - against 3.25 s and 2.56 s now that the
  EBADF looks again too. A timer that cannot be *started* at all - the
  process is out of threads - is the one remaining way the bound can go: the
  first arm now refuses the call with `DeadlineUnavailable` rather than
  letting a bare `RuntimeError` past every caller's `except
  requests.RequestException`, and a failed re-arm writes
  `provider http deadline_rearm_failed=True` and marks the deadline instead
  of dying in `threading.excepthook`. The mark is not the bound back: it
  bites at the next in-band check, and a call already stalled in a header
  read never reaches one, so that call stays unbounded - measured, still
  inside it at a 14 s give-up against a 4.0 s bound. What changed there is
  that it is no longer silent. What the bound
  does **not** cover is name resolution: `getaddrinfo` runs before any socket
  exists, so neither the connect timeout nor the timer reaches it and the OS
  resolver's own timeout is what ends it - added to the 45 s rather than
  counted inside it, through plain `requests` just the same (a resolver
  blocking 20 s held both for 20.00 s against a 4.0 s bound). The App
  watchdog is the backstop there and not a fix: it ends the App's wait, and
  the worker stays in `getaddrinfo` until the resolver gives up; the real
  answer is resolution on a thread, which is a bigger change than this one.
  What moved is that it is now a bound. Measured against
  a dripping loopback server with the constants scaled down (1 s socket
  timeout, 3 s total, promised bound 4.0 s): a 40-byte body drip returned
  after 7.81 s with the timeout never firing and a 5 000-byte drip still held
  the worker at 30.0 s, both before; a dripped status line, a dripped header
  block, a header block that never ends and a chunked or `Content-Length`
  gzip-of-nothing each held the worker to the harness's 40 s cap before, and
  all of them raise `ResponseDeadlineExceeded` at 3.00 s after. In shipped
  units a header drip of a byte every 10 s went from unbounded - still inside
  the call when a 100 s harness gave up watching - to 30.0 s against a
  declared 45 s. (An earlier draft of this paragraph said "90.0 s" there.
  That was the second at which the *server* stopped dripping, not the one at
  which the call ended, so it understated the before-state.)

  What is still true: **the pool is still shared**. Three REST providers
  still submit to `QThreadPool.globalInstance()`, so they still compete for
  slots with each other and with anything else that uses it - what has
  changed is that a slot is now held for a bounded time rather than for the
  life of the process. (b), a dedicated `QThreadPool` per provider, is still
  available as containment for that, and still frees nothing on its own. (d),
  a busy flag on the provider, is still moot: the park does that job from the
  App side, without writing a provider attribute from a pool thread.

  The residuals of the transport itself, recorded rather than fixed. **Name
  resolution is outside the bound** - the paragraph above - so a worker can
  be held for the OS resolver's own timeout on top of the 130/135/495 s these
  budgets promise, and a watchdog can therefore still fire inside a refresh
  that has not exceeded its own bound. Fixing it means resolving on a thread
  and cancelling that, which is a change to how every call is made rather
  than a clause in this one. **A server that closes the connection inside the
  header block is a successful, empty 200** - `http.client` treats EOF as the
  end of the headers, so `bounded_request` hands back a 200 with no body and
  the call site reports whatever `.json()` says about an empty document
  (plain `requests` does the same, so it is not this release's doing). The
  helper cannot tell that from a legitimate empty body - a 204, or a HEAD -
  so the judgement belongs at the call sites; Copilot's username resolve,
  where it produced "PAT may lack read:user" for a truncated reply, is the
  one that had it wrong and is fixed. The third review found that two rounds
  had closed two routes to that message without touching the rule behind
  them - `_resolve_username` answered `None` for **any** transport failure,
  so a deadline, an oversized reply, a refused encoding, a 500 and an
  ordinary offline machine all told the user to re-issue a credential that
  was fine. The rule is now what the message is a diagnosis of: GitHub
  answered and refused, a 401 or a 403 on `/user`. Everything else reaches
  the tile as what it was. **A `Session`, an adapter, a pool and a timer per
  call**, with no connection reuse: that is what makes the deadline possible
  (a mounted adapter is the only way to learn the socket) and what keeps a
  hostile endpoint's cookies and pool out of the next refresh, and it is what
  `requests.request` already did. It costs about 1 ms per call of machinery -
  measured 1.04 ms against plain `requests`' 0.83 ms on loopback - plus a TCP
  connect and a TLS handshake where a kept-alive pool would have neither,
  which is eleven of each on Azure's worst refresh. The fix, if that ever
  matters, is a session per provider with an explicit close between
  refreshes, which trades the isolation away.

  The rest of the list, a sentence each, from the two confirmation reviews.
  **The comma rule refuses more than urllib3 would** - `Content-Encoding: ,`
  builds no decoder there and is refused here - which is over-refusal in the
  fail-closed direction, is stated in the docstring, and reaches no host this
  app speaks to. **A single-layer gzip bomb is still open on a sub-floor
  urllib3**: `pyproject.toml` declares `urllib3>=2.6` and `release.yml`
  builds in a fresh venv, but `build.sh` and `build.ps1` install nothing, so
  a developer `.venv` left on 2.5 is not caught - one `pip install -e .` line
  closes it. **`cancel()` cannot stop a `_fire` already past its `_ended`
  check**, so a shutdown can land after the `finally` has run; harmless
  because the session, the pool and the connection are that call's own and
  are being closed, and the class docstring now says why rather than leaving
  it to luck. **`_watch_pool` is still unguarded**, deliberately: a pool
  shape it cannot wrap fails the call closed with its own `AttributeError`,
  a non-`RequestException` that each provider's blanket handler takes. **A
  2xx on `/user` that is not a 200, and a 200 whose JSON has no `login`,
  still read as "PAT may lack read:user"** - GitHub answered and did not
  refuse, so the rule's own wording does not quite cover them; neither is
  reachable against `api.github.com`, and `_resolve_username`'s docstring
  records both. **The CHANGELOG's own test counts are pinned by nothing**,
  by decision: a test that asserted them would have to re-collect the suite
  from inside it, and the counts are re-collected by hand each round
  instead. **Pre-existing and unchanged by this release**: `snapshot.raw`
  reaches Copy diagnostics by design, `x-github-request-id` is still logged,
  OpenRouter's tile still shows `str(exc)` for a `RequestException` (a host,
  no identifier) and its blanket handler still logs a traceback, and the
  `QThreadPool` is still shared, so the three REST providers still compete
  for slots - for a bounded time now.

  Two of that list closed in the confirmation pass rather than being
  recorded. `_unwatch_pools` was guarded from the caller but not total
  inside, so one pool that refused the `del` left every pool after it wrapped
  and `_watched` uncleared - each pool comes off under its own guard now, and
  the first failure is re-raised once the loop is done so the one log line is
  still written where it was. And `deadline.cancel()` sat above the guarded
  chain, where a `cancel()` that raised would have skipped the un-watch and
  `session.close()` with it (measured: session closed False); it is inside
  the chain now, with the close in a `finally` under it.

  Two things surfaced in the doing, and are worth not re-learning. First,
  `Response.iter_content(chunk_size=N)` cannot implement this. urllib3's
  `stream()` blocks until `N` bytes have arrived or the connection closes,
  and the per-socket read timeout never fires on a server dripping inside it,
  so the elapsed check would not run again until 64 KiB had been dripped.
  Measured at one byte per 50 ms behind a 2 s socket timeout,
  `iter_content(64 KiB)` never yielded at all. The drain is `raw.read1()`,
  with `iter_content(1)` behind it for a handle that has none (urllib3 1.x):
  correct there too, and 3 854 ms per MiB against `read1`'s 0.5 ms. Second,
  *no* read granularity bounds a read that never returns. `read1` loops
  inside urllib3 until the decoder yields something, and `iter_content(1)`
  goes through the same decode, so a stream of empty DEFLATE stored blocks
  (five bytes in, zero bytes out) blocked both paths until the harness gave
  up. Between-reads checks are for the size cap; the clock needs the socket.

  **Every dispatch of a hung REST provider costs a slot, which is why the
  kept retry is folded into the cadence.** An earlier draft of the retry
  deferral re-armed a parked provider's due for the instant the park lifts;
  measured against the round-2 tree with the same seeds that was 4→5 and 6→7
  dispatches an hour and 15→17 worst concurrent REST workers in the 60-seed
  six-hour fuzz. Owing the due no earlier than the next cadence wake puts all
  three numbers back (4, 6, 15/14/15). With an hour-long park the same rule
  is what keeps the due riding ordinary cadence wakes - pinned over eleven
  five-minute wakes inside one park - rather than arming an hour-long timer
  of its own.
- **On a one-core host every REST watchdog is thirteen minutes.**
  `_pool_wait_slack` adds `sum(every other REST budget) / maxThreadCount` to a
  dispatch's watchdog, because the cycle hands openrouter, copilot and azure
  to the shared pool in one burst and a budget that starts at dispatch would
  otherwise fire inside a refresh that has not exceeded its own bound. With
  1.3.2+cfa.7's numbers (openrouter 135, copilot 130, azure 495) that is
  **780 s for all three at capacity 1** - the sum plus the slack, whoever is
  waiting - 465/468/648 s at capacity 2 and 308/311/581 s at 4. The
  1.3.1+cfa.6 numbers were 365/365/365, 222/222/305 and 151/151/275, so this
  is up by a factor of two; the reason is that the per-request term used to be
  a per-socket timeout, which bounded nothing, and is now a whole call.

  It reads worse than it is. A watchdog ends the App's wait, not the
  provider's work, and until 1.3.2+cfa.7 it was the *only* thing that ended a
  wedged REST refresh - the worker itself never came back. A REST provider now
  gives up on its own at 130, 135 or 495 s and closes its own cycle, so the
  watchdog is a ceiling that is reached less often than before rather than
  more. What has not changed: while a cycle is open the scheduler timer is
  stopped, and a REST park still lasts until the worker reports back or one
  hour (`_REST_PARK_BACKSTOP_SECONDS`), whichever comes first, rather than
  twice the budget.

  The model is also pessimistic by construction: it assumes full serialisation
  of every other budget even at capacity 3, where the real wait is zero. It is
  still the right trade - a watchdog that fires early manufactures the failure
  it exists to catch, and feeds the parking machinery - but the alternative is
  to scale the allowance by pool size (or to have providers report when their
  work actually starts, which is a Provider-API change: the API is one
  callback).
- ~~**"Clear all browser data" still purges a profile the App may be
  scraping.**~~ **Closed in 1.3.1+cfa.6.** The dialog emits the id list on
  `browser_data_clear_requested` and the App defers each one exactly as it
  defers a removal; both purge paths now also ask `account_is_busy()`. The
  clear-all ids are held on a separate list, `config.pending_data_clears`,
  because the `pending_profile_purges` drain skips an id that is also a
  configured account by design - which is right for a removal a restored
  backup has undone and would drop every deferred clear at the next start.
  That second list is persisted too, and drained at startup beside the
  first, before any cookie is hydrated and before any provider exists, with
  no configured-account skip: the user asked for those profiles to be gone.
  It was in memory only for one round, which meant a quit inside the
  deferral window left the live provider session cookie on disk with the
  keyring copy already deleted - nothing in the UI would mention it again
  and clicking the button a second time was the only thing that reached it.
  The two lists are kept *disjoint*: an id owed both - one dialog session
  removing an account and clearing all browser data puts it on each, as two
  separate calls - stays on the clear list only, because both end in the
  same `purge_profile` and the clear's drain skips nothing. Carrying both
  cost a second deletion and, while that account's scrape was out, a second
  `deferred` line at every heartbeat. The cost of "skips nothing" is that a
  `config.json` restored from a backup taken inside the deferral window, or
  synced from another machine, signs the user out of every account it names
  at the next start with only an `info` line to explain it - the alternative
  drops every deferred clear instead, which is the defect the list exists to
  fix. The dialog also stops asking twice:
  `removed_profile_ids` drops whatever the clear-all set already covered.
- **A deferred purge makes the app write `config.json` on its own.**
  `_run_profile_purges` records what is still owed, and it is called from
  `App.__init__` and from the five-minute heartbeat - so while a purge is
  deferred the app rewrites the user's settings file without the user asking,
  which nothing else in it does. It is well guarded: `_persist_pending_purges`
  early-returns when neither list has changed, so the steady state (two empty
  lists) writes nothing and a normal start writes nothing. What a write costs
  is that `Config.save()` serialises the whole model, so a key an older or
  newer build wrote that this one does not model is dropped, and a concurrent
  hand-edit is overwritten. **1.3.1+cfa.6 put a second deferral list through
  the same writer** - `pending_data_clears`, for "Clear all browser data" -
  so there are now two reasons the app writes `config.json` unasked, through
  one helper and one `Config.save()` per drain. The alternative was leaving
  that list in memory, which loses a live account's session cookie to a quit
  inside the deferral window, and the write is the cheaper of the two.
  A third writer is still worth thinking twice about. **It was made atomic
  in 1.3.2+cfa.7** - `atomic_write.py`, a same-directory temp plus `fsync`
  plus `os.replace`, the helper `secrets.dat` and the meter catalog already
  used - so a crash or a power cut inside one of those unasked writes no
  longer truncates the file that holds every setting. It moved out of
  `secret_storage` to get there: that module imports `config`, so `config`
  cannot import it. One consequence to know about: the file is now `0600` on
  POSIX rather than `0644`, because `mkstemp` creates at `0600` and
  `os.replace` carries the temp file's mode across. (It still means
  an ad-hoc harness that
  drives `_run_profile_purges` must set `APPDATA` - an override on every OS,
  which `tests/conftest.py` sets for the suite - or it edits the developer's
  real config.)
- ~~**The log summariser's shared budget does not cover `repr()` values or
  large numbers, and `_raw_summary` catches only `TypeError`.**~~ **Closed in
  1.3.1+cfa.6**, with one correction to the fix sketched here:
  `len(str(value))` is itself the crash, because CPython 3.11+ raises
  `ValueError` on `str()` of an int over 4 300 digits and `json.dumps` hits
  the same limit from the inside. The length is estimated from
  `bit_length()` and a number past `_LOG_VALUE_LIMIT` digits never reaches
  the serialiser at all. The `repr()` fallback is wrapped and clipped, the
  key walk is guarded in both functions that do it (`_raw_keys_for_log` is
  evaluated in the same log call and would have raised first), and
  `_raw_summary` catches `Exception` with a *bounded* literal - `repr(raw)`
  was the unbounded thing it exists to prevent. Measured before and after:
  5 MB `bytes` 5 000 012 → 312 characters, fifty 4 200-digit integers
  210 440 → 50, and the three raising inputs return a string.
- ~~**The dispatch epoch is matched against the name the payload
  carries.**~~ **Closed in 1.3.1+cfa.6.** `_emit` compares the payload's own
  `provider` with the name the App dispatched and re-stamps it with
  `replace(snap, provider=_name)` when they differ, so the App's own notion
  of what it dispatched is the only thing that can decide which tile is
  touched, and a payload that named something else is logged once - naming
  the dispatched provider and a fixed literal, never the payload's own
  string. The comparison is a `getattr(snap, "provider", _name)` rather than
  an unconditional `replace`, deliberately: `dataclasses.replace` raises on
  anything that is not a dataclass, and a diagnostic must not be the thing
  that breaks a dispatch. What that leaves is a payload object with no
  `provider` attribute at all, which skips the stamp and then raises
  `AttributeError` at `_on_snapshot`'s `name = snapshot.provider` - the same
  failure it had before this change, and no provider in the tree can produce
  it (each one constructs a `UsageSnapshot`).
- **The resume-artifact threshold is still unreachable on an awake
  machine.** The scraper calls a timeout a resume artifact past
  `timeout_ms x max_attempts x RESUME_ARTIFACT_FACTOR`, which for Claude is
  40 x 2 x 3 = 240 s, while the App's watchdog for the same provider is
  160 + 20 s - so the watchdog always wins and the classification only ever
  fires across a real machine suspend, which is what it was written for.
  `self._started_at` is also set once in `__init__` and not reset in
  `_begin_attempt`. Comparing against the App's budget rather than the
  scraper's, and bypassing the retry branch for a classified resume
  artifact, is the rest of the fix; it is scraper *timing* and was left for
  its own change.

  ~~**Two `scraper.py` log lines carry uncapped page text.**~~ **Closed in
  1.3.1+cfa.6**, and there were four title sites rather than the three the
  earlier note counted. Titles clip at 200 and the key list takes
  `raw_keys=`'s shape (50 names of 60, with the true count beside them).
  Measured by driving `_finish` with a 1 MB `document.title` and 10 000 keys
  of 1 000 characters: `scrape ok` 11 079 134 → 3 664 characters and
  `scrape fail` 1 000 367 → 570. `_safe_url` was checked and was already
  bounded at 300. Note that `_load_failure_context` still puts the raw title
  into the *payload*, which is bounded downstream by `_raw_summary` and by
  `error_dialog._sanitize_raw` rather than at source.
- ~~**`CopilotProvider` and `OpenRouterProvider` do not declare
  `refresh_budget_seconds`.**~~ **Closed in 1.3.2+cfa.7**, and the flat 60 s
  turned out not to be about right after all. It was read off the nominal
  sum of per-socket timeouts (10 + 15 + 15), which is not a bound on
  anything; with a real per-call bound the same three calls are 130 s for
  Copilot and 135 s for OpenRouter, so both now name their own.
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

- **300 characters of provider-chosen text still reach the log verbatim.**
  `_error_for_log` bounds an error's length, flattens its line breaks and
  redacts Azure identifiers, but it does not run `_redact_emails`, which the
  error dialog's blob does - so an email, a bearer token or a balance a
  provider page puts in `snapshot.error` is written into the rotating file
  users are asked to attach to bug reports. Base behaves identically; this
  release bounded the cost of that record, not its content. The fix is one
  more call beside `_redact_azure_ids`, on an already-clipped string.
- **Less of a long error reaches the log than before.** The clip-before-
  redact window is 500 characters and the redaction shrinks what it keeps, so
  an error that is nothing but Azure identifiers now produces a ~200-character
  record where redact-then-clip produced 300. It is bounded either way and
  what is lost is identifiers, so it is the price of taking four regex passes
  over a 1.2 MB provider string off the GUI thread (163 ms to 0.08 ms).
- **`scrape fail`'s `error=%s` still has no cap.** Every other argument on
  that record is clipped and this one is not; all five callers pass a fixed
  literal (`"timeout"`, `"extractor retry limit exceeded"`), so nothing
  unbounded reaches it today. It bites the first time someone passes an
  exception's text there; the fix is the `_clip` helper already beside it.
- **200 characters of page-chosen title reach the log and 2 000 the
  clipboard.** A 1.45 MB `document.title` gives a 968-character log record
  and a 2 540-character copy-diagnostics blob with the title sanitized to
  2 012; a planted email is redacted there and an API-key-shaped string is
  not. Identical at base and unchanged by this release.
- **"Clear all browser data" is linear in the raw `profiles/` entry count, on
  the GUI thread.** One `resolve()` and one keyring write per entry: 12
  entries cost 5 ms of click, 1 002 cost 73 ms and 10 002 cost 714 ms with
  the keyring stubbed, and on Windows each of those writes decrypts and
  rewrites the whole secrets file. A real install has a handful of
  directories; the fix is to hoist the resolved root out of the loop and to
  skip names no keyring can hold.
- **`_begin_cycle` counts names that are not configured providers.**
  `requested = len(names)` is taken before the filter and `requested >
  len(dispatched)` is what marks a cycle partial, so a caller passing a stale
  name would make every cycle look partial and stop the idle backoff
  advancing. Unreachable today - every caller filters through `_providers` -
  and the fix is `len(set(names) & self._providers.keys())`.
- **A queued sign-in is answered by the settings save's refresh, not its
  own.** `_run_pending_manual` tests `full` first, so a queued full refresh
  discards `_pending_manual_providers` entirely; 1.3.1+cfa.6 made the tile
  speak for the user in that case but the per-provider refresh still does not
  run as itself. The full cycle covers that provider anyway, so the loss is
  ordering; the fix is to move the queued names to the front of the full
  cycle's order.
- **`_is_abandoned` mutates state from inside a predicate.** It pops the
  `_abandoned` entry and logs `abandoned worker assumed dead` once the
  deadline has passed, and `_purge_blocked_reason` calls it at every
  five-minute heartbeat - so *asking* whether a profile can be deleted can
  un-park a provider and write a WARNING. Correct wherever it happens; a pure
  `_is_abandoned_at(now)` for the predicate is the tidy version.
- **`providers/catalog.py` still imports the private
  `config._is_safe_profile_id`.** The settings dialog was given the public
  `is_usable_profile_id`; the catalog wants the *name* rule only, which is
  what it takes, so the fix is to publish `is_safe_profile_id` beside it
  rather than route the catalog through a predicate that touches the
  filesystem.
- **Three `inspect.getsource` assertions remain in the suite.**
  `test_app_logging.py`, `test_scraper.py` and `test_api_capture.py` each
  still grep a function's source. The two that were load-bearing were
  converted to behavioural tests; none of these three is the sole killer of
  any mutation, so they are redundancy rather than a gap, and they bite when
  someone moves the code they grep for.
- **`test_a_parked_provider_does_not_freeze_the_idle_backoff` is only
  measured at REST-sized budgets.** Its floor is derived from the run's own
  arithmetic now, so a re-tuned budget moves it - but at a 900 s budget the
  *ceiling* itself stops holding: the wedged run makes 31 dispatches against
  a control of 12 over the same six hours, because a watchdog wait longer
  than the cycle spacing keeps `_unchanged_cycles` down and the app on its
  short cadence. The quantity that governs that fixture is the **watchdog
  wait**, not the budget: `refresh_budget_seconds` plus
  `_WATCHDOG_SLACK_SECONDS` plus `_pool_wait_slack`. At pool capacity 1
  that is **780 s for all three REST providers** after 1.3.2+cfa.7 (openrouter
  and copilot are now in the same band as azure, which they were not before),
  so the margin against the 900 s at which the ceiling stops holding is
  **120 s, not 405**. Azure's 495 s budget is the smaller number and not the
  one to compare. It is still a note about the fixture's reach rather than a
  live rate defect: the 60-seed six-fake-hour fuzz was re-run with the real
  budgets rather than the fixture's 60/60/90, and is clean - no dead
  scheduler, no runaway, no double browser scrape, nothing dispatched while
  parked, worst concurrent REST workers 5/6/5 - with the median total
  dispatch rate going *down*, 55.3/h to 50.7/h. Bigger budgets cost the
  invariants nothing; what they cost is how long a wedged tile shows its last
  state.
- ~~**`SECURITY.md` does not describe "Clear all browser data".**~~
  **Closed in 1.3.2+cfa.7**, under its own heading beside the local-data one.
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
candidate span against a list of every claim already made, so 400 KB of
seven-character email addresses separated by spaces — a contact list, not an
attack — is 57 143 findings and took 58.0 s in `scan()`
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

One finding of the round-3 *confirmation* pass is closed in 1.3.2+cfa.7:
mutation S6a - dropping `.lower()` from the deny-glob compile side - survived
the whole suite, because every built-in deny glob is already lower-case and
lowering it again changes nothing. `paths.deny` is operator-editable and this
repository is told below to edit it, so eight parametrised cases now cover
both halves of the fold. Run both ways against `tests/test_egress_guard.py`
(460 tests): pattern side unlowered, 4 fail; candidate side unlowered, 5 fail,
one of which is the pre-existing `C:\Users\m\.AWS\CREDENTIALS` case.

What this round leaves open, deliberately:

- **Two overlapping redactions still resolve to one of them, not to their
  union.** An `api_key=` assignment whose value runs straight into an email
  address is dispatched as `[redacted:secret-assignment]` followed by the tail
  of that address: the credential-shaped part is removed and the domain
  survives. Merging the spans would be
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


---

## 10. Upstream contribution — after the next batch

> **A plan, not a task.** Nothing below is started and nothing is due. It is
> written down so the shape of it does not have to be reconstructed later.

The maintainer intends to offer the generic half of this fork back to
[upstream](https://github.com/jpajak/ai-gauge) as clean pull requests. **Not
before October 2026**, and not before the next batch of work lands here: a PR
opened against a tree that is still moving is a PR that gets rebased more than
it gets reviewed.

**The shape is a fork of the current build with everything proprietary
stripped**, not a series of cherry-picks off this branch. The things that come
out:

- every CFA Societies Canada / `cfacanada.org` reference,
- the `+cfa.N` version framing and `tools/check_versions.py`'s enforcement of
  it (upstream has its own scheme and this one would fail their build),
- `AI Gauge-datasheet.md`,
- this file and `docs/opencode-plugin-evaluation.md`,
- the audit provenance: `SECURITY-AUDIT.md`, `audit-artifacts/`, the
  `[tool.ai-gauge-audit]` block in `pyproject.toml`, and the "Relationship to
  upstream" section of the README,
- the screenshots, which are of real accounts with real numbers,
- the issue templates, which ask for this fork's version string,
- tenant and organisation strings in the tests.

**The candidates, in the order they are worth offering:**

1. **The transport bound** (`providers/_http.py`). The largest and the most
   generally useful: a REST call that returns or raises within a declared
   budget rather than within a per-socket timeout. Self-contained, and the
   1.3.2+cfa.7 changelog entry is most of its PR description already.
2. **The atomic config save** (`atomic_write.py` plus `Config.save`). Small,
   obviously correct, and fixes a real way to lose every setting.
3. **The scheduler work**: the per-provider error retry, the dispatch epoch and
   watchdog, and the adaptive cadence. Bigger and more opinionated; worth
   splitting.
4. **The meter catalog** (`providers/catalog.py` and the weekly self-scan).
   The largest behavioural addition and the one most likely to want discussion
   about whether upstream wants that shape at all.
5. **The Azure provider.** Whole and new; it depends on 1 and probably on 4.

What does *not* travel: anything that only makes sense against this fork's
threat model or its release process. The egress guard is the obvious case —
it is a development-time tool for this tree's agents, not a feature of the app.
