"""Lock down which platforms the release workflow actually ships.

macOS was dropped from the release matrix once already (commit 3bd5d4b) while
the macOS *code* and *test* matrix stayed in place, so the app kept working on
macOS but silently stopped shipping a binary. The documentation then disagreed
with the workflow for several releases. These tests keep the three in sync:
what CI builds, what the release body tells users to download, and what the
README documents.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLATFORMS = ("windows", "macos", "linux")


def _workflow(name: str) -> dict:
    with (REPO_ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _release_matrix() -> list[dict]:
    return _workflow("release.yml")["jobs"]["build"]["strategy"]["matrix"]["include"]


@pytest.mark.parametrize("label", PLATFORMS)
def test_release_matrix_builds_every_supported_platform(label):
    labels = {entry["label"] for entry in _release_matrix()}
    assert label in labels, (
        f"{label} is not built by release.yml. PyInstaller cannot "
        f"cross-compile, so dropping the runner drops the artifact entirely."
    )


def test_release_matrix_entries_are_complete():
    # A half-filled entry fails at run time on a tag push, which is the worst
    # moment to discover it.
    for entry in _release_matrix():
        assert set(entry) == {"os", "label", "archive_ext", "build_cmd"}, entry
        assert entry["archive_ext"] in {"zip", "tar.gz"}
        assert entry["build_cmd"] in {"./build.ps1", "./build.sh"}


def _packaging_step() -> str:
    steps = {s.get("name"): s for s in _workflow("release.yml")["jobs"]["build"]["steps"]}
    name = "Package (macOS / Linux tar.gz + sha256)"
    assert name in steps, f"{name!r} not found; did the workflow change?"
    return steps[name]["run"]


def test_macos_packaging_archives_the_app_bundle():
    """Assert what is archived, not merely that the words appear somewhere.

    An earlier version of this test checked that the strings "ai-gauge.app"
    and "macos-latest" appeared anywhere in the step. Changing the tar target
    to a hardcoded `ai-gauge` left both strings intact in the untouched
    if/else and the whole suite stayed green - while the macOS release shipped
    a bare Unix folder with no Info.plist. PyInstaller emits BOTH
    dist/ai-gauge/ and dist/ai-gauge.app on macOS, so the existence guard
    would have passed and nothing would have caught it before a user
    extracted the archive.
    """
    import re

    pack = _packaging_step()
    # The bundle must be what the macOS branch selects...
    assert re.search(r'payload="ai-gauge\.app"', pack), (
        "macOS branch must select the .app bundle"
    )
    # ...and tar must archive the selected payload, not a hardcoded name.
    assert re.search(r'tar -C dist -czf "\$archive" "\$payload"', pack), (
        "tar must archive $payload; a hardcoded target silently ships the "
        "wrong artifact"
    )


def test_macos_branch_keys_on_the_runner_not_the_label():
    # Keying on the matrix label means renaming the runner (macos-14,
    # macos-latest-large) silently falls through to the non-.app folder.
    pack = _packaging_step()
    assert 'RUNNER_OS" = "macOS"' in pack or "RUNNER_OS' = 'macOS'" in pack, (
        "payload selection must key on RUNNER_OS, which the runner itself sets"
    )


def test_packaging_fails_loudly_when_the_payload_is_missing():
    # Without this, a failed build tars nothing, the attestation is minted
    # over an empty archive, and the release publishes it.
    pack = _packaging_step()
    assert 'if [ ! -e "dist/$payload" ]' in pack
    assert "exit 1" in pack


def test_test_matrix_still_covers_macos():
    # The macOS menu-bar UI is a distinct code path (macos_status_item.py) and
    # has shipped broken before, so it must stay under CI even though the
    # maintainer develops on Windows.
    matrix = _workflow("test.yml")["jobs"]["test"]["strategy"]["matrix"]
    assert any("macos" in os_name for os_name in matrix["os"])


@pytest.mark.parametrize("label", PLATFORMS)
def test_release_body_lists_every_built_artifact(label):
    steps = _workflow("release.yml")["jobs"]["release"]["steps"]
    body = next(s for s in steps if s.get("name") == "Create draft GitHub Release")
    text = body["with"]["body"]
    assert f"-{label}." in text, f"release notes do not mention the {label} artifact"


def test_readme_documents_every_built_artifact():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    download = readme.split("## Download", 1)[1].split("\n## ", 1)[0]
    for label in PLATFORMS:
        assert f"-{label}." in download, f"README download table omits {label}"


def test_unsigned_macos_caveat_is_documented():
    # An unsigned .app reports itself as "damaged" rather than "unsigned", so
    # without this caveat a correct download looks like a corrupt one.
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "com.apple.quarantine" in readme
    steps = _workflow("release.yml")["jobs"]["release"]["steps"]
    body = next(s for s in steps if s.get("name") == "Create draft GitHub Release")
    assert "com.apple.quarantine" in body["with"]["body"]


def test_build_script_resigns_after_mutating_the_bundle():
    """PyInstaller ad-hoc-signs the .app as the LAST step of BUNDLE.assemble.

    build.sh then relocates QtWebEngineCore's framework layout and edits
    Info.plist, both of which invalidate that seal. On Apple Silicon an
    invalidly-signed Mach-O is refused at exec by AMFI, and clearing the
    quarantine flag does not help - so the bundle must be re-signed after the
    edits, not before.
    """
    script = (REPO_ROOT / "build.sh").read_text(encoding="utf-8")
    sign = script.find("codesign --force")
    assert sign != -1, "build.sh must re-sign the bundle"
    for mutation in ("PlistBuddy", 'rm -rf "$STRAY"'):
        assert script.find(mutation) < sign, (
            f"{mutation!r} runs after the re-sign, which re-breaks the signature"
        )
    assert "codesign --verify --deep --strict" in script


def test_release_verifies_the_macos_signature_before_publishing():
    pack = _packaging_step()
    assert "codesign --verify --deep --strict" in pack, (
        "CI must verify the bundle signature; an unlaunchable .app would "
        "otherwise be attested and published"
    )


def test_macos_artifact_is_labelled_as_apple_silicon_only():
    # macos-latest is arm64 and PyInstaller builds for the host arch, so the
    # archive does not run on Intel. Saying nothing sends Intel users into the
    # "damaged" troubleshooting path, which cannot help them.
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Apple Silicon" in readme
    assert "Intel" in readme
    steps = _workflow("release.yml")["jobs"]["release"]["steps"]
    body = next(s for s in steps if s.get("name") == "Create draft GitHub Release")
    assert "Apple Silicon" in body["with"]["body"]
