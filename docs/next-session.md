# Next session — parked items

State at close of the 2026-08-10 session. `main` is `1.0.0+cfa.2` at PRs #6–#16,
610 tests passing, all five providers reading.

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
| `providers/codex.py` → `readCard` | Scans the whole card for used/remaining, so a bare percentage resolves to *used*. Same bug class as Claude's polarity defect. | Codex works today, its structure differs (it takes the *first* percentage and has a text-window fallback that can pull wording from adjacent cards), and its live page is not observable from the dev environment. Changing a working provider on a guess is what caused the previous breakage. |
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
