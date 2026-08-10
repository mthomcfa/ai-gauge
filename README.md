# AI Gauge (security-hardened fork)

[![test](https://github.com/mthomcfa/ai-gauge/actions/workflows/test.yml/badge.svg)](https://github.com/mthomcfa/ai-gauge/actions/workflows/test.yml)
![Windows / macOS / Linux](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-0078d4)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

> **This is a fork of [jpajak/ai-gauge](https://github.com/jpajak/ai-gauge), not the upstream project.**
> It carries a full source audit plus security fixes that upstream does not have, and its
> version numbers are its own — they do **not** correspond to upstream releases of the same
> number. Read [Relationship to upstream](#relationship-to-upstream) before using or reporting
> a bug against it. Issues and security reports belong [here](https://github.com/mthomcfa/ai-gauge/issues),
> not upstream, unless you have confirmed the problem also reproduces on upstream.

If you pay for multiple AI subscriptions and frequently check your usage, AI Gauge might help. It shows session and weekly usage, reset times, account balances, and spend in a compact always-visible view, so you can get the most out of what you're paying for.

Compact monitor for **Claude.ai**, **ChatGPT Codex**, **GitHub Copilot**, **OpenRouter**, and **OpenCode** usage. Manual + auto refresh, with a platform-native UI on each OS:

- **Windows / Linux** — always-on-top draggable frameless widget plus a system-tray icon.
- **macOS** — Stats-style menu-bar item (`● 42% ● 78% ● 15%`); the panel opens as a popover when you click it.

> **Requires Python 3.11+.** Secrets live in the OS-native credential store (Windows Credential Manager / DPAPI, macOS Keychain, Linux Secret Service). Auto-start uses the platform's standard mechanism (Windows Task Scheduler / LaunchAgent / `~/.config/autostart`).

Current version: **1.0.0+cfa.1** — a fork version, see [Versioning](#versioning). Release notes in [CHANGELOG.md](CHANGELOG.md).

AI Gauge is an independent open-source project and unofficial local desktop
utility. It is not affiliated with Anthropic, OpenAI, GitHub, Microsoft,
OpenRouter, or any other provider. Provider pages and APIs may change without
notice.

## Screenshots

**Windows / Linux** — always-on-top floating widget, in full panel and collapsed pill modes:

<p align="center">
  <img src="docs/screenshots/win-panel-full.png" alt="AI Gauge full panel showing Claude, Codex, and Copilot tiles" width="320" />
  &nbsp;&nbsp;
  <img src="docs/screenshots/win-panel-compact.png" alt="AI Gauge collapsed pill mode" width="320" />
</p>

**macOS** — Stats-style menu-bar item with per-provider tinted dots; click to open the panel as a popover:

<p align="center">
  <img src="docs/screenshots/mac-menubar.png" alt="AI Gauge macOS menu-bar item with three colored dots and percentages" width="400" />
  &nbsp;&nbsp;
  <img src="docs/screenshots/mac-popover.png" alt="AI Gauge macOS popover panel with Claude, Codex, and Copilot tiles" width="320" />
</p>

<details>
<summary>Settings dialog</summary>

<p align="center">
  <img src="docs/screenshots/settings.png" alt="AI Gauge settings dialog with provider, refresh, and Copilot PAT options" width="640" />
</p>

</details>

## Download

Binaries are published on **[this fork's Releases page](https://github.com/mthomcfa/ai-gauge/releases)**. Do not download from upstream's releases — those are built from different code and do not contain the fixes in [SECURITY-AUDIT.md](SECURITY-AUDIT.md).

| OS      | Archive                                | Run                                          |
| ------- | -------------------------------------- | -------------------------------------------- |
| Windows | `ai-gauge-<file-version>-windows.zip`       | extract, run `ai-gauge.exe`                  |
| macOS   | `ai-gauge-<file-version>-macos.tar.gz`      | **Apple Silicon only.** Extract, drag `ai-gauge.app` to Applications |
| Linux   | `ai-gauge-<file-version>-linux.tar.gz`      | extract, run `./ai-gauge/ai-gauge`           |

`<file-version>` is the version with `+` replaced by `-`, so `1.0.0+cfa.1` ships as `ai-gauge-1.0.0-cfa.1-windows.zip`. Print it with `python tools/check_versions.py`.

**Intel Macs are not covered by the prebuilt archive.** PyInstaller builds for the host architecture and this fork's CI runs on Apple Silicon, so the `.app` is arm64-only. Intel users should [run from source](#run-from-source); the menu-bar UI works identically.

### Verify before you run it

Do both of these before running anything you downloaded. They are the strongest guarantee these builds offer, because **none of them are signed with an OS-trusted code-signing certificate** — see the per-OS first-launch notes below.

**1. Check the SHA256.** Every archive ships with a `.sha256` file beside it.

```bash
# macOS / Linux
shasum -a 256 -c ai-gauge-<file-version>-macos.tar.gz.sha256
```

```powershell
# Windows - compare the printed hash to the contents of the .sha256 file
Get-FileHash .\ai-gauge-<file-version>-windows.zip -Algorithm SHA256
```

**2. Verify the build provenance.** Every release carries a signed attestation proving it was built by this repository's CI from a specific commit. This needs the [GitHub CLI](https://cli.github.com/) installed and authenticated (`gh auth login`):

```bash
gh attestation verify ai-gauge-<file-version>-windows.zip --repo mthomcfa/ai-gauge
```

The SHA256 alone only proves the file matches what the release page says. The attestation is what ties it to this repository's CI — so if you only do one, do this one.

For a stricter check, pin the workflow that is allowed to have signed it, so an attestation minted by any *other* workflow in the repo is rejected:

```bash
gh attestation verify ai-gauge-<file-version>-windows.zip --repo mthomcfa/ai-gauge \
  --signer-workflow mthomcfa/ai-gauge/.github/workflows/release.yml
```

### First launch

- **Windows** — SmartScreen shows "Windows protected your PC" for an unsigned binary. Click **More info → Run anyway**.
- **macOS** — Gatekeeper quarantines unsigned apps downloaded from the internet. On current macOS the dialog reads **"ai-gauge.app is damaged and can't be opened"**, which is misleading: it means *unsigned*, not corrupt. After verifying the SHA256 and the attestation above, clear the quarantine flag:

  ```bash
  # Use wherever the app actually is - the archive extracts to ./ai-gauge.app,
  # so this path is only /Applications after you have moved it there.
  xattr -dr com.apple.quarantine /path/to/ai-gauge.app
  ```

  If you would rather not do that, [run from source](#run-from-source) instead — the menu-bar UI works identically.
- **Linux** — no equivalent gate; make sure the binary is executable (`chmod +x ai-gauge/ai-gauge`).

The macOS bundle is *ad-hoc* signed (which keeps it launchable on Apple Silicon) but not notarized, and the Windows binary is not Authenticode-signed. Neither is an OS-trusted identity: doing that properly needs an Apple Developer ID plus notarization, and a Windows code-signing certificate. Neither is set up for this fork, which is why the attestation is deliberately the thing to check.

## Run from source

**Windows (PowerShell):**

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .[dev]
.\.venv\Scripts\python.exe -m aigauge
```

**macOS / Linux (bash):**

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/python -m aigauge
```

On first launch the widget appears with enabled provider tiles. Claude and Codex use a **Sign in** flow; GitHub Copilot and OpenRouter are configured from Settings with API credentials. Open Settings to disable providers you don't use or to add more Claude/Codex accounts.

## First-time setup per provider

| Provider           | Setup                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Claude.ai**      | **Sign in (recommended):** opens an embedded browser. <b>Don't click "Continue with Google"</b> — Google refuses to authenticate inside embedded browsers. If your account is Google-linked, just type that same email into the **Enter your email** box and use the **magic link** sent to your inbox. **Paste cookie:** fallback if magic-link is unavailable; see below. Add extra Claude subscriptions from **Settings → Claude**. |
| **ChatGPT Codex**  | Same as Claude — use email + magic link in the embedded browser, or paste cookie as a fallback. If your OpenAI account routes through Google or a passkey, use **Paste cookie**; embedded browsers often cannot complete those flows. Add extra Codex subscriptions from **Settings → Codex**.                                                                                                                                                 |
| **GitHub Copilot** | Create a **fine-grained PAT** at <https://github.com/settings/personal-access-tokens/new>. For personal plans, add **Account permissions → Plan → Read**. Paste into Settings; set your monthly AI credit allowance (Pro=1,500, Pro+=7,000, Max=20,000). If Copilot is billed through an organization, enter the billing org and use a token/account with org billing access and **Organization permissions → Administration → Read**. |
| **OpenRouter**     | Create an inference API key at <https://openrouter.ai/keys> and paste it into Settings. To show account balance and model activity, also create a management key at <https://openrouter.ai/settings/provisioning-keys>. Management keys cannot be used for inference; AI Gauge stores it separately and only uses it for OpenRouter management endpoints. Daily spend budget is optional.                                                    |

### Multiple Claude / Codex accounts

Claude and Codex can track more than one subscription at a time. Open **Settings → Claude** or **Settings → Codex**, click **Add another**, give the account a short name, then use **Sign in** or **Paste cookie** for that specific row. The default account displays as `Claude` or `Codex`; named accounts display as `Claude (Work)`, `Codex (Account 2)`, etc.

The **General** tab controls provider groups. If Claude is checked, all configured Claude accounts appear; if Codex is checked, all configured Codex accounts appear. Secondary accounts can be removed from their provider tab. Each Claude/Codex account uses separate cookie storage, browser profile data, widget tile state, and history records.

Sessions persist between runs under the per-OS app-data directory:

| OS      | App data                                  | Secrets backend                           |
| ------- | ----------------------------------------- | ----------------------------------------- |
| Windows | `%APPDATA%/ai-gauge/`                     | Credential Manager (GitHub PAT + OpenRouter keys) + DPAPI-encrypted `secrets.dat` (cookies, since the Credential Manager blob limit is too small for ChatGPT JWTs) |
| macOS   | `~/Library/Application Support/ai-gauge/` | Login Keychain                            |
| Linux   | `~/.config/ai-gauge/`                     | Secret Service (GNOME Keyring / KWallet)  |

AI Gauge does not include telemetry or a backend service. Provider requests
are made from the local app to the configured providers. See
[SECURITY.md](SECURITY.md) for security and privacy notes.

### Paste cookie (fallback)

If the embedded-browser sign-in doesn't work for you (e.g. your account requires Google sign-in, passkey authentication, or you can't use the magic-link path), copy your existing session cookie from your normal browser into the app. Cookies last weeks before they need re-pasting.

1. Sign into <https://claude.ai> (or <https://chatgpt.com>) in **Chrome / Edge / Firefox** as you normally do.
2. For ChatGPT, press **F12** → **Network**, reload the page, click a
   `chatgpt.com` request, and copy the full **Request Headers → Cookie:** value.
   This includes split session cookies plus companion auth cookies such as
   `__Secure-oai-is`.
3. For Claude, press **F12** → **Network**, reload `https://claude.ai/settings/usage`,
   click a `claude.ai` request, and copy the full **Request Headers → Cookie:**
   value. It must include `sessionKey`.
4. In the app: Settings → Claude or Settings → Codex → click **Paste cookie** next to the account, paste, Save.

## Daily use

- **Windows / Linux:** the widget floats above other windows by default. Drag anywhere to move; close (✕) hides to tray. Right-click the tray icon for Refresh / Settings / Quit. Left-click toggles widget visibility. Tray icon turns yellow ≥75% / red ≥90% based on the highest tile reading.
- **macOS:** the menu-bar item shows tinted status dots for enabled provider/account tiles. Click it to open the panel as a popover; click outside to dismiss. Right-click for the same Refresh / Settings / Quit menu.
- **Linux without a system tray** (stock GNOME): the floating widget stays visible and serves the same Show / Refresh / Settings / Quit menu via right-click on the widget.
- **Collapse / expand:** click the **−** button in the widget header to shrink to the compact pill view. Enabled provider/account chips wrap onto additional rows when needed, with named secondary Claude/Codex accounts using just the account name to save space.
- **Hide unused providers:** uncheck Claude / Codex / Copilot / OpenRouter in Settings to remove their group from the widget — useful if you only use one or two of them.
- Auto-refresh is adaptive: manual refresh or changed usage enters the active
  cadence, then unchanged results back off toward the configured max interval.
  Defaults are 5 min active and 60 min idle max.
- Enable **Start at login** in Settings if you want it to run as a daily utility.

## Build a standalone binary

For most users the [pre-built downloads](#download) are easier — this section is for building locally or for maintainers cutting releases. The build machine needs Python 3.11+ and a `.venv` with `pip install -e .[dev]` already run; the resulting binary does **not** require Python on the target machine.

> **Windows: `build.ps1` requires PowerShell 7+.** It declares `#requires -version 7`, so Windows PowerShell 5.1 — still the default `powershell.exe` on Windows 10/11, and what you get from most Start-menu and right-click entries — refuses to run it and reports a `#requires` version error rather than a build failure. Check with `$PSVersionTable.PSVersion`; if it reports 5.x, install PowerShell 7 (`winget install Microsoft.PowerShell`) and build from `pwsh`. This affects the build script only — running the app from source, the tests, and `check_versions.py` all work under 5.1.

| OS      | Command          | Output                       |
| ------- | ---------------- | ---------------------------- |
| Windows | `.\build.ps1`    | `dist/ai-gauge/ai-gauge.exe` |
| macOS   | `./build.sh`     | `dist/ai-gauge.app`          |
| Linux   | `./build.sh`     | `dist/ai-gauge/ai-gauge`     |

Tagged commits matching `v*` trigger [the release workflow](.github/workflows/release.yml), but only a tag of the form `vX.Y.Z+cfa.N` is accepted — any other `v*` tag is rejected in the first job. Accepted tags build all three platforms in CI and upload them as a draft GitHub Release for the maintainer to publish.

Bundles are ~150-200 MB because the Chromium runtime ships inside. User data still lives outside the bundle, under the per-OS app-data directory.

For a single-file binary (slower first launch), pass `-OneFile` (PowerShell) or `--onefile` (bash). On macOS the `.app` bundle is recommended over the single-file form.

**First-launch warnings on signed-OS-bundle systems** - release artifacts are unsigned:

- **Windows:** SmartScreen -> "More info" -> "Run anyway". Windows builds include product/version metadata, but unsigned low-prevalence binaries can still trigger SmartScreen or Microsoft Defender reputation warnings.
- **macOS:** Gatekeeper blocks on first launch — see [First launch](#first-launch). The Control-click → Open bypass does *not* apply to a downloaded unsigned bundle (macOS reports it as "damaged", not "unidentified developer"), and macOS 15 removed that bypass in favour of Privacy & Security → Open Anyway. A bundle you built yourself is not quarantined and launches normally.
- **Linux:** no signing layer; just make `ai-gauge` executable if it isn't already.

See [RELEASING.md](RELEASING.md) for maintainer release steps.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest    # Windows
./.venv/bin/python -m pytest            # macOS / Linux
```

Tests cover: config round-trip, Copilot and OpenRouter REST helpers (with mocked HTTP), widget behavior, and snapshot models. Provider scrapers (Claude/Codex) require a live browser session and are validated manually.

## Relationship to upstream

This fork was created from upstream [`1df4536`](https://github.com/jpajak/ai-gauge/commit/1df4536) (upstream **v0.6.3**) and audited line by line. The audit found **no covert egress, telemetry, code execution, or credential exfiltration** — the upstream source was clean. It did find 13 hardening defects — 9 fully fixed, 1 improved, 2 partially fixed, and 1 left open as a maintainer call — plus one informational finding that is not a vulnerability. All are recorded with attack scenarios in [SECURITY-AUDIT.md](SECURITY-AUDIT.md).

**What this fork has that upstream does not:**

| Area | Hardening |
| --- | --- |
| Secret storage | Plaintext `secrets.dat` rejected unless explicitly opted in; atomic `0600` writes; undecryptable files quarantined rather than overwritten; `CRYPTPROTECT_UI_FORBIDDEN` so DPAPI cannot hang the tray; explicit owner-only Windows DACL |
| Embedded browser | `data:`/`blob:` top-frame navigation blocked (anti-phishing); host allowlist rejects look-alikes such as `claude.ai.evil.com` |
| Account removal | Deletes the whole QtWebEngine profile — live Chromium cookies and cache — not just the stored blob; plus a **Clear all browser data** button |
| OpenCode | Only allowlisted cookies injected from a pasted header; workspace URL pinned to `https` on `opencode.ai` (no `file:`, `data:`, other host, port, or embedded credentials) |
| Path safety | Account ids validated against traversal and Windows reserved device names before use as filesystem paths |
| Config durability | One bad setting no longer discards the whole config; unreadable files are preserved as `config.json.corrupt` |
| Diagnostics | Email addresses redacted, scraped page text truncated |
| Supply chain | All GitHub Actions pinned to commit SHAs, `GITHUB_TOKEN` at least privilege, PyInstaller version pinned, signed build-provenance attestation on every release |
| Gauge colors | Validated as `#RRGGBB` and laundered through `QColor(...).name()` at every stylesheet sink |

**Deliberate divergence.** Later upstream releases were reviewed and selectively cherry-picked rather than merged, because upstream v0.7.0 **regresses four of the fixes above**. Most relevant to gauge colors: upstream declares `green_color: str` with no validation and interpolates it straight into `setStyleSheet`, which is a genuine QSS injection from a hand-edited config; and it uses `Field(ge=0, le=100)` on the cutoffs, which raises and destroys the whole config file. This fork takes upstream's *features* and keeps its own hardening.

That also means **upstream cannot support this build**, and bugs here may not exist upstream. File issues [here](https://github.com/mthomcfa/ai-gauge/issues). If you have confirmed a bug also reproduces on a clean upstream checkout, filing it upstream too is welcome and useful.

## Versioning

Fork releases use a [PEP 440](https://peps.python.org/pep-0440/) local version segment:

```
1.0.0+cfa.1
└─┬─┘ └─┬─┘
  │     └── fork build counter — identifies this as a fork build
  └──────── this fork's own release counter, NOT an upstream release number
```

**The number before `+` does not mean "equivalent to upstream X."** This fork's `0.6.4` was built from upstream v0.6.3, while upstream separately shipped its own, unrelated `v0.6.4`; upstream also has a `v0.6.5`. The `+cfa.N` segment is what makes a fork build unambiguous, so always quote the full string in a bug report. The exact upstream commit this tree descends from is recorded in `pyproject.toml` under `[tool.ai-gauge-audit]`, and the app shows the full version in its panel header, its tray tooltip, and the `app_version` field of **Copy diagnostics**.

`tools/check_versions.py` enforces the scheme in CI — a bare, upstream-style number fails the build. Archive filenames substitute `-` for `+`, since GitHub normalises some characters in release asset names.

## Contributing

Bug reports, provider-layout fixes, and PRs are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for environment setup, test commands, and
the issue templates to use.

## When a tile shows an error

Click the tile to open **error details**, then **Copy diagnostics** — that blob
carries the page text and the failure reason, and is what makes a report
actionable. A few messages are worth recognising:

| Message | What it means |
| ------- | ------------- |
| `Claude's usage layout changed: could not read <row> …` | The page rendered but the reading could not be justified — either several meters share one container so the percentage cannot be attributed to a row, or there is no "used"/"remaining" wording beside the number. The tile refuses rather than showing a number that may be inverted or belong to a different meter. The blob carries the row's text. |
| `extractor retry limit exceeded` | The page loaded but never finished rendering usage within the time allowed. |
| `page failed to load` | A network or browser-level failure. The diagnostics carry Chromium's reason, e.g. `net::ERR_CONNECTION_RESET`. |
| `Not signed in to <provider>` | The stored session expired. Re-run sign-in, or use **Paste cookie** in Settings. |

Diagnostics for Claude also include an `api` section recording the *shape* of
the JSON the page fetched — field names, numbers and timestamps, with all other
strings reduced to a length. It never contains a response body and never leaves
your machine. See "API response shapes" in [SECURITY.md](SECURITY.md).

## Notes / limitations

- **Why an embedded browser instead of reading Chrome cookies?** Chrome 127+ added App-Bound Encryption (mid-2024) that blocks every external Python library from decrypting Chrome/Edge cookies. Owning the browser session ourselves is the only reliable workaround.
- **Claude / Codex layouts may change.** If a provider tile shows "error" after a UI update upstream, the page-extractor JS in `src/aigauge/providers/{claude,codex}.py` needs adjusting — the rest of the app keeps working.
- The Copilot REST endpoint returns the _current calendar month_ of billing usage. The widget tracks gross AI credits consumed against the included allowance; net quantity/amount is only the billable overage. Reset is computed as the 1st of the next month. GitHub does not currently expose a reliable personal-plan allowance field, so Settings uses a plan dropdown with a Custom fallback. Annual/request-based accounts are handled with a legacy premium-request fallback.
- **Copilot usage lags upstream.** The Copilot REST endpoint updates noticeably slower than Claude or Codex — credit counts can take hours to reflect recent activity. The widget shows the most recent value GitHub returns; treat the Copilot tile as a trailing indicator, not real-time.
- **Copilot AI credits.** GitHub moved Copilot from per-request quotas to token-based AI credits. Code completions and next edit suggestions remain included for paid plans, while Chat, CLI, cloud agent, Spaces, Spark, and third-party coding agents consume AI credits. The app shows the credit usage GitHub returns; if your account is org-billed, enter the billing organization so AI Gauge reads the organization billing pool.
- **OpenRouter uses two key types.** The inference key is used for `/key` spend data. The management key is required for `/credits` account balance and `/activity` model history. Without a management key, AI Gauge still shows key-level spend but cannot show balance or model activity.
- **OpenRouter time windows are UTC.** Today/month spend come from OpenRouter's current UTC day and month fields. Model activity comes from OpenRouter's default `/activity` history window: the last 30 completed UTC days, excluding the current UTC day.
