# Security Audit — AI Gauge 0.6.3

- **Scope:** full source tree (all ~11.2k lines of Python), both GitHub workflows, both build scripts, `pyproject.toml`, and all injected JavaScript.
- **Method:** complete manual read of every source file (no sampling), a two-engine review (Claude + Codex) over the branch diff, plus `bandit`, `pip-audit`, a PyPI canonical-name check of every dependency, and the full test suite. Static + in-container analysis only; the GUI was not run and Windows-only code paths (DPAPI, `schtasks`, `icacls`) executed only under test doubles.
- **Threat model:** a user installing on a personal Windows machine; the app handles live claude.ai/ChatGPT session cookies, a GitHub PAT, and OpenRouter API keys.

## Reconciled verdict: **SAFE TO INSTALL — from source, at this branch**

The two-engine audit agreed: **the source is clean.** No malicious, covert, or undisclosed behavior exists anywhere in the codebase — no undisclosed egress, no telemetry/update-ping/crash-reporter, no code-execution surface (`eval`/`exec`/`pickle`/`shell=True`/deep-links are all absent), and no credential exfiltration path (cookies/PAT/keys flow only to the OS keyring/DPAPI and to each provider's own domain; the scraping JS reads rendered usage text only and never touches `document.cookie`).

What the audit surfaced was **hardening work**, not backdoors. All confirmed findings are now fixed on this branch (table below). Two decisions gate the verdict:

1. **Build from source, don't trust the unsigned release.** The published release zips are unsigned and, until the provenance attestation added here lands in a tagged build, cannot be cryptographically tied to this audited source. Install via `pip install -e .` + `python -m aigauge` from this branch, or accept that a downloaded zip's contents are unverifiable.
2. **The DPAPI/keyring same-user residual is inherent and unfixable.** Any process already running as your Windows user can decrypt `secrets.dat`, read Credential Manager, and read the embedded browser's cookie store. No desktop app can prevent this; the app's own SECURITY.md discloses it honestly.

## Findings

Severity, `file:line` (at the fixed state), the defect, the concrete bad outcome, and the commit that closed it.

| ID | Severity | Location | Defect → concrete outcome | Status / commit |
|----|----------|----------|---------------------------|-----------------|
| AG-01 | **Medium** | `secret_storage.py:_load_all` | `AIGAUGE_ALLOW_PLAINTEXT_SECRETS` gated only writes; a planted plaintext `secrets.dat` on macOS/Linux was read and trusted → attacker-chosen session cookies adopted (session fixation). | **Fixed** `929018c` |
| AG-02 | **Medium** | `secret_storage.py:_load_all`, `_protect`/`_unprotect` | Decrypt/parse failures swallowed silently; DPAPI called with `dwFlags=0` → user silently signed out, evidence overwritten, and a DPAPI UI prompt could hang the tray app. | **Fixed** `1a086ac` (+ quarantine in `fabe62b`) |
| AG-03 | **Medium** | `webview/login_window.py:acceptNavigationRequest` | Sign-in window allowlist waved through `data:` (and, until the follow-up, `blob:`) top-frame navigations → full-window credential phishing in a chrome-less window. | **Fixed** `2fdcb4d`, `249ad5e` |
| AG-04 | **Medium** | `.github/workflows/*.yml` | Actions pinned by mutable tag (incl. third-party `action-gh-release`); `contents: write` on every release job → a compromised action could publish a trojaned binary with the project's own checksum. | **Fixed** `e152307` |
| AG-11 | **Medium** | `settings_dialog.py:apply_to` (removal path) | Removing an account cleared only the stored cookie blob; its whole QtWebEngine profile — including Chromium's own persisted live session cookies and cache — stayed on disk under `profiles/<id>/`. | **Fixed** `67aa7bc` |
| AG-12 | **Medium** | `webview/cookies.py:_set_cookie`, `_parse_cookie_pairs` | The **entire pasted header** was retained for OpenCode → any foreign analytics/tracking cookie the user copied was injected into the profile. (OpenCode is intentionally non-HttpOnly: its SPA reads the session cookie to hydrate — an upstream 0.6.3 decision left in place and commented; the injected-cookie allowlist is the real fix.) | **Fixed** `ef897ab`, `07e99f8` |
| AG-13 | **Medium** | `config.py` / `providers/opencode_go.py` (`usage_url`) | OpenCode usage URL was an unvalidated string loaded into the signed-in browser → a poisoned config could point the authenticated session at `file:`/`data:`/an attacker host. | **Fixed** `b613f3c` |
| AG-14 | **Medium** | `config.py:BrowserAccount.id` → `webview_profile_dir` | Account id flowed unvalidated into `profiles/<id>` → a poisoned config id (`../../evil`) redirected profile create/open/delete outside the app-data tree. | **Fixed** `a1b423b` |
| AG-05 | **Low** | `config.py`, `providers/opencode_go.py:17`, `webview/verify.py:35` | Developer's personal OpenCode workspace URL hardcoded as the default → an unconfigured user's authenticated browser navigates to the dev's workspace path (fails; leaks the dev's workspace id — no user data leaves the machine). | Open (maintainer call; provider off by default) |
| AG-06 | **Low** | `.github/workflows/release.yml` | Release binaries unsigned with no build provenance; SHA256 sums produced by the same pipeline. | **Partially fixed** `e83041b` (provenance attestation added; code signing still recommended) |
| AG-07 | **Low** | `pyproject.toml`, `build.ps1`/`build.sh` | Floating dependency ranges, no lockfile, and build scripts installed **unpinned** PyInstaller at build time. | **Partially fixed** `e83041b` (PyInstaller pinned; full `--require-hashes` lock deferred, see residual #4) |
| AG-08 | **Low** | `error_dialog.py:_format_diagnostics` | "Copy diagnostics" serialized up to ~8 KB of scraped page text (account name/email/org) verbatim to the clipboard. | **Fixed** `59218b0` |
| AG-09 | **Low** | `platforms/windows.py`, `secret_storage.py` | `schtasks`/`icacls` fell back to PATH-relative names if `%WINDIR%` unset (defense-in-depth; needs pre-existing same-user code exec). | Improved: `icacls` now resolved to System32 (`ae59a82`); `schtasks` unchanged (pre-existing). |
| AG-10 | **Info** | `webview/profile.py`, `login_window.py` | Spoofed Chrome UA; broad SSO allowlist; subresources unfiltered. | Not a vulnerability — required for SSO; no action. |

Scanner corroboration (post-fix): `bandit` = 30 Low + 1 Medium, **0 High** — all verified noise (the Medium is a B608 false positive on an f-string *text template* in `tools/wdsi_submission.py`; the Lows are fixed-argv subprocess/try-except-pass sites). `pip-audit` = no vulnerable project dependencies. All 11 declared dependencies are the canonical PyPI packages from their expected maintainers — no typosquats.

## Complete network egress map

Direct HTTPS via `requests` (the only network API used anywhere — verified by exhaustive grep):

| Host | Endpoints | Credential | Purpose |
|------|-----------|-----------|---------|
| `api.github.com` | `/user`, `/users/{u}/settings/billing/premium_request/usage`, `/users/{u}/settings/billing/usage/summary`, `/organizations/{org}/settings/billing/...` | GitHub PAT (Bearer) | Copilot credit / premium-request usage |
| `openrouter.ai` | `/api/v1/credits`, `/api/v1/key`, `/api/v1/activity` | OpenRouter key (Bearer) | Balance, key spend, model activity |

Embedded QtWebEngine (page loads carrying the user's session cookies; scraping JS reads rendered usage text only):

| Host | Purpose |
|------|---------|
| `claude.ai` | usage-page scrape + sign-in |
| `chatgpt.com` | Codex analytics scrape + sign-in |
| `opencode.ai` | workspace usage scrape + sign-in (URL now validated to `https` on `opencode.ai` only) |
| `anthropic.com`, `openai.com`, `oaistatic.com`, `oaiusercontent.com`, `auth0.com`, `google.com`, `youtube.com`, `appleid.apple.com`, `apple.com`, `icloud.com`, `microsoftonline.com`, `microsoft.com`, `live.com`, `github.com` | sign-in-window main-frame allowlist for provider SSO only |

Provider pages load their own third-party subresources (Cloudflare, Datadog RUM, analytics iframes) inside the sandboxed webview — that is the providers' content, not the app's. `QDesktopServices.openUrl` opens provider/usage pages in the user's default browser (no app egress). **There is no other outbound traffic: no telemetry, no update check, no crash reporting.**

## Windows persistence & platform integration

Per-user Task Scheduler entry `"AI Gauge"` (not a Run key / Startup folder), `LogonType=InteractiveToken`, `RunLevel=LeastPrivilege`, user-scoped, `Hidden=false`, opt-in only, removed on toggle-off — exactly as the README/Defender-submission docs claim. It runs `sys.executable`; any same-user process could edit the task or binary, inherent to every non-admin per-user install. App data lives under the standard user-only-ACL `%APPDATA%/ai-gauge`; `secrets.dat` now also gets an explicit owner-only DACL.

## Residual risks — could NOT be ruled out / not fixable

1. **DPAPI/keyring same-user scope (inherent, non-fixable):** any process running as your Windows user can decrypt `secrets.dat`, read Credential Manager, and decrypt the QtWebEngine cookie store. The app cannot protect its secrets from same-user malware.
2. **Live cookies also persist in the Chromium profile:** independent of `secrets.dat`, the embedded browser stores rotated session cookies under `profiles/<id>/` (Chromium's own at-rest encryption, still same-user-decryptable). Account removal and "Clear all browser data" now delete these.
3. **Embedded Chromium patch lag:** PyQt6-WebEngine's Chromium trails Chrome's security patches; it renders live provider pages while holding your sessions. Keep the dependency current.
4. **Full dependency hash-pinning deferred:** PyInstaller is now version-pinned and release artifacts get a provenance attestation, but a complete `pip install --require-hashes` lock was **not** shipped — a single hashed lock can't cover the win/mac/linux matrix (platform-only deps differ), and per-OS locks could not be generated or verified from this audit container. Recommended follow-up: generate per-platform hashed locks in CI.
5. **Binary supply chain not byte-verified:** the published release zips were not compared byte-for-byte against a source build, and the PyQt6/WebEngine wheels are large binaries trusted by provenance, not content review. Building from source removes the first gap, not the second.
6. **Runtime-only behavior:** the GUI was not executed; Windows DPAPI/`schtasks`/`icacls` paths ran only under test doubles. Static reading was exhaustive; dynamic Windows verification was out of reach in a Linux container.

## Fixes applied on this branch (commit map)

| Commit | Finding |
|--------|---------|
| `929018c` | AG-01 — plaintext `secrets.dat` read gate + `0600` perms |
| `1a086ac` | AG-02 — log DPAPI failures, `CRYPTPROTECT_UI_FORBIDDEN` |
| `2fdcb4d` | AG-03 — block `data:` top-frame navigation in sign-in window |
| `e152307` | AG-04 — SHA-pin actions, least-privilege `GITHUB_TOKEN` |
| `a1b423b` | AG-14 — validate account/profile ids against traversal |
| `67aa7bc` | AG-11 — delete the WebEngine profile on account removal + "Clear all browser data" |
| `ef897ab`, `07e99f8` | AG-12 — allowlist OpenCode cookies (OpenCode stays script-readable for SPA hydration) |
| `b613f3c` | AG-13 — validate the OpenCode usage URL before load |
| `fabe62b` | AG-02 — atomic writes, quarantine corrupt files, Windows DACL |
| `59218b0` | AG-08 — redact emails / truncate page text in diagnostics |
| `e83041b` | AG-06/AG-07 — build provenance, pin PyInstaller, fork+audit metadata |
| `ae59a82` | AG-09 — resolve `icacls` to System32 |
| `249ad5e` | AG-03 follow-up — block `blob:` main-frame navigation |
| `e6ea6fd` | review follow-up — close temp fd on all paths in `_atomic_write` |
| `8ed065f` | AG-11 follow-up — warn when a profile dir only partially deletes (Windows locks) |
| `daf0a06` | AG-13 follow-up — reject control chars/backslashes in the OpenCode URL |
| `f8d1206` | 3-way gate — a rejected config field no longer wipes the whole config |
| `f398181` | 3-way gate — bound quarantine files; reject Windows device-name ids |
| `cef527e` | 3-way gate — validate OpenCode URL before "Open usage in browser" |

## Self-review — three-way adversarial gate

Three independent adversarial passes ran over the full branch diff:

1. **Claude Opus, verify-each-fix** — attempted a bypass of every control.
2. **Claude Opus, regression hunt** — import cycles, use-after-free, fd leaks, logic bugs.
3. **Fable, fresh skeptic** — brute-forced the OpenCode URL validator across the codepoint range (IDN/homograph/fullwidth-dot/zero-width/percent-encoding/userinfo/port), symlink+traversal on `purge_profile`, host-suffix spoofing, and cookie-name tricks.

(A GitHub Copilot review — OpenAI/GPT-family — was also requested on the PR as a fourth, non-Claude engine.)

**No security control was defeated by any pass.** The three follow-ups from passes 1–2 (`blob:` main-frame gap — the most material, since the `data:` block had left the same opaque-origin phishing surface open — plus the `_atomic_write` fd-leak and the Windows partial-delete warning) were applied. Pass 3 then found one **functional regression the first two missed and this document originally overstated away**: a single config field rejected by the new strict validators bubbled out of `Config.load()`'s blanket `except` and reset the *entire* config, so an upgrading user with a custom OpenCode URL would silently lose all other settings. Fixed by coercing/dropping the bad field per-field instead (commit `f8d1206`), with tests asserting sibling settings survive. Pass 3's two LOW items (unbounded quarantine files on roaming `%APPDATA%`; Windows reserved device-name ids) and one NOTE (unvalidated URL in "Open usage in browser") were also fixed (`f398181`, `cef527e`) — so the reserved-device-name residual is now closed rather than accepted.

Post-fix verification: **316 tests pass** (from a 265 baseline; +51 regression tests), `bandit` 0 High, `pip-audit` no vulnerable declared dependencies.

---

## Addendum — audit of the 0.6.5+cfa.1 → 1.0.0+cfa.1 work (2026-08-10)

Scope: the diagnosability, route and API-capture changes made after
`0.6.5+cfa.1`. One exploitable defect was found and fixed; nothing else in the
range changed the security posture.

**The finding.** `webview/api_capture.py` records the shape of JSON the Claude
page fetches, so a provider change can be diagnosed from a real account. It
exposed that record as `window.__ag_api` in the page's **main world**, which is
shared with everything the provider page loads — the app's own bundle,
Cloudflare, RUM, analytics (see "Embedded Browser" above, which already notes
those subresources). Any of them could reassign it.

Reproduced in a real browser before fixing. A page script running

```js
window.__ag_api = {"/evil": {…}};
for (let i = 0; i < 50000; i++) window.__ag_api["flood" + i] = i;
```

replaced the record wholesale. Measured downstream:

| | before | after |
| --- | --- | --- |
| log line | 1,027,859 bytes, against a 512 KiB rotation with 2 backups | 1,098 bytes |
| clipboard | 1,328,044 bytes carrying attacker-chosen text | 4,667 bytes |

**Impact.** Not credential theft and not code execution — nothing from the page
is executed, and the capture has no egress path. The material consequence is
**anti-forensic**: one poisoned scrape rotates the log and three discard the
diagnostic history entirely, and the log is the only artifact that makes a
provider failure explainable. Secondarily, "Copy diagnostics" is what users are
asked to paste into bug reports, so its contents became partly attacker-chosen.

**Fix.** The record moved into a closure behind a non-configurable, getter-only
property that cannot be reassigned or redefined and does not expose the store.
Independently, both consumers of `snapshot.raw` now bound breadth as well as
per-value length. That cap sits at the consumer rather than on a named field:
an earlier cap keyed to `body_text` stopped covering the payload as soon as new
page-supplied fields were added, and would have done so again.

**Also checked, no change needed.** `bandit` over `src/aigauge`: 25 findings,
all LOW, all pre-existing `subprocess` and try/except/pass patterns already
assessed above, **zero in `api_capture.py`**. No `eval`/`exec`/`pickle`/
`shell=True`/`innerHTML` in any changed file. Navigation targets are hard-coded
relative paths, host-pinned and bounded. The only write to a provider origin is
the extractor's own `__ag_route_tries` key in `sessionStorage`.

**Caveat on assurance.** Unlike the original audit, this pass was **not**
corroborated by a second engine. A Codex CLI review was attempted and could not
run: the environment's egress policy denies `api.openai.com`. These findings
are single-source and should be read as such.

Post-fix verification: **602 tests pass**, with each fix mutation-verified —
reverted in isolation, the corresponding tests fail.
