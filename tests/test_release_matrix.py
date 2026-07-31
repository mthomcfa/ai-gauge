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


def test_macos_packaging_archives_the_app_bundle():
    # build.sh emits dist/ai-gauge.app on macOS and dist/ai-gauge on Linux.
    # Archiving the wrong one produces an empty or unusable download.
    steps = {s.get("name"): s for s in _workflow("release.yml")["jobs"]["build"]["steps"]}
    pack = steps["Package (macOS / Linux tar.gz + sha256)"]["run"]
    assert "ai-gauge.app" in pack
    assert "macos-latest" in pack


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
