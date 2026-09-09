"""The meter catalog — what the app believes a provider's usage page shows.

The catalog exists because hardcoded English labels broke three times in one
week. These tests pin the two properties the rest of the app leans on:

* the *bundled* catalog reproduces the labels the extractors used to carry, so
  moving them into data changed nothing that was working;
* ``label`` is stable and independent of the page's wording, because it is the
  history key (``history._state_key``) and a page relabel must not fork a
  metric's history.

Everything writes through an explicit ``base_dir``; the autouse ``APPDATA``
fixture in conftest already redirects ``app_data_dir()`` for the cases that do
not pass one.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aigauge.config import Config, app_data_dir
from aigauge.history import HistoryStore
from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot
from aigauge.providers.catalog import (
    BREAKDOWN_TAG,
    CATALOG_SCAN_INTERVAL,
    MAX_ADOPTED_METERS,
    MAX_EVIDENCE_CHARS,
    SOURCE_DISCOVERY,
    STATUS_ACTIVE,
    MeterCatalog,
    MeterSpec,
    _spec_to_raw,
    adopt_rows,
    bundled_catalog,
    clear_scans,
    extractor_source,
    is_adoptable_label,
    load_catalog,
    metric_for_spec,
    normalize_label,
    override_path,
    record_scan,
    row_evidence,
    scan_due,
    unreadable_reason,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The literal list the Claude extractor carried before the catalog existed.
# Order included: findRowByLabel scores against it, so a changed set changes
# which container a label resolves to.
LEGACY_CLAUDE_ROW_LABELS = [
    "Current session",
    "All models",
    "Weekly",
    "Opus only",
    "Sonnet only",
    "Cowork only",
    "Claude Design",
    "Daily included routine runs",
]


def _write_override(base_dir: Path, kind: str, meters: list[dict]) -> Path:
    path = base_dir / f"{kind}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "kind": kind, "meters": meters}), encoding="utf-8"
    )
    return path


# --- the bundled catalog ---------------------------------------------------


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_bundled_catalog_loads_and_has_unique_keys(kind):
    catalog = bundled_catalog(kind)

    assert catalog.specs, f"{kind}.json defines no meters"
    keys = [spec.key for spec in catalog.specs]
    assert len(keys) == len(set(keys))
    assert all(spec.aliases for spec in catalog.specs)


def test_claude_catalog_reproduces_the_labels_the_extractor_used_to_carry():
    assert list(bundled_catalog("claude").aliases()) == LEGACY_CLAUDE_ROW_LABELS


@pytest.mark.parametrize(
    "kind,expected",
    [("claude", {"session": "Session", "weekly_all": "Weekly"}),
     ("codex", {"session": "Session", "weekly": "Weekly"})],
)
def test_only_the_two_established_meters_are_primary(kind, expected):
    """Primary meters drive the tray colour, so this set is load-bearing."""
    catalog = bundled_catalog(kind)
    primary = {spec.key: spec.label for spec in catalog.specs if spec.primary}

    assert primary == expected


def test_bundled_windows_match_the_previous_hardcoded_ones():
    claude = bundled_catalog("claude")
    codex = bundled_catalog("codex")

    assert claude.spec_for_key("session").window == timedelta(hours=5)
    assert claude.spec_for_key("weekly_all").window == timedelta(days=7)
    assert claude.spec_for_key("daily_routine_runs").window == timedelta(days=1)
    # Unknown period, deliberately: a wrong window would show a wrong countdown.
    assert claude.spec_for_key("claude_design").window is None
    assert codex.spec_for_key("session").window == timedelta(hours=5)
    assert codex.spec_for_key("weekly").window == timedelta(days=7)


def test_no_bundled_meter_carries_a_polarity_hint():
    """A hint is an escape hatch for a user, not a guess we ship.

    ``normalize_percent`` resolves an unknown kind to *used*, so shipping a
    hint would be shipping the "42% left reported as 42% consumed" bug the
    refusal path exists to prevent.
    """
    for kind in ("claude", "codex"):
        assert all(spec.polarity is None for spec in bundled_catalog(kind).specs)


# --- alias matching --------------------------------------------------------


@pytest.mark.parametrize(
    "label", ["Current session", "current session", "  CURRENT   SESSION "]
)
def test_alias_matching_ignores_case_and_whitespace(label):
    spec = bundled_catalog("claude").spec_for_label(label)

    assert spec is not None and spec.key == "session"


def test_an_unknown_label_matches_nothing():
    assert bundled_catalog("claude").spec_for_label("Cowork sessions") is None


# --- override merge --------------------------------------------------------


def test_override_updates_only_the_fields_it_names(tmp_path):
    _write_override(tmp_path, "claude", [{"key": "session", "window_seconds": 3600}])

    spec = load_catalog("claude", base_dir=tmp_path).spec_for_key("session")

    assert spec.window == timedelta(hours=1)
    # Untouched fields survive - especially the label, which is the history key.
    assert spec.label == "Session"
    assert spec.aliases == ("Current session",)
    assert spec.primary is True


def test_override_can_add_a_page_alias_for_a_relabelled_meter(tmp_path):
    _write_override(
        tmp_path,
        "claude",
        [{"key": "weekly_all", "aliases": ["All models", "Weekly", "Weekly limit"]}],
    )

    catalog = load_catalog("claude", base_dir=tmp_path)

    assert catalog.spec_for_label("Weekly limit").key == "weekly_all"
    assert "Weekly limit" in catalog.aliases()


def test_override_can_disable_an_adopted_meter(tmp_path):
    _write_override(tmp_path, "claude", [{"key": "opus_only", "enabled": False}])

    catalog = load_catalog("claude", base_dir=tmp_path)

    assert catalog.spec_for_key("opus_only").enabled is False
    assert "Opus only" not in catalog.aliases()
    assert catalog.spec_for_label("Opus only") is None


def test_override_appends_unknown_keys(tmp_path):
    _write_override(
        tmp_path,
        "claude",
        [{"key": "cloud_runs", "label": "Cloud runs", "aliases": ["Cloud runs"]}],
    )

    catalog = load_catalog("claude", base_dir=tmp_path)

    assert catalog.spec_for_key("cloud_runs").label == "Cloud runs"
    assert len(catalog.specs) == len(bundled_catalog("claude").specs) + 1


def test_an_unreadable_override_leaves_the_bundled_catalog_intact(tmp_path):
    path = tmp_path / "claude.json"
    path.write_text("{not json", encoding="utf-8")

    assert load_catalog("claude", base_dir=tmp_path).specs == bundled_catalog(
        "claude"
    ).specs


def test_an_override_entry_with_an_unsafe_key_is_dropped(tmp_path):
    _write_override(
        tmp_path,
        "claude",
        [{"key": "../../etc/passwd", "label": "Bad", "aliases": ["Bad"]}],
    )

    catalog = load_catalog("claude", base_dir=tmp_path)

    assert [spec.key for spec in catalog.specs] == [
        spec.key for spec in bundled_catalog("claude").specs
    ]


def test_the_override_path_lives_under_the_app_data_dir():
    assert override_path("claude") == app_data_dir() / "meter_catalog" / "claude.json"


# --- turning a row into a metric -------------------------------------------


def test_a_primary_meter_is_untagged_and_a_breakdown_meter_is_tagged():
    catalog = bundled_catalog("claude")
    row = {"percent": 40.0, "kind": "used", "reset_text": "2 hr"}

    session = metric_for_spec(catalog.spec_for_key("session"), row)
    opus = metric_for_spec(catalog.spec_for_key("opus_only"), row)

    assert session.tag is None
    assert opus.tag == BREAKDOWN_TAG
    assert (session.label, opus.label) == ("Session", "Opus only")


def test_a_remaining_row_is_converted_to_percent_used():
    spec = bundled_catalog("claude").spec_for_key("session")

    metric = metric_for_spec(spec, {"percent": 90.0, "kind": "remaining"})

    assert metric.percent_used == 10.0


def test_a_meter_with_no_known_window_never_reads_as_idle_when_it_has_a_reset():
    spec = bundled_catalog("claude").spec_for_key("claude_design")
    resets_at = datetime.now() + timedelta(days=40)

    metric = metric_for_spec(spec, {"percent": 0.0}, resets_at=resets_at)

    assert metric.window is None
    assert metric.reset_label is None
    assert metric.resets_at == resets_at


def test_a_bare_percentage_is_refused_rather_than_read_as_used():
    spec = bundled_catalog("claude").spec_for_key("session")

    assert unreadable_reason({"percent": 42.0, "kind": "unknown"}, spec) is not None


def test_a_polarity_hint_rescues_a_bare_percentage_when_the_user_sets_one():
    spec = MeterSpec(
        key="k", label="K", aliases=("K",), primary=False, polarity="remaining"
    )
    card = {"percent": 42.0, "kind": "unknown"}

    assert unreadable_reason(card, spec) is None
    assert metric_for_spec(spec, card).percent_used == 58.0


def test_a_row_with_no_percentage_is_absent_not_a_metric():
    spec = bundled_catalog("claude").spec_for_key("session")

    assert unreadable_reason({"percent": None}, spec) is None
    assert metric_for_spec(spec, {"percent": None}) is None


# --- discovery: adoption ---------------------------------------------------


def _row(label, percent=7.0, **extra):
    row = {
        "label": label,
        "percent": percent,
        "kind": "used",
        "reset_text": "3 days",
        "in_container": True,
    }
    row.update(extra)
    return row


def test_an_unknown_row_is_adopted_as_an_informational_meter(tmp_path):
    adopted = adopt_rows("claude", [_row("Cowork sessions")], base_dir=tmp_path)

    assert [spec.label for spec in adopted] == ["Cowork sessions"]
    assert adopted[0].primary is False
    assert adopted[0].adopted is True

    catalog = load_catalog("claude", base_dir=tmp_path)
    assert catalog.spec_for_label("Cowork sessions").key == adopted[0].key


def test_adoption_is_idempotent(tmp_path):
    rows = [_row("Cowork sessions")]

    assert adopt_rows("claude", rows, base_dir=tmp_path)
    assert adopt_rows("claude", rows, base_dir=tmp_path) == []


def test_a_row_the_catalog_already_knows_is_not_adopted(tmp_path):
    assert adopt_rows("claude", [_row("Opus only")], base_dir=tmp_path) == []
    assert not (tmp_path / "claude.json").exists()


def test_disabling_an_adopted_meter_stops_it_being_adopted_again(tmp_path):
    """Otherwise "off" means "back next week as Cowork sessions_2"."""
    rows = [_row("Cowork sessions")]
    key = adopt_rows("claude", rows, base_dir=tmp_path)[0].key
    path = tmp_path / "claude.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for meter in document["meters"]:
        if meter["key"] == key:
            meter["enabled"] = False
    path.write_text(json.dumps(document), encoding="utf-8")

    assert adopt_rows("claude", rows, base_dir=tmp_path) == []
    keys = [spec.key for spec in load_catalog("claude", base_dir=tmp_path).specs]
    assert keys.count(key) == 1
    assert f"{key}_2" not in keys


def test_disabling_a_bundled_meter_does_not_resurrect_it_under_a_new_key(tmp_path):
    _write_override(tmp_path, "claude", [{"key": "opus_only", "enabled": False}])

    assert adopt_rows("claude", [_row("Opus only")], base_dir=tmp_path) == []

    keys = [spec.key for spec in load_catalog("claude", base_dir=tmp_path).specs]
    assert "opus_only_2" not in keys


def test_a_parked_entry_is_not_adopted_again(tmp_path):
    """A review that rejects a discovered row must be able to make it stick."""
    _write_override(
        tmp_path,
        "claude",
        [
            {
                "key": "cloud_runs",
                "label": "Cloud runs",
                "aliases": ["Cloud runs"],
                "status": "pending",
            }
        ],
    )

    assert adopt_rows("claude", [_row("Cloud runs")], base_dir=tmp_path) == []


def test_a_page_row_named_after_a_display_label_is_not_adopted(tmp_path):
    """Claude renders "Session" and "Daily routine runs" as labels, not aliases.

    Adopting one gives two specs with the same ``label``, and ``label`` is the
    history key and what ratio.py selects Session/Weekly by.
    """
    rows = [_row("Session", percent=3.0, reset_text="400 hr"),
            _row("Daily routine runs")]

    assert adopt_rows("claude", rows, base_dir=tmp_path) == []


def test_no_two_meters_share_a_display_label_after_a_scan(tmp_path):
    rows = [
        _row("Session", percent=3.0, reset_text="400 hr"),
        _row("Weekly", percent=9.0),
        _row("Cowork sessions"),
    ]

    adopt_rows("claude", rows, base_dir=tmp_path)

    labels = [normalize_label(s.label) for s in load_catalog("claude", base_dir=tmp_path).specs]
    assert len(labels) == len(set(labels))


def test_a_row_sharing_a_meters_display_label_cannot_fork_its_history(tmp_path):
    """What the collision actually costs: `history` keys on provider::label.

    Two specs labelled "Session" with different reset times land on one key,
    and every refresh looks like a period rollover - `history.jsonl` fills up
    with fabricated periods. Two refreshes here produced two of them.
    """
    adopt_rows("claude", [_row("Session", percent=3.0, reset_text="400 hr")],
               base_dir=tmp_path)
    catalog = load_catalog("claude", base_dir=tmp_path)

    def _snapshot() -> UsageSnapshot:
        metrics = []
        for spec in catalog.enabled_specs:
            metric = metric_for_spec(
                spec,
                {"percent": 3.0, "kind": "used"},
                resets_at=datetime.now() + (spec.window or timedelta(hours=400)),
            )
            if metric is not None:
                metrics.append(metric)
        return UsageSnapshot(
            provider="claude", status=SnapshotStatus.OK, metrics=metrics
        )

    store = HistoryStore(base_dir=tmp_path)
    store.record_snapshot(_snapshot())

    assert store.record_snapshot(_snapshot()) == []
    assert not (tmp_path / "history.jsonl").exists()


def test_known_labels_covers_every_spec_whatever_its_state():
    catalog = bundled_catalog("claude")

    known = catalog.known_labels()

    assert "opus only" in known
    # Display labels too: "Session" is no alias of anything, and a page row
    # reading "Session" must not become a second meter with that history key.
    assert "session" in known and "weekly" in known


def test_adoption_preserves_hand_edits_already_in_the_override(tmp_path):
    _write_override(tmp_path, "claude", [{"key": "opus_only", "enabled": False}])

    adopt_rows("claude", [_row("Cowork sessions")], base_dir=tmp_path)

    catalog = load_catalog("claude", base_dir=tmp_path)
    assert catalog.spec_for_key("opus_only").enabled is False
    assert catalog.spec_for_label("Cowork sessions") is not None


def test_an_adopted_meter_can_be_switched_off_by_editing_the_override(tmp_path):
    key = adopt_rows("claude", [_row("Cowork sessions")], base_dir=tmp_path)[0].key
    path = tmp_path / "claude.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for meter in document["meters"]:
        if meter["key"] == key:
            meter["enabled"] = False
    path.write_text(json.dumps(document), encoding="utf-8")

    assert load_catalog("claude", base_dir=tmp_path).spec_for_label(
        "Cowork sessions"
    ) is None


def test_an_unreadable_override_is_preserved_before_it_is_replaced(tmp_path):
    """A trailing comma must not cost the user their hand edits.

    The write replaces the whole document, so an override file that cannot be
    parsed is moved aside rather than discarded — the same treatment a
    corrupt config gets.
    """
    path = tmp_path / "claude.json"
    path.write_text(
        '{"version": 1, "meters": [{"key": "mine", "enabled": false},]}',
        encoding="utf-8",
    )

    adopt_rows("claude", [_row("Cowork sessions")], base_dir=tmp_path)

    backup = tmp_path / "claude.json.corrupt"
    assert backup.exists(), "the unreadable file was silently overwritten"
    assert "mine" in backup.read_text(encoding="utf-8")
    assert load_catalog("claude", base_dir=tmp_path).spec_for_label("Cowork sessions")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_override_file_is_written_owner_only_and_atomically(tmp_path):
    """It carries page-derived labels and the account they were seen on."""
    adopt_rows("claude", [_row("Cowork sessions")], base_dir=tmp_path)

    path = tmp_path / "claude.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["claude.json"], "temp file left behind"


def test_adoption_stops_at_the_cap(tmp_path):
    rows = [_row(f"Meter number {chr(97 + i)}") for i in range(MAX_ADOPTED_METERS + 5)]

    adopt_rows("claude", rows, base_dir=tmp_path)

    adopted = [s for s in load_catalog("claude", base_dir=tmp_path).specs if s.adopted]
    assert len(adopted) == MAX_ADOPTED_METERS


@pytest.mark.parametrize(
    "label",
    [
        "",
        "x" * 60,                      # too long to be a meter name
        "Upgrade to Max",              # page furniture
        "Manage subscription",
        "Plan usage limits",
        "Personal usage",
        "Resets in 3 days",
        "Learn more about limits",
        "42",                          # not a name
        "https://claude.ai/usage",     # link text
        "Sign in to continue",
    ],
)
def test_junk_labels_are_never_adopted(label, tmp_path):
    assert is_adoptable_label(label, catalog=bundled_catalog("claude")) is False
    assert adopt_rows("claude", [_row(label)], base_dir=tmp_path) == []


@pytest.mark.parametrize(
    "label",
    [
        "Current",                            # a fragment of "Current session"
        "Opus",                               # of "Opus only"
        "Design",                             # of "Claude Design"
        "Daily included routine runs 3 of 10",  # that row with its count glued on
        "Session limit",                      # a relabel, which belongs in aliases
    ],
)
def test_a_label_overlapping_a_known_meter_is_not_adopted(label, tmp_path):
    """A fragment of a known label is that meter again under a second name.

    It reports the same number twice, and while the fragment was also being
    injected as a rival label it made the meter it came from unreadable.
    """
    assert is_adoptable_label(label, catalog=bundled_catalog("claude")) is False
    assert adopt_rows("claude", [_row(label)], base_dir=tmp_path) == []


def test_a_new_meter_that_merely_shares_a_word_is_still_adopted(tmp_path):
    """Whole words, not bare substrings: "Cowork sessions" contains "Session"."""
    assert adopt_rows("claude", [_row("Cowork sessions")], base_dir=tmp_path)


def test_a_percentage_with_no_reset_text_outside_the_container_is_not_adopted(tmp_path):
    row = _row("Cowork sessions", reset_text=None, in_container=False)

    assert adopt_rows("claude", [row], base_dir=tmp_path) == []


def test_a_row_without_a_percentage_is_not_adopted(tmp_path):
    assert adopt_rows("claude", [_row("Cowork sessions", percent=None)], base_dir=tmp_path) == []


def test_an_adopted_meter_is_given_no_window(tmp_path):
    """The same refusal to guess that keeps `polarity` unset.

    A window inferred from the wording is a guess, and a wrong one makes an
    active meter read "idle" instead of showing its number. The user can put
    the real period in the override file.
    """
    spec = adopt_rows("claude", [_row("Daily agent runs")], base_dir=tmp_path)[0]

    assert spec.window is None


@pytest.mark.parametrize("kind", ["unknown", "", None])
def test_a_row_with_no_used_or_remaining_wording_is_not_adopted(kind, tmp_path):
    """unreadable_reason would refuse it on every refresh anyway."""
    row = _row("Cowork sessions", kind=kind)

    assert adopt_rows("claude", [row], base_dir=tmp_path) == []


# --- discovery: the weekly gate --------------------------------------------


def test_a_kind_that_has_never_been_scanned_is_due():
    assert scan_due(Config(), "claude") is True


def test_a_recent_scan_is_not_due():
    config = Config()
    record_scan(config, "claude", save=False)

    assert scan_due(config, "claude") is False
    assert scan_due(config, "codex") is True, "the scan is per kind"


def test_the_scan_comes_due_again_after_the_interval():
    config = Config()
    now = datetime.now()
    record_scan(config, "claude", now=now - CATALOG_SCAN_INTERVAL, save=False)

    assert scan_due(config, "claude", now=now) is True


def test_a_timestamp_from_the_future_is_treated_as_due():
    """A clock change must not switch discovery off for a week."""
    config = Config()
    now = datetime.now()
    record_scan(config, "claude", now=now + timedelta(days=30), save=False)

    assert scan_due(config, "claude", now=now) is True


def test_a_provider_with_no_config_never_scans():
    assert scan_due(None, "claude") is False


def test_the_scan_time_survives_a_config_round_trip():
    config = Config()
    record_scan(config, "claude")

    assert scan_due(Config.load(), "claude") is False


def test_clear_scans_arms_every_kind():
    config = Config()
    record_scan(config, "claude", save=False)
    record_scan(config, "codex", save=False)

    clear_scans(config)

    assert scan_due(config, "claude") and scan_due(config, "codex")


def test_a_poisoned_scan_block_is_dropped_rather_than_fatal():
    config = Config.model_validate({"meter_catalog_last_scan": ["not", "a", "map"]})

    assert config.meter_catalog_last_scan == {}


# --- history key stability -------------------------------------------------


def test_a_page_relabel_does_not_fork_a_metrics_history(tmp_path):
    """The point of separating ``label`` from ``aliases``.

    History keys on ``provider::label``. If the display label followed the
    page's wording, Claude renaming "All models" to "Weekly" would start a
    second, empty history for the same meter.
    """
    store = HistoryStore(base_dir=tmp_path)
    catalog = bundled_catalog("claude")
    resets_at = datetime.now() + timedelta(days=3)

    for page_label in ("All models", "Weekly"):
        spec = catalog.spec_for_label(page_label)
        metric = metric_for_spec(
            spec, {"percent": 30.0, "kind": "used"}, resets_at=resets_at
        )
        store.record_snapshot(
            UsageSnapshot(
                provider="claude", status=SnapshotStatus.OK, metrics=[metric]
            )
        )

    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert list(current) == ["claude::Weekly"]


# --- extractor plumbing ----------------------------------------------------


def test_catalog_labels_reach_the_extractor_as_data_not_code():
    catalog = MeterCatalog(
        kind="claude",
        specs=(
            MeterSpec(
                key="k",
                label="K",
                aliases=("'); alert(1); //",),
                primary=False,
            ),
        ),
    )

    source = extractor_source("const L = __AG_ROW_LABELS__;", catalog, discover=False)

    # A JSON string literal, so the quote is escaped and the label stays a
    # label. Spliced in raw it would have closed the array and run.
    assert source == 'const L = ["\'); alert(1); //"];'


def _row_labels(catalog) -> list[str]:
    return json.loads(extractor_source("__AG_ROW_LABELS__", catalog, discover=False))


def test_the_rival_label_set_carries_bundled_meters_only(tmp_path):
    """ROW_LABELS decides attribution, not what gets read.

    A row whose container also names another meter has no attributable
    percentage, so putting an adopted label in here lets a discovered row
    make a *primary* meter unreadable.
    """
    adopt_rows("claude", [_row("Cowork sessions")], base_dir=tmp_path)

    labels = _row_labels(load_catalog("claude", base_dir=tmp_path))

    assert "Cowork sessions" not in labels
    assert labels == LEGACY_CLAUDE_ROW_LABELS


def test_a_disabled_bundled_meter_still_counts_as_a_rival(tmp_path):
    """Disabling stops us reading it; the page still renders its row."""
    _write_override(tmp_path, "claude", [{"key": "opus_only", "enabled": False}])

    assert "Opus only" in _row_labels(load_catalog("claude", base_dir=tmp_path))


def test_an_alias_that_looks_like_an_injection_marker_stays_data():
    """The markers are filled in one pass, so inserted text is not rescanned."""
    catalog = MeterCatalog(
        kind="claude",
        specs=(MeterSpec(key="k", label="K", aliases=("__AG_DISCOVER__",)),),
    )

    source = extractor_source(
        "const L = __AG_ROW_LABELS__; const D = __AG_DISCOVER__;",
        catalog,
        discover=True,
    )

    assert source == 'const L = ["__AG_DISCOVER__"]; const D = true;'


def test_discovery_is_off_unless_the_scan_is_due():
    catalog = bundled_catalog("claude")

    assert "false" in extractor_source("__AG_DISCOVER__", catalog, discover=False)
    assert "true" in extractor_source("__AG_DISCOVER__", catalog, discover=True)


# --- packaging -------------------------------------------------------------
#
# The catalog is data next to the code. Both packaging paths have to carry it
# explicitly, and a frozen build that silently lost it would read no meters at
# all - the failure would only show up on a user's machine.


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_the_catalog_files_are_inside_the_package(kind):
    path = REPO_ROOT / "src" / "aigauge" / "providers" / "meter_catalog" / f"{kind}.json"

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == kind


def test_the_wheel_config_names_the_catalog():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "src/aigauge/providers/meter_catalog/*.json" in text


@pytest.mark.parametrize(
    "script,spec",
    [
        # PyInstaller's --add-data separator is os.pathsep, so the two scripts
        # cannot share one string.
        ("build.sh", "src/aigauge/providers/meter_catalog:aigauge/providers/meter_catalog"),
        ("build.ps1", r"src\aigauge\providers\meter_catalog;aigauge\providers\meter_catalog"),
    ],
)
def test_the_pyinstaller_builds_bundle_the_catalog(script, spec):
    text = (REPO_ROOT / script).read_text(encoding="utf-8")

    assert "--add-data" in text, f"{script} bundles no data files"
    assert spec in text, f"{script} does not bundle the meter catalog"


# --- provider wiring -------------------------------------------------------
#
# The scan has to be armed by the provider, not by a rebuild: the app can run
# for weeks without reconstructing its providers. These drive the real
# refresh() with a stubbed ScrapeRunner, so the extractor source and the
# adoption call are the ones production uses.


class _FakeRunner:
    last: dict = {}

    def __init__(self, **kwargs):
        type(self).last = kwargs

    def run(self, on_done):  # pragma: no cover - never reached in these tests
        pass


@pytest.fixture()
def fake_runner(monkeypatch):
    from aigauge.providers import claude as claude_module
    from aigauge.providers import codex as codex_module

    monkeypatch.setattr(claude_module, "ScrapeRunner", _FakeRunner)
    monkeypatch.setattr(codex_module, "ScrapeRunner", _FakeRunner)
    return _FakeRunner


@pytest.mark.parametrize(
    "kind,provider_path",
    [
        ("claude", "aigauge.providers.claude.ClaudeProvider"),
        ("codex", "aigauge.providers.codex.CodexProvider"),
    ],
)
def test_a_due_scan_turns_discovery_on_in_the_injected_extractor(
    kind, provider_path, fake_runner
):
    import importlib

    module_name, class_name = provider_path.rsplit(".", 1)
    provider_cls = getattr(importlib.import_module(module_name), class_name)
    config = Config()

    provider_cls(config=config).refresh(lambda snapshot: None)
    due_source = fake_runner.last["extractor_js"]

    record_scan(config, kind, save=False)
    provider_cls(config=config).refresh(lambda snapshot: None)
    settled_source = fake_runner.last["extractor_js"]

    assert "const DISCOVER = true;" in due_source
    assert "const DISCOVER = false;" in settled_source


def test_a_provider_with_no_config_never_asks_for_a_scan(fake_runner):
    from aigauge.providers.claude import ClaudeProvider

    ClaudeProvider().refresh(lambda snapshot: None)

    assert "const DISCOVER = false;" in fake_runner.last["extractor_js"]


def test_a_completed_scan_adopts_new_rows_and_records_the_scan(fake_runner):
    from aigauge.providers.claude import ClaudeProvider

    config = Config()
    ClaudeProvider(config=config).refresh(lambda snapshot: None)
    payload = {
        "logged_out": False,
        "session": {"percent": 5, "kind": "used", "reset_text": "6 min"},
        "weekly_all": {"percent": 26, "kind": "used", "reset_text": "Thu 9:59 AM"},
        "title": "Claude",
        "url": "https://claude.ai/settings/usage",
        "body_text": "Plan usage Current session 5% used Weekly 26% used",
        "discovered": [
            {"label": "Cowork sessions", "percent": 7.0, "kind": "used",
             "reset_text": "3 days", "in_container": True},
            {"label": "Upgrade to Max", "percent": 20.0, "kind": "unknown",
             "reset_text": None, "in_container": True},
        ],
    }

    snapshot = fake_runner.last["build"](payload)

    assert snapshot.status == SnapshotStatus.OK
    catalog = load_catalog("claude")
    assert catalog.spec_for_label("Cowork sessions") is not None
    assert catalog.spec_for_label("Upgrade to Max") is None
    assert scan_due(config, "claude") is False


_OK_PAYLOAD = {
    "logged_out": False,
    "session": {"percent": 5, "kind": "used", "reset_text": "6 min"},
    "weekly_all": {"percent": 26, "kind": "used", "reset_text": "Thu 9:59 AM"},
    "title": "Claude",
    "url": "https://claude.ai/settings/usage",
    "body_text": "Plan usage Current session 5% used Weekly 26% used",
}


def _payload(**extra) -> dict:
    return {**_OK_PAYLOAD, **extra}


def test_a_page_we_could_not_read_neither_adopts_nor_burns_the_scan(fake_runner):
    """A logged-out or challenged page still renders rows.

    Adopting from one writes page furniture into the catalog for good, and
    stamping the scan means the real page is not looked at for another week -
    including when the user pressed "Re-scan meters now".
    """
    from aigauge.providers.claude import ClaudeProvider

    config = Config()
    ClaudeProvider(config=config).refresh(lambda snapshot: None)

    snapshot = fake_runner.last["build"](
        {
            "logged_out": True,
            "title": "Claude",
            "url": "https://claude.ai/login",
            "body_text": "Log in to Claude",
            "discovered": [_row("Cowork sessions")],
        }
    )

    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert load_catalog("claude").spec_for_label("Cowork sessions") is None
    assert scan_due(config, "claude") is True


def test_codex_also_refuses_to_scan_a_page_it_could_not_read(fake_runner):
    """The same guard, in the other provider's copy of it."""
    from aigauge.providers.codex import CodexProvider

    config = Config()
    CodexProvider(config=config).refresh(lambda snapshot: None)

    snapshot = fake_runner.last["build"](
        {
            "logged_out": True,
            "title": "Log in",
            "url": "https://chatgpt.com/auth/login",
            "body_text": "Log in to continue",
            "discovered": [_row("Cloud tasks limit")],
        }
    )

    assert snapshot.status == SnapshotStatus.AUTH_REQUIRED
    assert load_catalog("codex").spec_for_label("Cloud tasks limit") is None
    assert scan_due(config, "codex") is True


def test_a_build_retry_stamps_the_scan_exactly_once(fake_runner, monkeypatch):
    """ScrapeRunner rebuilds after a transient error; the scan is one event."""
    from aigauge.providers.claude import ClaudeProvider

    saves: list[int] = []
    monkeypatch.setattr(Config, "save", lambda self: saves.append(1))
    config = Config()
    ClaudeProvider(config=config).refresh(lambda snapshot: None)
    discovered = [_row("Cowork sessions")]

    # The first attempt lands on a half-rendered page: rows, but nothing the
    # snapshot can be built from.
    errored = fake_runner.last["build"](
        {
            "logged_out": False,
            "title": "Claude",
            "url": "https://claude.ai/settings/usage",
            "body_text": "Plan usage 50%",
            "discovered": discovered,
        }
    )
    assert errored.status == SnapshotStatus.ERROR
    assert saves == []

    fake_runner.last["build"](_payload(discovered=discovered))
    fake_runner.last["build"](_payload(discovered=discovered))

    assert saves == [1]


def test_a_scan_that_found_no_usage_container_is_not_a_completed_scan(
    fake_runner, caplog
):
    """"Found nothing" and "could not look" are different answers."""
    from aigauge.providers.claude import ClaudeProvider

    config = Config()
    ClaudeProvider(config=config).refresh(lambda snapshot: None)

    with caplog.at_level(logging.INFO, logger="aigauge.providers.claude"):
        snapshot = fake_runner.last["build"](_payload(discovered=None))

    assert snapshot.status == SnapshotStatus.OK
    assert scan_due(config, "claude") is True
    assert "classification=discovery_no_container" in caplog.text


def test_a_scan_that_found_nothing_new_still_counts(fake_runner):
    from aigauge.providers.claude import ClaudeProvider

    config = Config()
    ClaudeProvider(config=config).refresh(lambda snapshot: None)

    fake_runner.last["build"](_payload(discovered=[]))

    assert scan_due(config, "claude") is False


def test_a_scrape_that_never_ran_discovery_does_not_record_a_scan(fake_runner):
    from aigauge.providers.claude import ClaudeProvider

    config = Config()
    ClaudeProvider(config=config).refresh(lambda snapshot: None)
    # No "discovered" key: the extractor gave up before the scan completed.
    fake_runner.last["build"](
        {
            "logged_out": False,
            "session": {"percent": 5, "kind": "used", "reset_text": "6 min"},
            "weekly_all": {"percent": 26, "kind": "used", "reset_text": "Thu 9:59"},
            "title": "Claude",
            "url": "https://claude.ai/settings/usage",
            "body_text": "Plan usage Current session 5% used Weekly 26% used",
        }
    )

    assert scan_due(config, "claude") is True


# --- provenance ------------------------------------------------------------
#
# A discovered meter is structurally identical to a bundled one, so without
# provenance nobody can tell which meters the app invented for itself, when,
# from which account, or on what evidence. A review step is going to need
# exactly that, and it has to be recorded at adoption time - the page has
# moved on by the time anyone looks.


def _adopted(tmp_path, row=None, **kwargs):
    rows = [row or _row("Cowork sessions")]
    return adopt_rows("claude", rows, base_dir=tmp_path, **kwargs)[0]


def test_an_adopted_meter_records_where_it_came_from(tmp_path):
    now = datetime(2026, 9, 9, 14, 30, 5)

    spec = _adopted(tmp_path, account_id="claude-work", now=now)

    assert spec.source == SOURCE_DISCOVERY
    assert spec.first_seen == "2026-09-09T14:30:05"
    assert spec.account_id == "claude-work"
    assert spec.evidence == "Cowork sessions 7% used resets 3 days"
    assert spec.status == STATUS_ACTIVE


def test_provenance_survives_the_round_trip_through_the_override_file(tmp_path):
    now = datetime(2026, 9, 9, 14, 30, 5)
    _adopted(tmp_path, account_id="claude-work", now=now)

    reloaded = load_catalog("claude", base_dir=tmp_path).spec_for_label(
        "Cowork sessions"
    )

    assert (reloaded.source, reloaded.account_id, reloaded.first_seen) == (
        SOURCE_DISCOVERY,
        "claude-work",
        "2026-09-09T14:30:05",
    )
    assert reloaded.evidence == "Cowork sessions 7% used resets 3 days"


def test_every_bundled_meter_declares_itself_bundled():
    for kind in ("claude", "codex"):
        for spec in bundled_catalog(kind).specs:
            assert spec.source == "bundled", f"{kind}:{spec.key}"
            assert spec.adopted is False
            assert spec.first_seen is None and spec.evidence is None


def test_an_adopted_meter_is_otherwise_shaped_like_a_bundled_one(tmp_path):
    _adopted(tmp_path)
    entry = json.loads((tmp_path / "claude.json").read_text(encoding="utf-8"))["meters"][0]
    bundled = json.loads(
        (REPO_ROOT / "src/aigauge/providers/meter_catalog/claude.json").read_text(
            encoding="utf-8"
        )
    )["meters"][0]

    assert set(bundled) <= set(entry), "an adopted entry is missing a structural field"
    assert entry["primary"] is False


def test_evidence_is_composed_from_the_row_not_copied_out_of_it(tmp_path):
    """The raw row text can carry an account email; this file is one users open."""
    row = _row(
        "Cowork sessions",
        raw="Signed in as someone@example.com Cowork sessions 7% used",
        reset_text="3 days",
    )

    spec = _adopted(tmp_path, row=row)

    assert "@example.com" not in (spec.evidence or "")
    assert "someone" not in (spec.evidence or "")


def test_an_email_in_the_label_itself_is_redacted(tmp_path):
    assert "@" not in (row_evidence(_row("Plan for someone@example.com")) or "")
    assert "[redacted-email]" in row_evidence(_row("Plan for someone@example.com"))


def test_evidence_is_capped(tmp_path):
    evidence = row_evidence(_row("A" * 400, reset_text="B" * 400))

    assert len(evidence) <= MAX_EVIDENCE_CHARS


def test_an_unknown_field_on_an_entry_is_preserved_not_eaten(tmp_path):
    """A later release will add review state; an older loader must not drop it."""
    _write_override(
        tmp_path,
        "claude",
        [
            {
                "key": "cloud_runs",
                "label": "Cloud runs",
                "aliases": ["Cloud runs"],
                "reviewed_by": "someone",
                "review_note": {"decision": "keep"},
            }
        ],
    )

    spec = load_catalog("claude", base_dir=tmp_path).spec_for_key("cloud_runs")

    assert spec.extra == {"reviewed_by": "someone", "review_note": {"decision": "keep"}}
    assert _spec_to_raw(spec)["reviewed_by"] == "someone"


def test_an_entry_awaiting_review_is_loaded_but_not_read(tmp_path):
    """The hook the review step needs: `status` gates without deleting.

    This is about *reading* only. Whether a parked entry can be adopted again
    is a different property, pinned by the adoption tests above - gating the
    read while leaving the label adoptable is how "parked" became "comes back
    next week under a new key".
    """
    _write_override(
        tmp_path,
        "claude",
        [
            {
                "key": "cloud_runs",
                "label": "Cloud runs",
                "aliases": ["Cloud runs"],
                "status": "pending",
            }
        ],
    )

    catalog = load_catalog("claude", base_dir=tmp_path)

    assert catalog.spec_for_key("cloud_runs").status == "pending"
    assert catalog.spec_for_label("Cloud runs") is None
    assert "Cloud runs" not in catalog.aliases()


def test_the_provider_records_the_account_a_meter_was_discovered_on(fake_runner):
    from aigauge.providers.claude import ClaudeProvider

    ClaudeProvider(account_id="claude-work", config=Config()).refresh(lambda s: None)
    fake_runner.last["build"](
        {
            "logged_out": False,
            "session": {"percent": 5, "kind": "used", "reset_text": "6 min"},
            "weekly_all": {"percent": 26, "kind": "used", "reset_text": "Thu 9:59 AM"},
            "title": "Claude",
            "url": "https://claude.ai/settings/usage",
            "body_text": "Plan usage Current session 5% used Weekly 26% used",
            "discovered": [
                {"label": "Cowork sessions", "percent": 7.0, "kind": "used",
                 "reset_text": "3 days", "in_container": True},
            ],
        }
    )

    spec = load_catalog("claude").spec_for_label("Cowork sessions")
    assert spec.account_id == "claude-work"
    assert spec.evidence == "Cowork sessions 7% used resets 3 days"


@pytest.mark.parametrize(
    "label",
    [
        # Codex's own shared-limit wording. The blocklist has to reject page
        # furniture without rejecting a limit named after where it applies.
        "Workspace monthly credit limit",
        "Cowork sessions",
        "Opus 4 only",
        "Daily included routine runs",
        "5 hour usage limit",
    ],
)
def test_a_plausible_meter_name_is_not_mistaken_for_page_furniture(label):
    assert is_adoptable_label(label) is True
