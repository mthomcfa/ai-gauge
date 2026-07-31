"""The fork's version scheme is load-bearing, so lock it down.

A bare, upstream-style number is ambiguous: this fork's 0.6.4 was built from
upstream v0.6.3 while upstream shipped its own unrelated v0.6.4, and upstream
also has a v0.6.5. The +cfa.N local segment is the only thing that makes a
build identifiable in a bug report, in the diagnostics dump, and in the app's
own title bar. CI enforces it via tools/check_versions.py; these tests make
sure that enforcement actually works and cannot rot.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_check_versions():
    spec = importlib.util.spec_from_file_location(
        "check_versions", REPO_ROOT / "tools" / "check_versions.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_versions"] = module
    spec.loader.exec_module(module)
    return module


def test_declared_version_is_a_valid_pep440_local_version():
    from aigauge import __version__

    parsed = Version(__version__)
    assert parsed.local is not None, (
        f"{__version__} has no local segment; it is indistinguishable from an "
        "upstream release of the same number"
    )
    assert parsed.local.startswith("cfa")


def test_pyproject_and_package_agree():
    from aigauge import __version__

    check = _load_check_versions()
    assert check._read_pyproject_version() == __version__


@pytest.mark.parametrize(
    "version,ok",
    [
        ("0.6.5+cfa.1", True),
        ("1.2.3+cfa.10", True),
        # Bare upstream-style numbers must be refused - this is the whole point.
        ("0.6.5", False),
        ("0.6.4", False),
        ("0.7.0", False),
        # Near-misses that would silently reintroduce ambiguity.
        ("0.6.5+cfa", False),
        ("0.6.5+dev.1", False),
        ("0.6.5-cfa.1", False),
        ("0.6.5+cfa.1.extra", False),
        ("", False),
    ],
)
def test_check_versions_enforces_the_fork_scheme(version, ok):
    check = _load_check_versions()
    failures: list[str] = []
    check._check_fork_scheme(version, failures)
    assert (failures == []) is ok, f"{version!r} should be {'accepted' if ok else 'rejected'}"


@pytest.mark.parametrize(
    "version,expected",
    [
        ("0.6.5+cfa.1", "0.6.5-cfa.1"),
        ("1.0.0+cfa.12", "1.0.0-cfa.12"),
        ("0.6.5", "0.6.5"),
    ],
)
def test_archive_version_strips_plus(version, expected):
    # GitHub normalises some characters in release asset names, so archives
    # never carry the '+'. The release workflow does the same substitution in
    # its file_version output.
    check = _load_check_versions()
    assert check.archive_version(version) == expected


def _release_workflow() -> dict:
    import yaml

    with (REPO_ROOT / ".github" / "workflows" / "release.yml").open(
        encoding="utf-8"
    ) as handle:
        return yaml.safe_load(handle)


def test_release_workflow_exposes_a_sanitized_file_version():
    workflow = _release_workflow()
    outputs = workflow["jobs"]["resolve-version"]["outputs"]
    assert "file_version" in outputs, "resolve-version must publish file_version"
    body = "\n".join(
        step.get("run", "") for step in workflow["jobs"]["resolve-version"]["steps"]
    )
    # The substitution itself, not merely the output's existence.
    assert "${version//+/-}" in body


@pytest.mark.parametrize("step_name", ["Package (Windows zip + sha256)",
                                       "Package (macOS / Linux tar.gz + sha256)"])
def test_packaging_steps_never_use_the_raw_version(step_name):
    # A '+' reaching an asset filename is exactly the failure this guards, and
    # the packaging steps are where the name is actually built.
    workflow = _release_workflow()
    steps = {s.get("name"): s for s in workflow["jobs"]["build"]["steps"]}
    assert step_name in steps, f"{step_name!r} not found; did the workflow change?"
    wired = "\n".join(str(v) for v in steps[step_name].get("env", {}).values())
    assert "outputs.file_version" in wired
    # Substring check would match file_version too, so strip it before looking.
    assert "outputs.version" not in wired.replace("outputs.file_version", "")


def _run_blocks(job: dict) -> list[tuple[str, str]]:
    return [
        (step.get("name", "<unnamed>"), step["run"])
        for step in job["steps"]
        if "run" in step
    ]


def test_release_workflow_never_interpolates_values_into_shell():
    """A git tag is attacker-supplied text; ${{ }} inside run: executes it.

    Git ref names permit ';', '$', '&' and '`', so a tag such as
    v0.0.0+cfa.0";id;:" injected straight into a bash comparison runs `id`
    before the version check can reject it. Values must arrive via env: and be
    referenced as quoted shell variables instead.
    """
    workflow = _release_workflow()
    offenders: list[str] = []
    for job_name, job in workflow["jobs"].items():
        for step_name, run in _run_blocks(job):
            if "${{" not in run:
                continue
            # matrix.* is workflow-authored, not attacker-controlled.
            leaked = [
                frag
                for frag in run.split("${{")[1:]
                if not frag.lstrip().startswith("matrix.")
            ]
            if leaked:
                offenders.append(f"{job_name}/{step_name}")
    assert not offenders, (
        "shell blocks interpolate non-matrix values directly: " + ", ".join(offenders)
    )


def test_release_tag_is_validated_before_use():
    # Rejecting a malformed tag up front keeps every downstream step from ever
    # seeing an unexpected string.
    workflow = _release_workflow()
    ver_step = next(
        s for s in workflow["jobs"]["resolve-version"]["steps"] if s.get("id") == "ver"
    )
    assert "github.ref_name" in str(ver_step.get("env", {})), "tag must arrive via env"
    assert "cfa" in ver_step["run"] and "exit 1" in ver_step["run"], (
        "the tag must be pattern-checked against the fork scheme before use"
    )


def test_readme_and_changelog_track_the_current_version():
    from aigauge import __version__

    check = _load_check_versions()
    assert check._readme_mentions_version(__version__)
    assert check._changelog_has_section(__version__)


def test_fork_metadata_records_the_upstream_base():
    # The version number deliberately does NOT encode the upstream base, so
    # that provenance has to live somewhere machine-readable.
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    audit = data["tool"]["ai-gauge-audit"]
    assert audit["audited_upstream_commit"]
    assert audit["audited_upstream_tag"].startswith("v")
    assert audit["fork_repo"].endswith("mthomcfa/ai-gauge")
