# Contributing to AI Gauge

> **This is [mthomcfa/ai-gauge](https://github.com/mthomcfa/ai-gauge), a
> security-hardening fork of
> [jpajak/ai-gauge](https://github.com/jpajak/ai-gauge).** It has its own
> version numbers and its own hardening that upstream does not carry — see
> [Relationship to upstream](README.md#relationship-to-upstream). General
> feature work is usually better contributed upstream; this fork prioritises
> security fixes and takes upstream features selectively.

Thanks for your interest. AI Gauge is a small cross-platform desktop utility,
so most contributions fall into one of three buckets:

- **Provider layout fixes** — when Claude, Codex, or OpenCode change their
  usage page and a tile starts showing `error · layout changed`. These are the
  most common and most welcome PRs.
- **Bug reports and small bug fixes** — anything around tray, widget,
  cookie storage, settings, or refresh logic.
- **New providers or new features** — please open an issue first so we can
  agree on scope before you write code.

## Development environment

Requirements: **Windows 10/11, macOS, or Linux** and **Python 3.11+**. The
test suite runs headlessly under `QT_QPA_PLATFORM=offscreen`; manual smoke
testing is still useful on each OS because tray/menu-bar behavior and native
credential storage are platform-specific.

```powershell
git clone https://github.com/mthomcfa/ai-gauge.git
cd ai-gauge

py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .[dev]
```

Run the app:

```powershell
.\.venv\Scripts\python.exe -m aigauge
```

Run the tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The version-sync check that gates CI:

```powershell
.\.venv\Scripts\python.exe tools\check_versions.py
```

## Pull request expectations

- Keep changes focused. One PR per logical change.
- Add or update tests when you change non-trivial logic.
- Run `python -m pytest` and `tools/check_versions.py` locally before opening the PR;
  both run in CI on push and pull request.
- If you bump the version, update `pyproject.toml`, `src/aigauge/__init__.py`,
  `README.md`, and add a `CHANGELOG.md` section. CI will fail otherwise.
- **Fork versions must carry the `+cfa.N` local segment** (e.g. `0.6.5+cfa.1`).
  `tools/check_versions.py` rejects a bare number, because it would collide
  with a real upstream release of the same value but different code. The number
  before `+` is this fork's own counter, not a claim about the upstream base —
  see [Versioning](README.md#versioning).
- Avoid logging cookies, PATs, or full provider response bodies. The logger
  is rotated under the per-OS app-data directory, and users may attach those
  logs to issues.

## Reporting bugs

Use the issue templates under [.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/):

- **Bug report** — generic crashes, UI issues, settings glitches.
- **Provider layout broken** — a Claude, Codex, or OpenCode tile started failing.
- **Feature request** — new ideas worth discussing before code.

For anything that exposes session cookies or tokens, please follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Code style

- Python: standard `from __future__ import annotations`, type hints encouraged,
  no formatter is enforced but please keep diffs minimal.
- Tests: use `pytest` and `pytest-qt`. Avoid mocking the database or the
  filesystem when an in-memory or `tmp_path` alternative works.
- **Write tests that fail when the behaviour they cover is removed.** Several
  tests in this repo have shipped green while asserting nothing — matching a
  word that also appeared in a comment, or inspecting the wrong lines of a
  workflow. Before trusting a new test, revert the code it covers and confirm
  it actually fails.
- Comments should explain *why*, not *what*. The repo's existing style favors
  short, sparse comments over docstring boilerplate.

## License

By contributing, you agree that your contributions are licensed under the
same MIT license that covers the rest of the project. See [LICENSE](LICENSE).
