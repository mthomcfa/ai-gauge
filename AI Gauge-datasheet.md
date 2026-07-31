# Product Data Sheet: AI Gauge (security-hardened fork)

> Describes [mthomcfa/ai-gauge](https://github.com/mthomcfa/ai-gauge) at
> **0.6.5+cfa.1**, a security-hardening fork of
> [jpajak/ai-gauge](https://github.com/jpajak/ai-gauge). Fork version numbers
> are its own and do not correspond to upstream releases of the same number.

## Product Summary

AI Gauge is a local desktop utility for monitoring AI service usage across Claude.ai, ChatGPT Codex, GitHub Copilot, OpenRouter, and OpenCode. The implemented app runs as a PyQt6 desktop application with a floating widget on Windows/Linux and a menu-bar item on macOS. It shows provider usage percentages, reset timing, account balance/spend details where available, and refresh status without using a hosted backend or telemetry service.

## Primary Users

- Individual AI power users who pay for multiple subscriptions and want a compact local view of quota/balance status.
- Developers or technical users using Claude, Codex, Copilot, and OpenRouter enough to care about session, weekly, monthly, or spend limits.
- Users comfortable configuring API keys, GitHub personal access tokens, browser sign-in sessions, pasted cookies, and local diagnostics.

## Core Workflows

- Launch the tray/widget utility on Windows/Linux or menu-bar utility on macOS.
- Enable/hide providers and adjust refresh, fade-when-inactive, always-on-top, and start-at-login settings.
- Sign in to Claude or ChatGPT Codex through embedded Chromium, paste cookies as a fallback, and manage multiple named Claude/Codex accounts.
- Configure GitHub Copilot with a fine-grained PAT, optional username/billing organization, and a monthly AI credit allowance.
- Configure OpenRouter with an inference key, optional management key, and optional daily budget.
- Refresh usage manually or through adaptive auto-refresh, then inspect tiles, compact chips, error details, and local logs.

## Implemented Capabilities

- Cross-platform desktop app for Windows, macOS, and Linux, packaged with PyInstaller and runnable from source via `ai-gauge`.
- Floating widget on Windows/Linux, compact pill mode, tray/menu actions, no-tray Linux fallback, and native macOS menu-bar popover.
- Provider tiles for Claude, Codex, GitHub Copilot, OpenRouter, and OpenCode.
- OpenCode usage scraping from a user-configured workspace URL, pinned to `https` on `opencode.ai`, with Rolling, Weekly, and Monthly meters and a cookie-paste setup path for accounts that cannot complete Google sign-in in an embedded browser.
- Per-account configurable gauge colors: each Claude/Codex/OpenCode account, plus Copilot and OpenRouter, can set three severity cutoffs and four band colors, applied consistently to bars, compact chips, the Windows/Linux tray dot, and the macOS menu-bar dots.
- Claude usage scraping from `https://claude.ai/new#settings/usage`, including session and weekly limits.
- Codex usage scraping from `https://chatgpt.com/codex/cloud/settings/analytics#personal-usage`, including session and weekly limits.
- GitHub Copilot AI credit usage via GitHub REST billing summary endpoints for user or organization billing scopes, with a legacy premium-request fallback.
- OpenRouter account/key data via `/credits`, `/key`, and `/activity`, including balance, UTC day/month spend, optional daily budget gauge, and top model activity.
- Adaptive refresh cadence with active and idle intervals, manual refresh, and refresh pull-forward shortly after known reset times.
- Per-period peak history persisted locally in `current.json` and `history.jsonl`; there is still no history UI, so the data is available only on disk.
- Local diagnostic logging for auth, layout, API, and refresh lifecycle issues. Copyable diagnostics redact email addresses, truncate scraped page text, and carry the full `app_version`.
- Independently audited: a full line-by-line source audit found no covert egress, telemetry, code execution, or credential exfiltration, and its hardening findings are fixed in this fork. See `SECURITY-AUDIT.md`.

## Data Inputs and Integrations

- User-entered settings stored under per-OS app-data directories.
- Claude and Codex sessions from embedded Chromium profiles or pasted `Cookie:` headers, with separate storage per named account.
- GitHub Copilot fine-grained PAT stored in the OS credential store; optional username, billing organization, and AI credit allowance values.
- OpenRouter inference key and optional management key stored in the OS credential store; optional daily budget entered by the user.
- External integrations are direct local requests from the app to Claude.ai, ChatGPT, GitHub API, and OpenRouter API. No server-side app backend, public API routes, or web app routes were found in the codebase.
- Secrets use Windows Credential Manager/DPAPI, macOS Keychain, or Linux Secret Service depending on platform. This fork additionally refuses a planted plaintext secrets file unless explicitly opted in, writes `0600` atomically, quarantines rather than overwrites an undecryptable store, passes `CRYPTPROTECT_UI_FORBIDDEN` so DPAPI cannot hang the tray, and applies an owner-only Windows DACL.

## Outputs and Artifacts

- On-screen usage dashboard tiles showing percentage used, reset timing, status, and explanatory notes.
- Compact chips and tray/menu-bar indicators with severity colors based on usage thresholds.
- OpenRouter balance, day/month spend, daily budget progress when configured, and model-activity rows.
- Auth-required and error states, clickable error details, and copyable diagnostics.
- Local app config, browser profiles, encrypted/credential-store secrets, diagnostic log file, current period state, and append-only usage history JSONL.
- Release artifacts are OS-specific archives for Windows, macOS, and Linux, each with a SHA256 sum and a signed build-provenance attestation verifiable with `gh attestation verify`. They are not OS-code-signed. The running app does not generate user-facing exports.

## Differentiators to Investigate

- Local-only operation with no telemetry or backend — confirmed by audit rather than hypothesised, which may matter to privacy/security-conscious users.
- Hypothesis: an audited, hardened build with signed build provenance may distinguish this fork from both upstream and comparable utilities.
- Hypothesis: combining Claude, Codex, Copilot, and OpenRouter usage in one compact desktop surface may distinguish it from provider-specific dashboards.
- Hypothesis: multiple Claude/Codex account support is useful for users juggling personal/work subscriptions.
- Hypothesis: reset-aware refresh scheduling, pace indicators, and OpenRouter model breakdowns add context beyond raw percentages.

## Marketing-Relevant Constraints

- The app is unofficial and not affiliated with Anthropic, OpenAI, GitHub, Microsoft, OpenRouter, OpenCode, or other providers.
- This is a fork. Upstream cannot support these builds, and version numbers overlap upstream's without matching its code, so the full `+cfa.N` string must be quoted in any support context.
- Release binaries carry provenance attestation but are **not** code-signed, so Windows SmartScreen warns and macOS Gatekeeper reports an unsigned bundle as "damaged". Both need a documented user workaround.
- Claude, Codex, and OpenCode scraping depends on provider web page structure; layout, authentication, Cloudflare/security checks, or provider UI changes can break reads.
- Copilot usage can lag GitHub's upstream reporting by hours and is described as a trailing indicator, not real time.
- Copilot's current usage-based model is tracked as AI credits rather than premium request counts; annual/request-based accounts may still rely on GitHub's legacy premium-request API fallback.
- OpenRouter activity uses the last 30 completed UTC days and excludes the current UTC day; balance and model activity require a management key.
- Same-user local processes can generally decrypt/access stored session tokens or keys through the OS credential model; do not imply process-level isolation.
- No implemented collaboration, alerting, cloud sync, mobile app, browser extension, team dashboard, or export workflow was found.

## Suggested Positioning Angles

- Local desktop monitor for AI subscription usage across several providers.
- Compact quota and spend visibility for users with multiple AI accounts.
- Technical utility for tracking Claude/Codex/OpenCode reset windows, Copilot monthly AI credits, and OpenRouter spend.
- Audited-and-hardened alternative to the upstream project for users who want the source reviewed and the build attested.
- Privacy-conscious angle based on local storage and direct provider requests, with caveats about credential-store threat models.
- Maintenance/support angle around diagnostics for provider layout and API changes.
