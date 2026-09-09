# Next session — parked items

State at close of the 2026-08-10 session. `main` is `1.0.0+cfa.2` at PRs #6–#16,
610 tests passing, all five providers reading.

> **Updated for `1.1.0+cfa.3`.** The Claude/Codex label definitions moved out of
> the extractors into a meter catalog (§7), and the Codex polarity defect that
> headed §4 is fixed. Everything else below stands as written.

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
| `webview/scraper.py` → timeout | Wall-clock, so it does not account for system sleep. A laptop resumed after two days reported `elapsed_s: 228477` and fired a stale scrape per provider. | Cosmetic in effect — the resumed cycle fails and recovers — but it produces one spurious failure per provider on every resume, and nonsense elapsed values in the log. |
| `app.py` → `_error_retry_time` | The fast retry is **cycle-wide, not per-provider**. One permanently-failing provider makes every cycle count as failing, so healthy providers get refreshed every minute too until the bound engages — and once it has engaged (the counter never resets, because no cycle is ever clean), a genuinely transient failure on a *different* provider gets no fast retry at all. | Inherited shape: the pre-existing `_stale_error_retry_time` was cycle-wide too. The blast radius grew because any error now triggers it rather than only errors carrying stale metrics. Bounded at three cycles, so the cost is finite. Per-provider retry is the right fix and a bigger change than a close-out warranted. |
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
2. **The refresh queue is strictly serial.** `app._start_next_refresh` returns
   early if anything is in flight. The providers are independent; parallelising
   turns 40–70s into roughly the slowest single provider.
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
