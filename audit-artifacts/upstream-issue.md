<!--
STAGING FILE — ready to file as-is at
https://github.com/jpajak/ai-gauge/issues. Nothing here is filed
automatically; posting it is a deliberate human action.

Contains no cookies, tokens, PATs, or account-identifying data. Every finding
below was re-verified against upstream `main` at 0.7.0 before this was written.
-->

# Security hardening findings — audited at 0.6.3, re-verified against 0.7.0

A full line-by-line source audit of AI Gauge **0.6.3** found **no malicious or
covert behavior** — no undisclosed egress, no telemetry, no code-execution
surface, and no credential-exfiltration path. The scraping JS reads rendered
usage text only. That result is worth stating plainly: the code was clean.

It did surface a set of hardening issues. All of them were re-checked against
upstream `main` at **0.7.0**, and **all ten remain unfixed there** — none of
`_blocked_main_frame_scheme`, `validate_opencode_usage_url`, the account-id
validator, the OpenCode cookie allowlist, or `CRYPTPROTECT_UI_FORBIDDEN`
appears in 0.7.0. Items 11–13 are *new* in 0.7.0 and were not present at 0.6.3.

Fixes with regression tests are prepared and running in a fork (see the
companion PR write-up). Severities are the auditor's. Happy to split this into
separate issues, or to open PRs one finding at a time, if that suits your
workflow better.

## Medium

1. **Account removal leaves the live session on disk.** Removing a browser
   account clears the stored cookie blob but leaves the account's whole
   QtWebEngine profile under `profiles/<id>/`, including Chromium's own
   persisted (rotated, live) session cookies and cache. A user who "removes"
   an account reasonably expects the session to be gone.
   *Fix:* tear down and delete the profile directory on removal (guarded to
   stay inside `profiles/`), plus a "Clear all browser data" action.

2. **Pasted OpenCode cookie header injected wholesale.** For OpenCode the paste
   flow injected every cookie in the pasted `Cookie:` header, including any
   unrelated analytics/tracking cookies the user copied alongside the session.
   *Fix:* allowlist cookies in OpenCode's own namespace; drop the rest.
   (OpenCode's session cookie intentionally stays script-readable so the SPA
   hydrates — that part is by design and unchanged.)

3. **OpenCode usage URL loaded without validation.** `usage_url` is a bare
   string loaded into the signed-in embedded browser via `QUrl`. A poisoned
   `config.json` (or a mistyped Settings value) could point the authenticated
   session at `file:`/`data:`/an attacker host.
   *Fix:* validate to `https` on `opencode.ai` with a workspace path and no
   credentials/port/IP/look-alike host, enforced at the load chokepoint.

4. **Account id → path traversal.** `BrowserAccount.id` flows unvalidated into
   `profiles/<id>`. A config with `id: "../../evil"` redirects profile
   create/open/delete outside the app-data tree.
   *Fix:* validate ids to the generated `slug-<hex>` / fixed-provider shape and
   assert the resolved profile path stays under `profiles/`.

5. **Sign-in window allowed `data:` top-frame navigation.** The embedded,
   address-bar-less sign-in window allowlists hosts for `http(s)` but waved
   through `data:` URLs, which have an opaque origin — a phishing canvas if a
   provider redirect were ever abused.
   *Fix:* block `data:` main-frame navigation (keep `about:`/`blob:`).

6. **Secret-storage failure handling.** `secrets.dat` decrypt/parse failures
   were swallowed and the file was liable to be overwritten on the next save;
   the plaintext dev-fallback file was written with the process umask; writes
   were non-atomic.
   *Fix:* quarantine (don't destroy) an undecryptable file, log failures,
   write atomically, `0600` the dev file, apply an owner-only Windows DACL, and
   pass `CRYPTPROTECT_UI_FORBIDDEN` to DPAPI.

## Low

7. **Unsigned releases, no provenance.** Release zips are unsigned and carry no
   build attestation. *Fix:* add build-provenance attestation (code signing
   still recommended).

8. **Unpinned build tooling / floating deps.** `build.ps1`/`build.sh` installed
   PyInstaller unpinned at build time. *Fix:* pin it; consider per-platform
   hashed dependency locks.

9. **Diagnostics can leak PII.** "Copy diagnostics" serialized up to ~8 KB of
   scraped page text (which can show the account holder's name/email/org) to
   the clipboard. *Fix:* redact email-shaped strings and truncate page text.

10. **Hardcoded default OpenCode workspace URL.** The default `usage_url` points
    at what appears to be a specific workspace id. Consider shipping an empty
    default with a "configure your workspace URL" state.

## New in 0.7.0 (not present at 0.6.3)

These arrived with the configurable gauge colors and are the reason the fork
cherry-picked that feature rather than merging it.

11. **QSS injection from a hand-edited config (Medium).** `ColorThresholds`
    declares `green_color: str = "#22c55e"` (`config.py:110`) with no validator,
    and `widget.py:734` and `widget.py:877` interpolate it straight into a
    stylesheet: `f"QProgressBar::chunk {{ background:{color}; ... }}"`. A value
    such as `red; } * { background: url(http://host/x) } a {` closes the
    declaration and injects arbitrary QSS. Qt's `url()` is a local-file sink
    rather than a network one, so the realistic impact is arbitrary local-file
    read-as-image plus UI restyling — not egress — but it is reachable from a
    config file that any local process running as the user can write.
    *Fix:* validate to `#RRGGBB`, and additionally launder every stylesheet
    sink through `QColor(...).name()` so safety does not depend on validation
    having run.

12. **One bad colour value destroys the entire config (Medium).**
    `config.py:107` uses `Field(default=59, ge=0, le=100)`. Pydantic raises on
    an out-of-range value, and `Config.load()`'s `except Exception: return
    cls()` (`config.py:177`) turns that into a silent full reset — losing named
    accounts, Copilot username/quota/billing org, OpenRouter budget, OpenCode
    workspace URL, window geometry, autostart and provider toggles. The next
    `save()` makes it permanent. A single typo (`"green_max": 105`) is enough.
    *Fix:* coerce rather than raise (validate in `mode="before"` and clamp), and
    more generally, salvage the valid keys instead of discarding the whole file.
    Worth noting this is not colour-specific — `{"window": {"height": "abc"}}`
    and `{"expanded_tiles": 5}` destroy the config the same way today.

13. **Release workflow executes the git tag (Low; needs tag-push access).**
    `.github/workflows/release.yml:33-34` interpolates
    `${{ steps.ver.outputs.version }}` directly into a bash comparison. Git ref
    names permit `;`, `$`, `&` and backticks, so a tag like
    `v0.0.0";id;touch${IFS}/tmp/x;:"` executes before the version check can
    reject it. Reproduced end to end. The same pattern repeats at lines 129,
    143. Exploiting it requires push access to tags — a maintainer, or a
    compromised token or action — so it is defence-in-depth rather than a
    remote hole, but it sits in the pipeline that signs and publishes binaries.
    *Fix:* pass values via `env:` and reference them as quoted shell variables,
    and validate the tag shape before any other step consumes it.

## Not vulnerabilities (noted for completeness)

- Spoofed Chrome User-Agent and a broad SSO host allowlist in the sign-in
  window are required for real provider SSO flows.
- The DPAPI/keyring model protects secrets from *other* local users but not
  from same-user processes — inherent to every desktop credential store, and
  already disclosed in `SECURITY.md`.
