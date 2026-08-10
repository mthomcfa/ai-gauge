"""Keep the repository's documentation honest about the fork.

Documentation in this repo has drifted from the code more than once: the README
pointed downloads at upstream's releases for several commits, RELEASING
described a 3-OS matrix after macOS had been dropped, and the issue templates
went on asking for a bare `0.5.0` version long after bare numbers became
ambiguous. Prose has no compiler, so the load-bearing claims get asserted here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
FORK_REPO = "mthomcfa/ai-gauge"
UPSTREAM_REPO = "jpajak/ai-gauge"

DOCS = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "RELEASING.md",
    "AI Gauge-datasheet.md",
]


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", DOCS)
def test_no_doc_sends_users_to_upstream_for_this_forks_artifacts(name):
    """Upstream links are fine as attribution, not as a destination.

    The README shipped a download table and a CI badge pointing at upstream,
    so the front page of a security-hardening fork recommended the unhardened
    binaries. Any upstream link that is a *release*, *download*, *action*, or
    *advisory* URL is the bug; plain repo links are attribution.
    """
    text = _read(name)
    lines = text.splitlines()
    bad = []
    for i, line in enumerate(lines):
        if f"{UPSTREAM_REPO}/releases" in line or f"{UPSTREAM_REPO}/actions" in line:
            bad.append(line.strip())
        elif f"{UPSTREAM_REPO}/security" in line:
            # A secondary "report upstream too" path is correct; a primary one
            # is the bug. Judge from the surrounding paragraph, not the line -
            # the qualifying clause is usually wrapped onto another line.
            window = " ".join(lines[max(0, i - 4) : i + 3]).lower()
            if not any(word in window for word in ("also", "as well", "upstream too")):
                bad.append(line.strip())
    assert not bad, f"{name} points at upstream for our own artifacts: {bad}"

    if f"{UPSTREAM_REPO}/security" in text:
        # Ours must come first, so the primary route is unambiguous.
        assert text.index(f"{FORK_REPO}/security") < text.index(
            f"{UPSTREAM_REPO}/security"
        ), f"{name} offers upstream's advisory form before this fork's"


def test_readme_badge_tracks_this_repo():
    readme = _read("README.md")
    badge_lines = [ln for ln in readme.splitlines() if "badge.svg" in ln]
    assert badge_lines, "README has no CI badge"
    for line in badge_lines:
        assert FORK_REPO in line, f"badge shows another repo's status: {line}"


def test_readme_states_it_is_a_fork_above_the_fold():
    # Someone landing on the repo must learn this before they download.
    head = "\n".join(_read("README.md").splitlines()[:20])
    assert "fork" in head.lower()
    assert UPSTREAM_REPO in head


@pytest.mark.parametrize(
    "template",
    ["bug_report.yml", "provider_layout_broken.yml"],
)
def test_issue_templates_ask_for_the_full_fork_version(template):
    """A bare number in the placeholder trains users to report ambiguously."""
    path = REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / template
    with path.open(encoding="utf-8") as handle:
        form = yaml.safe_load(handle)
    version_fields = [
        item for item in form["body"] if item.get("id") == "version"
    ]
    assert version_fields, f"{template} does not ask for a version"
    for field in version_fields:
        placeholder = field["attributes"].get("placeholder", "")
        assert "+cfa." in placeholder, (
            f"{template} placeholder {placeholder!r} is a bare upstream-style "
            "number"
        )


def test_issue_templates_are_valid_github_forms():
    for path in sorted((REPO_ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml")):
        with path.open(encoding="utf-8") as handle:
            form = yaml.safe_load(handle)
        if path.name == "config.yml":
            assert "contact_links" in form
            continue
        assert form.get("name") and form.get("description")
        assert isinstance(form.get("body"), list) and form["body"]


def test_security_policy_routes_reports_to_this_fork():
    text = _read("SECURITY.md")
    assert f"{FORK_REPO}/security/advisories/new" in text
    # Upstream is still named, because shared code affects both.
    assert UPSTREAM_REPO in text


def test_documented_verification_steps_are_actually_given():
    """Telling users to verify without showing how produces no verification."""
    readme = _read("README.md")
    assert "gh attestation verify" in readme
    # Both, not either: an `or` here let the macOS/Linux command be deleted
    # while the PowerShell one kept the test green.
    assert "shasum -a 256" in readme, "no SHA256 check given for macOS/Linux"
    assert "Get-FileHash" in readme, "no SHA256 check given for Windows"
    # gh is not preinstalled anywhere; saying so is part of the instruction.
    assert "cli.github.com" in readme or "gh auth login" in readme


def test_docs_do_not_advertise_a_platform_ci_does_not_build():
    """The README once listed a macOS archive that CI had stopped producing."""
    with (REPO_ROOT / ".github" / "workflows" / "release.yml").open(
        encoding="utf-8"
    ) as handle:
        workflow = yaml.safe_load(handle)
    built = {e["label"] for e in workflow["jobs"]["build"]["strategy"]["matrix"]["include"]}
    download = _read("README.md").split("## Download", 1)[1].split("\n## ", 1)[0]
    advertised = {label for label in ("windows", "macos", "linux") if f"-{label}." in download}
    assert advertised <= built, (
        f"README advertises archives CI does not build: {advertised - built}"
    )


@pytest.mark.parametrize("name", ["README.md", "AI Gauge-datasheet.md"])
def test_provider_list_is_complete(name):
    # OpenCode shipped as a provider but stayed missing from the datasheet and
    # the layout-bug template for several releases.
    text = _read(name).lower()
    for provider in ("claude", "codex", "copilot", "openrouter", "opencode"):
        assert provider in text, f"{name} does not mention {provider}"


def test_upstream_staging_files_are_not_addressed_to_this_repo():
    """These are drafts for upstream; they must not describe fork-only work.

    They are never filed automatically - posting them is a human action - but
    they should be accurate if and when they are.
    """
    for name in ("upstream-issue.md", "upstream-pr.md"):
        text = (REPO_ROOT / "audit-artifacts" / name).read_text(encoding="utf-8")
        assert "not filed" in text.lower() or "deliberate human action" in text.lower()
        assert UPSTREAM_REPO in text


# --- build prerequisites ---------------------------------------------------
#
# build.ps1 declares "#requires -version 7", so Windows PowerShell 5.1 - still
# the default powershell.exe on Windows 10/11 - refuses to run it and reports a
# #requires error rather than a build failure. That requirement was stated
# nowhere: not in the README's build section, not in CONTRIBUTING's stated
# requirements, not in RELEASING's Windows build step, and not in the upgrade
# instructions sent to a user, who then lost a build cycle to it.

_WINDOWS_BUILD_DOCS = ["README.md", "CONTRIBUTING.md", "RELEASING.md"]


def _required_powershell_major() -> int:
    """Read the requirement from build.ps1 rather than restating it."""
    import re

    source = (REPO_ROOT / "build.ps1").read_text(encoding="utf-8")
    match = re.search(r"#requires\s+-version\s+(\d+)", source, re.IGNORECASE)
    assert match, "build.ps1 no longer declares a #requires version"
    return int(match.group(1))


@pytest.mark.parametrize("name", _WINDOWS_BUILD_DOCS)
def test_docs_state_the_powershell_version_build_ps1_demands(name):
    """Anchored on build.ps1, so bumping it fails here instead of silently.

    The point is not that the words "PowerShell 7" appear somewhere; it is that
    whatever version build.ps1 actually demands is the version the docs name.
    """
    major = _required_powershell_major()
    text = _read(name)

    assert f"PowerShell {major}" in text, (
        f"{name} documents the Windows build but never says it needs "
        f"PowerShell {major}"
    )


@pytest.mark.parametrize("name", _WINDOWS_BUILD_DOCS)
def test_docs_name_the_shell_that_actually_works(name):
    # "PowerShell 7" alone is not actionable on a machine whose Start menu says
    # "Windows PowerShell": the executable is called pwsh, and that is the word
    # a stuck reader needs.
    assert "pwsh" in _read(name), f"{name} does not name pwsh"
