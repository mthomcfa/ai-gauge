"""The fork's version scheme is load-bearing, so lock it down.

A bare, upstream-style number is ambiguous: this fork's 0.6.4 was built from
upstream v0.6.3 while upstream shipped its own unrelated v0.6.4, and upstream
also has a v0.6.5. The +cfa.N local segment is the only thing that makes a
build identifiable in a bug report, in the diagnostics dump, and in the app's
panel header. CI enforces it via tools/check_versions.py; these tests make
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


def test_no_workflow_step_builds_a_filename_from_the_raw_version():
    """A '+' reaching an asset filename is the failure this guards.

    Deliberately sweeps EVERY step of every job rather than naming the
    packaging steps: an earlier version of this test named them, and a
    cosmetic rename was enough to make it error out. Worse, when it did match,
    it inspected only two steps - the release-notes body could regress to the
    raw version with the whole suite still green.
    """
    import re

    # Only filenames matter. The release *title* legitimately shows the real
    # version, '+' and all, so a blanket "no outputs.version anywhere" rule
    # would be wrong rather than merely strict.
    interpolated = re.compile(r"ai-gauge-\$\{\{([^}]+)\}\}")
    shell_var = re.compile(r"ai-gauge-[\$\{]+([A-Za-z_][A-Za-z0-9_]*)")

    workflow = _release_workflow()
    offenders: list[str] = []
    checked = 0
    for job_name, job in workflow["jobs"].items():
        for step in job["steps"]:
            label = f"{job_name}/{step.get('name', '<unnamed>')}"
            env = step.get("env", {})
            blob = step.get("run", "") + "\n" + str(step.get("with", ""))
            for expr in interpolated.findall(blob):
                # matrix.label alone carries no version; only flag a name that
                # actually interpolates one.
                if "version" not in expr:
                    continue
                checked += 1
                if "file_version" not in expr:
                    offenders.append(f"{label}: ai-gauge-${{{{{expr}}}}}")
            if shell_var.search(blob):
                # The shell-local name ("$version") rarely matches the env key
                # ("FILE_VERSION"), so resolving by name misses the regression.
                # Instead require that a step which builds an archive name is
                # not handed the raw version at all.
                for key, value in env.items():
                    if "version" not in str(value):
                        continue
                    checked += 1
                    if "file_version" not in str(value):
                        offenders.append(f"{label}: env {key}={value}")
    assert checked, "no archive-name construction found; did the workflow change?"
    assert not offenders, (
        "these build a filename from the raw version, which contains '+': "
        + "; ".join(offenders)
    )


def test_main_rejects_a_bare_version(monkeypatch):
    """The scheme check must be wired into main(), not merely defined.

    Testing the helper in isolation proves nothing: deleting the single call
    in main() leaves the helper, its docstring and every parametrised case
    passing, while `python tools/check_versions.py` starts accepting a bare
    0.6.6 - the exact ambiguity this whole scheme exists to prevent.
    """
    check = _load_check_versions()
    monkeypatch.setattr(check, "_read_pyproject_version", lambda: "0.6.6")
    monkeypatch.setattr(check, "_read_init_version", lambda: "0.6.6")
    monkeypatch.setattr(check, "_readme_mentions_version", lambda v: True)
    monkeypatch.setattr(check, "_changelog_has_section", lambda v: True)
    assert check.main() == 1


def test_main_accepts_a_well_formed_fork_version(monkeypatch):
    check = _load_check_versions()
    monkeypatch.setattr(check, "_read_pyproject_version", lambda: "9.9.9+cfa.3")
    monkeypatch.setattr(check, "_read_init_version", lambda: "9.9.9+cfa.3")
    monkeypatch.setattr(check, "_readme_mentions_version", lambda v: True)
    monkeypatch.setattr(check, "_changelog_has_section", lambda v: True)
    assert check.main() == 0


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
