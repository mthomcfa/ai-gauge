<!--
STAGING FILE — for the maintainer to review, edit, and (optionally) file
upstream at https://github.com/jpajak/ai-gauge/issues.

Do NOT include any real cookies, tokens, PATs, or account-identifying data.
The examples below are redacted placeholders. This file is not filed
automatically.
-->

# Security hardening findings — AI Gauge 0.6.3

A full source audit of AI Gauge 0.6.3 found **no malicious or covert behavior** —
no undisclosed egress, no telemetry, no code-execution surface, and no
credential-exfiltration path. The scraping JS reads rendered usage text only.

It did surface a set of hardening issues worth fixing. Fixes with regression
tests are prepared in a branch (see the companion PR write-up). Summary below;
severities are the auditor's.

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

## Not vulnerabilities (noted for completeness)

- Spoofed Chrome User-Agent and a broad SSO host allowlist in the sign-in
  window are required for real provider SSO flows.
- The DPAPI/keyring model protects secrets from *other* local users but not
  from same-user processes — inherent to every desktop credential store, and
  already disclosed in `SECURITY.md`.
