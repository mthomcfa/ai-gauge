"""Verify the version string is consistent across files that reference it.

Run from the repo root:

    python tools/check_versions.py

Exits non-zero if any tracked file disagrees. Used both by CI (test.yml) and
the release checklist in RELEASING.md.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# <release>+cfa.<n> - see _check_fork_scheme().
_FORK_VERSION_RE = re.compile(r"\d+\.\d+\.\d+\+cfa\.\d+")


def _read_pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("could not find version in pyproject.toml")
    return m.group(1)


def _read_init_version() -> str:
    text = (REPO_ROOT / "src" / "aigauge" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("could not find __version__ in src/aigauge/__init__.py")
    return m.group(1)


def _readme_mentions_version(version: str) -> bool:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    return version in text


def _changelog_has_section(version: str) -> bool:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.search(rf"(?m)^##\s+{re.escape(version)}\b", text) is not None


def _check_fork_scheme(version: str, failures: list[str]) -> None:
    """Require the fork's PEP 440 local segment.

    Fork releases must carry ``+cfa.N``. Without it the number collides with a
    real upstream release of the same value but entirely different code: this
    fork's 0.6.4 was built from upstream v0.6.3 while upstream shipped its own,
    unrelated v0.6.4. A bare number is therefore ambiguous in bug reports, in
    the diagnostics dump, and in the app's panel header, so CI rejects one.
    """
    if not _FORK_VERSION_RE.fullmatch(version):
        failures.append(
            f"version {version!r} does not match the fork scheme "
            f"<release>+cfa.<n> (e.g. 0.6.5+cfa.1). A bare upstream-style "
            f"number is ambiguous - see 'Versioning' in README.md."
        )


def archive_version(version: str) -> str:
    """Filename-safe form of a version.

    '+' is legal in a PEP 440 version and in a git tag, but GitHub normalises
    some non-alphanumerics in release *asset* names. Rather than depend on
    unverified behaviour there, archives use a '-' and the version string keeps
    the '+'.
    """
    return version.replace("+", "-")


def main() -> int:
    pyproject = _read_pyproject_version()
    init = _read_init_version()

    failures: list[str] = []

    if pyproject != init:
        failures.append(
            f"pyproject.toml version ({pyproject}) != src/aigauge/__init__.py version ({init})"
        )

    _check_fork_scheme(pyproject, failures)

    if not _readme_mentions_version(pyproject):
        failures.append(f"README.md does not mention version {pyproject}")

    if not _changelog_has_section(pyproject):
        failures.append(f"CHANGELOG.md has no '## {pyproject}' section")

    if failures:
        print("Version check FAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1

    print(f"Version check OK: {pyproject}")
    # The release checklist and the README both need the filename form, and
    # deriving it by hand is how a '+' ends up in an asset name.
    print(f"  archive file-version: {archive_version(pyproject)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
