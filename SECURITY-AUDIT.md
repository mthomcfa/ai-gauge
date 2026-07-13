# Security Audit — AI Gauge 0.6.3

- **Scope:** full source tree at commit `1df4536` (branch base), all 11,222 lines of Python, both GitHub workflows, both build scripts, `pyproject.toml`, and all injected JavaScript.
- **Method:** complete manual read of every source file (no sampling), cross-checked with `bandit`, `pip-audit`, a PyPI canonical-name check of every dependency, and the full test suite (263 tests passing before and after fixes). Static + in-container analysis only; the GUI was not run and Windows-only code paths (DPAPI, schtasks) were not executed.
- **Threat model:** user installing on a personal Windows machine; app handles live claude.ai/ChatGPT session cookies, a GitHub PAT, and OpenRouter API keys.

## Verdict: **SAFE TO INSTALL** (from source, at this branch) — with eyes open about the residual risks below

No malicious, hidden, or undisclosed behavior was found anywhere in the codebase. Specifically ruled out by direct inspection:

- **No undisclosed egress.** Every network destination is a documented first-party provider endpoint (complete map below). There is no telemetry, no analytics, no update pinger, no crash reporter, and no request that carries more than the credential needed for that provider.
- **No code-execution surface.** No `eval`/`exec`/`pickle`/`marshal`/yaml, no dynamic imports of remote code, no `shell=True`, no custom URL-scheme/deep-link handler. All subprocess calls are fixed-argv OS utilities (`schtasks`, `launchctl`, `explorer`/`open`/`xdg-open`, and self-relaunch).
- **No credential exfiltration.** Cookies/PAT/keys flow only: paste/sign-in → DPAPI file or OS keyring → provider's own domain. The injected scraping JS only *reads* rendered usage text and returns it to Python; it never touches `document.cookie`, storage, or form fields, and cookies are injected scoped to the matching provider domain only. Log statements record cookie *names* and sanitized URLs (query/fragment stripped) — never values.

The findings that drive the verdict:

1. **The absence of any hostile or covert behavior** across the entire tree (the decisive positive finding).
2. **AG-01/AG-02 (secret-storage hardening)** — real weaknesses, now fixed on this branch, but they were failure-handling/hardening gaps, not exfiltration paths.
3. **AG-06 (unsigned releases, no provenance)** — the published binaries cannot be cryptographically tied to this audited source. **Install by building from source (or `pip install -e .` + `python -m aigauge`) rather than downloading the release zip**, or accept that the zip's contents are unverifiable beyond a SHA256 the same CI produced.

## Findings

| ID | Severity | Location | Defect | Attack scenario → outcome | Fix |
|----|----------|----------|--------|---------------------------|-----|
| AG-01 | **Medium** | `src/aigauge/secret_storage.py:102-117` | `AIGAUGE_ALLOW_PLAINTEXT_SECRETS=1` gated only *writes*; on non-Windows an existing plaintext `secrets.dat` was read and trusted with no opt-in. | On macOS/Linux, anything able to drop a file in the app-data dir (sync tooling, another app's bug) plants `secrets.dat` → app silently adopts attacker-chosen session cookies (session fixation into the embedded browser). | **Fixed** (`929018c`): read path now requires the same env-var opt-in; dev plaintext file created `0600`. |
| AG-02 | **Medium** | `src/aigauge/secret_storage.py:116-117, 80, 91` | All decrypt/parse failures swallowed (`except Exception: return {}`); DPAPI called with `dwFlags=0` (UI prompts allowed). | DPAPI decrypt failure (credential rotation, copied blob, tampering) is indistinguishable from "no secrets": user silently signed out and the next save destroys the evidence; a DPAPI UI prompt from a tray app would hang invisibly. | **Fixed** (`1a086ac`): failures logged; `CRYPTPROTECT_UI_FORBIDDEN` passed to both calls. |
| AG-03 | **Medium** | `src/aigauge/webview/login_window.py:93-94` | Sign-in browser's host allowlist waved through `data:` top-frame navigations. | Open redirect / compromised provider page navigates the chrome-less (no address bar) sign-in window to a `data:` URL → pixel-perfect credential phishing page indistinguishable from the real login. | **Fixed** (`2fdcb4d`): `data:` main-frame navigation blocked; `about:`/`blob:` kept. |
| AG-04 | **Medium** | `.github/workflows/release.yml`, `test.yml` | All actions pinned by mutable tag (incl. third-party `softprops/action-gh-release@v2`); `contents: write` granted to every release-workflow job; `test.yml` had no `permissions` block. | Compromise of any tagged action repo silently swaps the code that builds and publishes the shipped binary — a credential-stealing build would carry the project's own release checksum. | **Fixed** (`e152307`): every action pinned to a full commit SHA; token dropped to `contents: read` everywhere except the one job that creates the draft release. |
| AG-05 | **Low** | `src/aigauge/config.py:111-114`, `providers/opencode_go.py:17`, `webview/verify.py:35` | The developer's personal OpenCode workspace URL (`wrk_01KX3HT8MFWCMHR2289KGPZ1RD`) is baked in as the default `usage_url` and verify target. | User enables OpenCode without configuring a URL → their authenticated embedded browser navigates to the developer's workspace path (fails/403s; confusing, and leaks the dev's workspace ID — no user data leaves the machine). | Not fixed (maintainer decision): default should be empty with an explicit "configure your workspace URL" state. Provider is off by default; app UI passes the configured URL everywhere. |
| AG-06 | **Low** | `.github/workflows/release.yml` (releases) | Release binaries are unsigned and carry no build provenance/attestation; the SHA256 sums are produced by the same pipeline that could be compromised. | A compromised CI or release account ships a trojaned zip that passes its own checksum. | Recommend `actions/attest-build-provenance` (or Sigstore) + eventual code signing. Mitigation today: build from source. |
| AG-07 | **Low** | `pyproject.toml:23-30` | Runtime deps use floating ranges (`PyQt6>=6.7,<7` …) and there is no lockfile, so each tag build resolves whatever PyPI serves that day. | A compromised future release of any dependency flows straight into the shipped binary with no review step. (Today's resolution audited clean: `pip-audit` zero findings; all 11 deps are the canonical PyPI packages from their expected maintainers — no typosquats.) | Recommend a constraints/lock file consumed by CI with `--require-hashes`. |
| AG-08 | **Low** | `src/aigauge/error_dialog.py:44-52`, providers' `raw["body_text"]` | "Copy diagnostics" serializes `snapshot.raw`, which includes up to 8 KB of rendered page text (can contain the account holder's name/email/org shown on the provider page). Never sent anywhere by the app; only leaves the machine if the user pastes it into a bug report. | User shares diagnostics publicly → mild PII disclosure. No credential material is in the payload (page text can't contain HttpOnly cookie values). | Consider redacting email-shaped strings before display. Log files only ever get a 300-char truncated summary. |
| AG-09 | **Low** | `src/aigauge/platforms/windows.py:28-33` | `schtasks.exe` falls back to a PATH-relative name if `%WINDIR%`/`%SystemRoot%` are unset. | Requires an attacker who already controls the process environment/PATH — i.e., same-user code execution, which already means game over for DPAPI secrets. Defense-in-depth only. | Optional: hard-fail instead of falling back. |
| AG-10 | **Info** | `webview/profile.py:17-21`, `login_window.py:24-46` | Spoofed Chrome/Windows User-Agent on all platforms; sign-in allowlist includes broad SSO suffixes (`google.com`, `apple.com`, `microsoft.com`, `live.com`, `github.com`, `youtube.com`), and *subresources* are intentionally unfiltered. | Not a vulnerability. UA spoofing is a provider-ToS/fingerprinting matter; the allowlist breadth is required for real SSO flows and applies to main-frame loads in a user-visible window. | None required. |

Scanner corroboration: `bandit` reported 27 Low + 1 Medium — all verified as noise against source (the Medium is a B608 "SQL injection" false positive on an f-string *text template* in `tools/wdsi_submission.py:101`; the Lows are the subprocess/try-except-pass sites analyzed above). `pip-audit`: no vulnerable project dependencies (only the container's own `pip` flagged).

## Complete network egress map

Direct HTTPS via `requests` (only these, verified by exhaustive grep — no other network API is used anywhere):

| Host | Endpoints | Credential sent | Purpose |
|------|-----------|-----------------|---------|
| `api.github.com` | `/user`, `/users/{u}/settings/billing/premium_request/usage`, `/users/{u}/settings/billing/usage/summary`, `/organizations/{org}/settings/billing/...` | GitHub PAT (Bearer) | Copilot credit/premium-request usage |
| `openrouter.ai` | `/api/v1/credits`, `/api/v1/key`, `/api/v1/activity` | OpenRouter mgmt/inference key (Bearer) | Balance, key spend, model activity |

Embedded QtWebEngine (page loads with the user's session cookies; scraping JS reads rendered usage text only):

| Host | Purpose |
|------|---------|
| `claude.ai` | usage page scrape + sign-in |
| `chatgpt.com` | Codex analytics scrape + sign-in |
| `opencode.ai` | workspace usage scrape + sign-in (URL user-configurable — the user can point this at any URL; the sign-in window is still allowlist-restricted) |
| `anthropic.com`, `openai.com`, `oaistatic.com`, `oaiusercontent.com`, `auth0.com`, `google.com`, `youtube.com`, `appleid.apple.com`, `apple.com`, `icloud.com`, `microsoftonline.com`, `microsoft.com`, `live.com`, `github.com` | sign-in window main-frame allowlist for provider SSO flows only |

Additionally: provider pages themselves load their own third-party subresources (Cloudflare challenges, Datadog RUM, analytics iframes) inside the sandboxed webview — that is the providers' content, not the app's, and the app filters the resulting console noise rather than sending anything. `QDesktopServices.openUrl` opens `github.com`/`openrouter.ai`/provider usage pages in the user's default browser (no app egress). **There is no other outbound traffic: no telemetry, no update check, no crash reporting.**

## Windows persistence & platform integration

- **Mechanism:** per-user Task Scheduler entry `"AI Gauge"` (not a Run key, not the Startup folder), created via `schtasks /Create /XML` with a generated XML: `LogonType=InteractiveToken`, `RunLevel=LeastPrivilege`, user-scoped trigger, no stored password, `Hidden=false`. Removed on toggle-off. Opt-in only (Settings checkbox). Exactly matches what the README/Defender-submission docs claim.
- **Hijackability:** the task runs `sys.executable` — wherever the user extracted the app. Any same-user process could edit either the task or the binary; this is inherent to every non-admin per-user install, not specific to this app.
- **App data:** `%APPDATA%/ai-gauge` inherits the standard user-only ACL; `secrets.dat` (DPAPI blob), `config.json` (no secrets — verified field-by-field), logs, history/ratio JSON (usage percentages only), and `profiles/{account-id}/` QtWebEngine profiles.

## Residual risks — could NOT be ruled out / not fixable

1. **DPAPI/keyring user-scope (inherent, non-fixable):** any process already running as your Windows user can call `CryptUnprotectData` on `secrets.dat`, read Credential Manager entries, and decrypt the QtWebEngine profile cookie store. The app cannot protect its secrets from same-user malware; no desktop app can. SECURITY.md discloses this honestly.
2. **Live cookies also live in the Chromium profile:** independent of `secrets.dat`, the embedded browser persists rotated session cookies under `profiles/{account-id}/` (Chromium's own at-rest encryption, still same-user-decryptable).
3. **Embedded Chromium patch lag:** PyQt6-WebEngine's Chromium trails Chrome's security patches by weeks-to-months, and this engine renders live provider pages while holding your sessions. Keep the dependency current.
4. **Binary supply chain not byte-verified:** the published release zips could not be compared byte-for-byte against a source build from this container, and PyQt6/PyQt6-WebEngine wheels are large binary blobs audited by provenance (canonical Riverbank packages), not content. Building from source removes the first gap, not the second.
5. **Runtime-only behavior:** the GUI was not executed and Windows-specific paths (DPAPI, schtasks) ran only under test doubles. Static reading of these paths was exhaustive, but dynamic Windows verification was out of reach in this Linux container.
6. **Upstream trust:** upstream `jpajak/ai-gauge` is a young, single-author, low-prevalence project (9 stars; latest release v0.6.3 on 2026-07-10 matches this source's version). Nothing in its history here looks anomalous, but a small project means a small blast-radius-to-detection ratio — pin to an audited commit (such as this branch) rather than tracking `main`.

## Fixes applied on this branch

| Commit | Finding |
|--------|---------|
| `929018c` | AG-01 — plaintext `secrets.dat` read gate + `0600` perms |
| `1a086ac` | AG-02 — log DPAPI failures, `CRYPTPROTECT_UI_FORBIDDEN` |
| `2fdcb4d` | AG-03 — block `data:` top-frame navigation in sign-in window |
| `e152307` | AG-04 — SHA-pin actions, least-privilege `GITHUB_TOKEN` |

Post-fix verification: full test suite 265 passed (263 baseline + 2 new regression tests), `bandit` unchanged (no new findings), `pip-audit` clean.
