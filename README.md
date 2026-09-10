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

Compact monitor for **Claude.ai**, **ChatGPT Codex**, **Microsoft** (Azure spend + Copilot), **OpenRouter**, and **OpenCode** usage. Manual + auto refresh, with a platform-native UI on each OS:

- **Windows / Linux** — always-on-top draggable frameless widget plus a system-tray icon.
- **macOS** — Stats-style menu-bar item (`● 42% ● 78% ● 15%`); the panel opens as a popover when you click it.

> **Requires Python 3.11+.** Secrets live in the OS-native credential store (Windows Credential Manager / DPAPI, macOS Keychain, Linux Secret Service). Auto-start uses the platform's standard mechanism (Windows Task Scheduler / LaunchAgent / `~/.config/autostart`).

Current version: **1.2.0+cfa.4** — a fork version, see [Versioning](#versioning). Release notes in [CHANGELOG.md](CHANGELOG.md).

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

`<file-version>` is the version with `+` replaced by `-`, so `1.2.0+cfa.4` ships as `ai-gauge-1.2.0-cfa.4-windows.zip`. Print it with `python tools/check_versions.py`.

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
| **Microsoft — Copilot** | Create a **fine-grained PAT** at <https://github.com/settings/personal-access-tokens/new>. For personal plans, add **Account permissions → Plan → Read**. Paste into **Settings → Microsoft → Copilot**; set your monthly AI credit allowance (Pro=1,500, Pro+=7,000, Max=20,000). If Copilot is billed through an organization, enter the billing org and use a token/account with org billing access and **Organization permissions → Administration → Read**. |
| **Microsoft — Azure**   | Needs an Entra ID app registration; see [Azure month-to-date spend](#azure-month-to-date-spend) below. |
| **OpenRouter**     | Create an inference API key at <https://openrouter.ai/keys> and paste it into Settings. To show account balance and model activity, also create a management key at <https://openrouter.ai/settings/provisioning-keys>. Management keys cannot be used for inference; AI Gauge stores it separately and only uses it for OpenRouter management endpoints. Daily spend budget is optional.                                                    |

### Azure month-to-date spend

The **Microsoft** tab in Settings holds three sub-headings: **Azure**,
**Foundry**, and **Copilot**. Azure and Copilot are separate providers with
separate credentials; they share a tab because they are one vendor
relationship to the person configuring them.

The Azure tile shows month-to-date spend against a monthly allowance, broken
into component rows by Azure service:

```
Microsoft · Azure
Spend this month (CAD 36.10 of 150.00)      resets Oct 1
[■■■■■■■░░░░░░░░░░░░░░░░░░░░░░]
  Foundry                    12.40   34%
  Azure OpenAI                8.05   22%
  Container Apps              6.90   19%
  Marketplace models          4.10   11%
  Storage                     1.20    3%
  Other (3 services)          3.45   10%
  Forecast end of month      ~71.00  47%
```

Only the top row counts toward the tray/menu-bar colour. The component rows
are shares of spend, not usage against a limit, so a single service at 96% of
the month's spend must not turn the tray red.

#### Setting it up

1. **Register an application.** Azure portal → **Microsoft Entra ID** → **App
   registrations** → **New registration**. Single tenant is fine; no redirect
   URI is needed — this uses client credentials, not an interactive sign-in.
2. **Create a client secret.** In the new registration → **Certificates &
   secrets** → **New client secret**. Copy the secret **Value** (not the Secret
   ID); it is shown only once.
3. **Grant it two roles on the subscription.** Azure portal → your
   subscription → **Access control (IAM)** → **Add role assignment**, with the
   app registration as the member:
   - **Cost Management Reader** — cost queries, the forecast, and budgets.
   - **Reader** — listing resources (to find Foundry resources by kind) and
     reading the subscription's offer type.

   Reader is optional in the narrow sense that the spend gauge still works
   without it, but you then lose the Foundry roll-up and the sponsorship
   warning, and you have to pin Foundry resource IDs by hand.
4. **Fill in Settings → Microsoft → Azure.** Directory (tenant) ID,
   Application (client) ID, the client secret, and the Subscription ID — all
   three IDs are GUIDs, and anything else is refused. Set a monthly allowance
   and the day of the month it resets.
5. **Enable the tile** on the **General** tab (Microsoft Azure).

#### Allowance and reset semantics

- The allowance is **a number you state**, in your subscription's own billing
  currency. The tile shows whichever currency the API reports and never assumes
  dollars.
- If the subscription already has a **monthly cost Budget** in Azure, that
  amount is used instead, so the figure does not live in two places.
- **Reset day** defaults to 1 (calendar month). Set it to the day your credit
  actually renews — a Visual Studio credit resets on its own anniversary, not
  on the 1st, and querying the calendar month would measure the wrong window.
  Capped at 28 so the date exists in February.

#### Cost is gross of credits

Cost Management reports **consumption**, and explicitly excludes free and
prepaid credits — there is no credit line in the data to subtract. So:

- This gauge measures spend against the allowance you entered. It is **not** a
  live read of your remaining Visual Studio or MCA credit balance.
- **Azure Sponsorship offers are not supported by Cost Management at all.** They
  report zero cost while the sponsorship credit drains. AI Gauge detects this
  from the subscription's offer type and shows a warning row instead of a 0%
  gauge, because a reassuring gauge here is a failure you cannot see.

#### Foundry is a row, not a tile

Microsoft Foundry (formerly Azure AI Foundry) bills **per token to the Azure
subscription**. Its spend is already inside the Azure total, so a separate
Foundry tile would show the same money twice with nothing saying so. It is a
roll-up row inside the Azure tile instead, and every cost row lands in exactly
one bucket, so the rows always sum to the total.

Foundry resources are found by resource **kind** (`AIServices`). That matters:
Azure OpenAI, Speech, Vision, Language and Foundry all share the
`Microsoft.CognitiveServices/accounts` resource type, so filtering by type
would fold all of them into the Foundry row. If the app registration cannot
list resources, pin the resource IDs under **Settings → Microsoft → Foundry**,
one per line.

#### Refresh rate and lag

Cost Management data lags **8–24 hours** (up to 72 on pay-as-you-go) and is
refreshed about six times a day, so there is nothing to gain from polling it
often. The tile therefore fetches **at most once per hour** regardless of the
app's refresh interval, and serves its cached result in between with the
timestamp of the real fetch. It also honours the API's own back-off headers on
a 429. Cost Management rate limits are shared across your whole tenant, so this
throttle protects anything else you run against the same subscription.

The tile's top row always says which date the numbers are actually from.

#### Checking it against a real account

```bash
python -m aigauge.providers.azure --probe
```

Prints the offer type, how many Foundry resources were found, the service
names and currency the API actually returned, the bucket breakdown, the
budget, and the forecast — with all IDs and resource names stripped.

### Multiple Claude / Codex accounts

Claude and Codex can track more than one subscription at a time. Open **Settings → Claude** or **Settings → Codex**, click **Add another**, give the account a short name, then use **Sign in** or **Paste cookie** for that specific row. The default account displays as `Claude` or `Codex`; named accounts display as `Claude (Work)`, `Codex (Account 2)`, etc.

The **General** tab controls provider groups. If Claude is checked, all configured Claude accounts appear; if Codex is checked, all configured Codex accounts appear. Secondary accounts can be removed from their provider tab. Each Claude/Codex account uses separate cookie storage, browser profile data, widget tile state, and history records.

Sessions persist between runs under the per-OS app-data directory:

| OS      | App data                                  | Secrets backend                           |
| ------- | ----------------------------------------- | ----------------------------------------- |
| Windows | `%APPDATA%/ai-gauge/`                     | Credential Manager (GitHub PAT, OpenRouter keys, Azure client secret) + DPAPI-encrypted `secrets.dat` (cookies, since the Credential Manager blob limit is too small for ChatGPT JWTs) |
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
- **Hide unused providers:** uncheck Claude / Codex / Copilot / Microsoft Azure / OpenRouter in Settings to remove their group from the widget — useful if you only use one or two of them.
- Auto-refresh is adaptive: manual refresh or changed usage enters the active
  cadence, then unchanged results back off toward the configured max interval.
  Defaults are 5 min active and 60 min idle max.
- Enable **Start at login** in Settings if you want it to run as a daily utility.

## Usage meters (Claude / Codex)

Claude and Codex publish more than one meter, and both providers reshuffle
their usage pages regularly. AI Gauge reads **every meter its catalog knows
about**, and each becomes its own row on the tile:

| Provider | Meters read | Drives the tray colour |
| --- | --- | --- |
| Claude | Session (5 h), Weekly (7 d), Opus only, Sonnet only, Cowork only, Claude Design, Daily routine runs | Session + Weekly |
| Codex | Session (5 h), Weekly (7 d), plus any additional usage card the page shows | Session + Weekly |

Everything else is **informational**: those rows appear when you expand the
tile (the **▸ Show details** button in the tile header), and they never change
the tray / menu-bar colour. That is deliberate — Opus sitting at 91% of a sub-limit is not
the same thing as your account being at 91% of its quota.

### Re-scan meters

Once a week, on the first refresh after seven days, each provider is asked for
*every* labelled row its usage page renders — not just the ones the catalog
recognises. New rows are added as informational meters and appear on the next
refresh. **Settings → General → Re-scan meters now** runs it immediately.

The scan is entirely local: it reads what the embedded browser already
rendered. Nothing is downloaded, and there is no remote catalog.

### Editing the meter catalog

The shipped definitions live in `src/aigauge/providers/meter_catalog/` and are
overlaid by an editable file in your app-data directory:

```
<app data>/meter_catalog/claude.json
<app data>/meter_catalog/codex.json
```

(`%APPDATA%/ai-gauge/`, `~/Library/Application Support/ai-gauge/`, or
`~/.config/ai-gauge/` — see the table above.) Entries are matched by `key`, and
an override changes only the fields it names:

```json
{
  "version": 1,
  "kind": "claude",
  "meters": [
    { "key": "cowork_only", "enabled": false },
    { "key": "weekly_all", "aliases": ["All models", "Weekly", "Weekly limit"] }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `key` | Identifies the meter. An unknown key adds a new meter; a known one edits it. An entry for a new key needs `label` and `aliases` as well — one that only names a key it does not recognise is dropped with a warning in the log. |
| `label` | What the tile shows. Also the history key (`provider::label`), so changing it starts that meter's history over. **Do not rename one meter's `label` to another meter's**: two entries sharing a label share one history key, which merges the two series and reads as a period rollover on every refresh — whichever entry the loader saw last wins, so the tile can show either meter's number under that name. Renaming a bundled meter to something new is fine; it just starts a fresh history. |
| `aliases` | The wordings the page may use. Add one here when a provider renames a row and the tile stops reading it. |
| `window_seconds` | The meter's period, or `null` if unknown. Drives the reset countdown and the pace line in the tile's tooltip ("you are 40% through the window"), so a wrong value is worse than none. |
| `boundaries` | Where this meter's text stops, for Codex's plain-text fallback: the app reads from the meter's alias up to the first of these words. Defaults to every *other* meter's aliases, which is normally right — set it when a page puts something else between the cards. |
| `primary` | `true` lets the meter drive the tray colour. Only Session and Weekly ship as primary, and a discovered meter is never adopted as primary. |
| `enabled` | `false` hides the meter, stops the app looking for it, **and stops the weekly scan adopting that row again** under a new key. This is how you switch off a row the scan picked up. |
| `polarity` | `"used"` or `"remaining"`, consulted **only** when the page shows no wording beside the percentage. Nothing ships with one: without wording the app refuses the reading rather than guessing, because a quota shown backwards is the one error that does not announce itself. |
| `status` | `"active"` (the default) means the meter is read. Reserved for a review step; anything else parks the entry — it is not read, and not adopted again either. |

Every entry also records where it came from, so a meter the app added for
itself can be judged afterwards — the page has moved on by the time you look:

| Field | Meaning |
| --- | --- |
| `source` | `"bundled"` for a shipped meter, `"discovery"` for one the weekly scan adopted, `"user"` for one you added by hand. Written once, when the entry is created: an entry you add without a `source` becomes `"user"` and stays that way. |
| `first_seen` | When it was adopted (local time, ISO-8601). |
| `account_id` | Which account's page it was seen on. |
| `evidence` | The row text that justified it — label, percentage, and reset wording, capped at 200 characters with any email address redacted. |

Unrecognised fields are preserved rather than dropped, so a newer release can
add one without an older build eating it. If the file cannot be parsed — a
trailing comma is the usual one — it is moved to `<kind>.json.corrupt` before
anything is written over it, and the log says so; a second corruption keeps
that first copy and discards the newer one, because the first is the one
holding your edits. If the file cannot be *read* at all — locked by an editor
or a backup agent — nothing is written over it and the adoption is skipped
until the next scan. Delete the override file to go back to the shipped
catalog; the weekly scan will re-adopt anything the page still shows and you
have not disabled.

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

Known open items, deliberate non-fixes and the reasoning behind past decisions are tracked in [docs/next-session.md](docs/next-session.md) — GitHub Issues are disabled on this repo, so that file is the tracker.

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
1.2.0+cfa.4
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
| `Claude's usage layout changed: could not read <row> …`<br>`Codex's usage layout changed: could not read <row> …` | The page rendered but the reading could not be justified — either several meters share one container so the percentage cannot be attributed to a row, or there is no "used"/"remaining" wording beside the number. The tile refuses rather than showing a number that may be inverted or belong to a different meter. The blob carries the row's text. Only Session and Weekly can produce this; an unreadable informational row is skipped instead. |
| `extractor retry limit exceeded` | The page loaded but never finished rendering usage within the time allowed. |
| `page failed to load` | A network or browser-level failure. The diagnostics carry Chromium's reason, e.g. `net::ERR_CONNECTION_RESET`. |
| `Not signed in to <provider>` | The stored session expired. Re-run sign-in, or use **Paste cookie** in Settings. |

Diagnostics for Claude also include an `api` section recording the *shape* of
the JSON the page fetched — field names, numbers and timestamps, with all other
strings reduced to a length. It never contains a response body and never leaves
your machine. See "API response shapes" in [SECURITY.md](SECURITY.md).

## Notes / limitations

- **Why an embedded browser instead of reading Chrome cookies?** Chrome 127+ added App-Bound Encryption (mid-2024) that blocks every external Python library from decrypting Chrome/Edge cookies. Owning the browser session ourselves is the only reliable workaround.
- **Claude / Codex layouts may change.** A renamed row is usually a one-line fix: add the new wording to `aliases` in your `meter_catalog` override (see [Editing the meter catalog](#editing-the-meter-catalog)), no rebuild needed. A restructured page can still need the extractor JS in `src/aigauge/providers/{claude,codex}.py` adjusted — the rest of the app keeps working either way.
- The Copilot REST endpoint returns the _current calendar month_ of billing usage. The widget tracks gross AI credits consumed against the included allowance; net quantity/amount is only the billable overage. Reset is computed as the 1st of the next month. GitHub does not currently expose a reliable personal-plan allowance field, so Settings uses a plan dropdown with a Custom fallback. Annual/request-based accounts are handled with a legacy premium-request fallback.
- **Copilot usage lags upstream.** The Copilot REST endpoint updates noticeably slower than Claude or Codex — credit counts can take hours to reflect recent activity. The widget shows the most recent value GitHub returns; treat the Copilot tile as a trailing indicator, not real-time.
- **Copilot AI credits.** GitHub moved Copilot from per-request quotas to token-based AI credits. Code completions and next edit suggestions remain included for paid plans, while Chat, CLI, cloud agent, Spaces, Spark, and third-party coding agents consume AI credits. The app shows the credit usage GitHub returns; if your account is org-billed, enter the billing organization so AI Gauge reads the organization billing pool.
- **OpenRouter uses two key types.** The inference key is used for `/key` spend data. The management key is required for `/credits` account balance and `/activity` model history. Without a management key, AI Gauge still shows key-level spend but cannot show balance or model activity.
- **OpenRouter time windows are UTC.** Today/month spend come from OpenRouter's current UTC day and month fields. Model activity comes from OpenRouter's default `/activity` history window: the last 30 completed UTC days, excluding the current UTC day.
- **Azure spend is gross of credits, and lags.** Cost Management excludes free and prepaid credits, so the Azure gauge measures consumption against an allowance you state — it is not a live credit balance. Data lags 8–24 h (up to 72 h on pay-as-you-go), so the tile fetches at most hourly and always shows the date the numbers are from. See [Azure month-to-date spend](#azure-month-to-date-spend).
- **Azure Sponsorship subscriptions are not supported by Cost Management.** They report zero cost while the credit drains; AI Gauge shows a warning row rather than an empty gauge.
- **Azure is one subscription at a time.** Multi-subscription roll-up is not implemented; point the tile at the subscription whose spend you care about, or use the resource-group filter to narrow it further.
