<!--
STAGING FILE — a ready-to-use PR description for contributing these hardening
fixes upstream (https://github.com/jpajak/ai-gauge). Nothing here is filed
automatically; opening the PR is a deliberate human action.

Contains no secrets or tokens. Figures below are from the fork at 0.6.5+cfa.1.
-->

# Security hardening (post-audit)

A full line-by-line source audit found the codebase **clean** — no covert
egress, telemetry, code-execution paths, or credential exfiltration. This change
set addresses the hardening findings it surfaced, plus three issues introduced
in 0.7.0. Each fix is a focused commit with a regression test.

The work was audited at **0.6.3** (`1df4536`) and has been kept current since;
every finding was re-verified against `main` at 0.7.0 before this was written.
See the companion issue write-up for the findings themselves.

## What changed

| Area | Change |
|------|--------|
| `secret_storage.py` | Gate plaintext reads behind the existing opt-in; write `0600`; atomic writes (`os.replace`); quarantine (not overwrite) an undecryptable `secrets.dat`; log DPAPI failures; `CRYPTPROTECT_UI_FORBIDDEN`; explicit owner-only Windows DACL via `icacls` (resolved to System32). |
| `webview/login_window.py` | Block `data:` and `blob:` top-frame navigation in the embedded sign-in window. |
| `settings_dialog.py` + `webview/profile.py` | `purge_profile()` deletes an account's whole QtWebEngine profile on removal (guarded to stay under `profiles/`); new "Clear all browser data" button; corrected the Remove tooltip. |
| `webview/cookies.py` | Allowlist OpenCode cookies from a pasted header (drop foreign cookies). Per-provider HttpOnly kept — OpenCode stays script-readable for SPA hydration, as in 0.6.3. |
| `config.py` / `providers/opencode_go.py` | `validate_opencode_usage_url()` — `https` on `opencode.ai`, workspace path, no credentials/port/IP/look-alike host; enforced as a field validator and at the load chokepoint. |
| `config.py` | Validate `BrowserAccount.id` against traversal and Windows reserved device names; make `webview_profile_dir()` the single traversal-checked chokepoint. |
| `config.py` | **`Config.load()` salvages valid settings** instead of discarding the whole file when any one value fails to parse or validate, and preserves an unreadable file as `config.json.corrupt` rather than silently destroying it. |
| `config.py` / `widget.py` / `macos_status_item.py` | Validate gauge colours to `#RRGGBB` and launder every stylesheet sink through `QColor(...).name()`, so a hand-edited config cannot inject QSS. (Applies to the 0.7.0 colour feature.) |
| `error_dialog.py` | Redact emails and truncate scraped page text in "Copy diagnostics"; include `app_version`. |
| `.github/workflows/*.yml` | SHA-pin all actions; least-privilege `GITHUB_TOKEN`; build-provenance attestation; **stop interpolating the git tag into shell commands** and validate its shape before use. |
| `build.ps1` / `build.sh` | Pin the build-time PyInstaller version. |

## Behavior notes for review

- **OpenCode HttpOnly is intentionally unchanged.** An initial hardening pass
  forced HttpOnly on all providers, which would have reverted the 0.6.3 fix
  that keeps OpenCode's cookie script-readable for SPA hydration. That was
  caught and corrected — only the injected-cookie allowlist changed for
  OpenCode.
- **Poisoned-config handling is a deliberate fail-safe.** An invalid account id
  or OpenCode URL now falls back to a safe default on load rather than being
  adopted, and unrelated settings survive. The security property holds while
  the user keeps their configuration.
- **Colour validation coerces, it never raises.** Raising inside a validator
  reaches `Config.load()`'s blanket `except` and resets everything, so all
  colour checks run in `mode="before"` and fall back to the band default.
- **`--require-hashes` is not included.** A single hashed lock cannot cover the
  win/mac/linux matrix (platform-only deps differ) and per-OS locks could not
  be verified in the audit environment; recommended as a follow-up.
- **Scope note.** This branch also carries a fork-specific version scheme
  (`+cfa.N`) and fork-facing documentation. Those are **not** intended for
  upstream and should be dropped from any PR filed here — the security fixes
  above are the contribution.

## Testing

- `pytest` — 446 passing in the fork (265 at the audited 0.6.3 baseline).
- `bandit -r src/ tools/` — 0 High, 0 Medium in `src/`.
- `pip-audit` — no vulnerable dependencies.
- Each fix has a regression test that was mutation-checked: the covered
  behaviour was reverted and the test confirmed to fail.
