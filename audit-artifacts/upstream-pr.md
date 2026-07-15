<!--
STAGING FILE — a ready-to-adapt PR description for contributing these
hardening fixes upstream (https://github.com/jpajak/ai-gauge). The maintainer
should review and edit before opening anything upstream. Nothing here is filed
automatically. No secrets/tokens are included.
-->

# Security hardening (post-audit)

A full source audit of 0.6.3 found the codebase clean (no covert egress,
telemetry, code-execution, or credential-exfiltration paths). This change set
addresses the hardening findings it surfaced. Each fix is a focused commit with
a regression test; the suite goes from 265 to 316 passing, `bandit` reports 0
High, and `pip-audit` is clean.

## What changed

| Area | Change |
|------|--------|
| `secret_storage.py` | Gate plaintext reads behind the existing opt-in; write `0600`; atomic writes (`os.replace`); quarantine (not overwrite) an undecryptable `secrets.dat`; log DPAPI failures; `CRYPTPROTECT_UI_FORBIDDEN`; explicit owner-only Windows DACL via `icacls` (resolved to System32). |
| `webview/login_window.py` | Block `data:` and `blob:` top-frame navigation in the embedded sign-in window. |
| `settings_dialog.py` + `webview/profile.py` | `purge_profile()` deletes an account's whole QtWebEngine profile on removal (guarded to stay under `profiles/`); new "Clear all browser data" button; corrected the Remove tooltip. |
| `webview/cookies.py` | Allowlist OpenCode cookies from a pasted header (drop foreign cookies). Per-provider HttpOnly kept (OpenCode stays script-readable for SPA hydration, as in 0.6.3). |
| `config.py` / `providers/opencode_go.py` | `validate_opencode_usage_url()` — `https` on `opencode.ai`, workspace path, no credentials/port/IP/look-alike host; enforced as a field validator and at the load chokepoint. |
| `config.py` | Validate `BrowserAccount.id` against traversal; make `webview_profile_dir()` the single traversal-checked chokepoint. |
| `error_dialog.py` | Redact emails and truncate scraped page text in "Copy diagnostics". |
| `.github/workflows/*.yml` | SHA-pin all actions; least-privilege `GITHUB_TOKEN`; build-provenance attestation. |
| `build.ps1` / `build.sh` | Pin the build-time PyInstaller version. |

## Behavior notes for review

- **OpenCode HttpOnly is intentionally unchanged.** The initial hardening pass
  forced HttpOnly on all providers, which reverts the 0.6.3 fix that keeps
  OpenCode's cookie script-readable for SPA hydration; that was corrected — only
  the injected-cookie allowlist changed for OpenCode.
- **Poisoned-config handling:** an invalid account id or OpenCode URL in
  `config.json` now causes a fall back to defaults on load rather than being
  adopted. This is a deliberate fail-safe.
- **`--require-hashes` is not included.** A single hashed lock can't cover the
  win/mac/linux matrix (platform-only deps differ) and per-OS locks couldn't be
  verified in the audit environment; recommended as a follow-up.

## Testing

- `pytest` — 316 passing (was 265; +51 regression tests).
- `bandit -r src/ tools/` — 0 High (Low/Medium are pre-existing fixed-argv
  subprocess and an f-string-template false positive).
- `pip-audit` — no vulnerable dependencies.
