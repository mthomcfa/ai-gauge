"""Which labelled rows on a provider's usage page are meters.

Claude and Codex reshuffle their usage surfaces often — Claude changed it three
times in one week — and every reshuffle used to mean editing a hardcoded label
list in the extractor. The definitions live in data instead: a JSON file per
provider *kind* bundled with the package, overlaid by an editable file in the
app-data dir that the weekly self-scan also writes into.

Two properties the rest of the app depends on:

* ``label`` is the history key (``history._state_key``). It is deliberately
  independent of the page's wording, so a page relabel does not fork a metric's
  history. ``aliases`` carry the page wording.
* ``primary`` decides whether a metric drives the tray/menu-bar colour. Only
  primary meters are emitted untagged; everything else carries
  ``BREAKDOWN_TAG`` and is informational (see ``gauge.provider_max_percent``).

Discovery is entirely local: it reads rows the page already rendered into the
embedded browser. Nothing is fetched, and no catalog is ever downloaded.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import app_data_dir
from ..models import UsageMetric
from ._common import normalize_percent
from .idle import idle_reset_state

log = logging.getLogger("aigauge.providers.catalog")

# Tag carried by every non-primary meter. Tagged metrics are informational:
# the widget hides them until a tile is expanded and gauge.provider_max_percent
# leaves them out of the tray colour.
BREAKDOWN_TAG = "meter_breakdown"

CATALOG_DIR_NAME = "meter_catalog"
CATALOG_SCAN_INTERVAL = timedelta(days=7)

# Adoption guards. A discovered row has to look like a meter label - short,
# alphabetic, with a percentage beside it - or the override file fills up with
# page furniture and every refresh grows another junk gauge.
MAX_LABEL_CHARS = 40
MAX_ADOPTED_METERS = 24

_KEY_RE = re.compile(r"[a-z0-9_]{1,64}")
_KIND_RE = re.compile(r"[a-z0-9_]{1,32}")
# Ordinary label punctuation only: anything with a colon, percent sign, URL or
# markup is page text, not a meter name. A leading digit is allowed because
# Codex's own five-hour card starts with one ("5 hour usage limit"); the
# letter/digit counts below are what keep a stray number out.
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'&()/+-]*")

# Wording that appears next to a percentage but never names a meter. Matched as
# a substring of the normalized label, so "Manage plan" and "plan usage limits"
# are both rejected by "plan usage"/"manage".
_NON_METER_MARKERS = (
    "plan usage",
    "usage limits",
    "usage breakdown",
    "personal usage",
    "team usage",
    "credits remaining",
    "current plan",
    "your plan",
    "upgrade",
    "manage",
    "settings",
    "learn more",
    "view all",
    "see details",
    "sign in",
    "log in",
    "logout",
    "billing",
    "invoice",
    "payment",
    "member",
    "seat",
    "support",
    "help",
    "privacy",
    "terms",
    "cookie",
    "loading",
    "error",
    "feedback",
    "account",
    "profile",
    "notification",
    "workspace",
    "resets",
    "reset in",
)

# Window inference for adopted meters. Only the wordings the existing meters
# already use; anything else gets no window, which is safe (window only gates
# the idle-countdown display).
_WINDOW_HINTS: tuple[tuple[re.Pattern[str], timedelta], ...] = (
    (re.compile(r"\bdaily\b|\bper day\b|\b24[- ]hour\b", re.IGNORECASE), timedelta(days=1)),
    (re.compile(r"\bweekly\b|\b7[- ]day\b", re.IGNORECASE), timedelta(days=7)),
    (re.compile(r"\b5[- ]hour\b|\bsession\b", re.IGNORECASE), timedelta(hours=5)),
)


def normalize_label(text: Any) -> str:
    """Case-folded, whitespace-collapsed form used for every alias comparison."""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def clean_label(text: Any) -> str:
    """Trim a page label down to the words, keeping its original casing."""
    label = re.sub(r"\s+", " ", str(text or "")).strip()
    return label.strip(" \t·•:,-–—")


@dataclass(frozen=True)
class MeterSpec:
    """One meter the app knows how to read.

    ``key`` addresses the row in the extractor payload, ``label`` is what the
    user (and ``history``) sees, ``aliases`` are the labels the page may render.
    """

    key: str
    label: str
    aliases: tuple[str, ...]
    window: timedelta | None = None
    primary: bool = False
    # Codex reads a card out of the page's plain text when the DOM walk fails;
    # these labels bound that text window. Empty means "every other alias".
    boundaries: tuple[str, ...] = ()
    # Only consulted when the page shows no used/remaining wording beside the
    # percentage. Unset everywhere by default: guessing polarity is how a quota
    # monitor reports 42% left as 42% consumed.
    polarity: str | None = None
    adopted: bool = False
    enabled: bool = True

    def matches(self, label: Any) -> bool:
        normalized = normalize_label(label)
        return any(normalize_label(alias) == normalized for alias in self.aliases)

    def to_js(self, *, all_aliases: Sequence[str] = ()) -> dict[str, Any]:
        boundaries = list(self.boundaries)
        if not boundaries:
            boundaries = [a for a in all_aliases if not self.matches(a)]
        return {
            "key": self.key,
            "label": self.label,
            "aliases": list(self.aliases),
            "boundaries": boundaries,
            "primary": bool(self.primary),
        }


@dataclass(frozen=True)
class MeterCatalog:
    kind: str
    specs: tuple[MeterSpec, ...] = ()

    @property
    def enabled_specs(self) -> tuple[MeterSpec, ...]:
        return tuple(spec for spec in self.specs if spec.enabled)

    def aliases(self) -> tuple[str, ...]:
        """Every enabled alias, de-duplicated, in catalog order."""
        seen: set[str] = set()
        out: list[str] = []
        for spec in self.enabled_specs:
            for alias in spec.aliases:
                normalized = normalize_label(alias)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    out.append(alias)
        return tuple(out)

    def spec_for_key(self, key: str) -> MeterSpec | None:
        for spec in self.specs:
            if spec.key == key:
                return spec
        return None

    def spec_for_label(self, label: Any) -> MeterSpec | None:
        for spec in self.enabled_specs:
            if spec.matches(label):
                return spec
        return None

    def to_js(self) -> list[dict[str, Any]]:
        all_aliases = self.aliases()
        return [spec.to_js(all_aliases=all_aliases) for spec in self.enabled_specs]


# --- loading ---------------------------------------------------------------


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        text = clean_label(item)
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _coerce_window(value: Any) -> timedelta | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0 or value > 366 * 24 * 3600:
        return None
    return timedelta(seconds=float(value))


def _coerce_polarity(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in ("used", "remaining") else None


def _spec_from_raw(raw: Any) -> MeterSpec | None:
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or "").strip().lower()
    if _KEY_RE.fullmatch(key) is None:
        log.warning("meter catalog: dropping entry with unusable key %r", raw.get("key"))
        return None
    label = clean_label(raw.get("label"))
    aliases = _coerce_str_tuple(raw.get("aliases"))
    if not label or not aliases:
        log.warning("meter catalog: dropping entry %s without a label/aliases", key)
        return None
    return MeterSpec(
        key=key,
        label=label,
        aliases=aliases,
        window=_coerce_window(raw.get("window_seconds")),
        primary=bool(raw.get("primary")),
        boundaries=_coerce_str_tuple(raw.get("boundaries")),
        polarity=_coerce_polarity(raw.get("polarity")),
        adopted=bool(raw.get("adopted")),
        enabled=raw.get("enabled") is not False,
    )


def _meters_from_document(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    meters = data.get("meters")
    if not isinstance(meters, list):
        return []
    return [item for item in meters if isinstance(item, dict)]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("meter catalog: cannot read %s", path)
        return None


def bundled_path(kind: str) -> Path:
    """Filesystem path of the packaged catalog for ``kind``.

    ``importlib.resources`` first so a zipped or frozen install resolves the
    same way the wheel does; ``__file__`` is the fallback for loaders that hand
    back no traversable resource.
    """
    name = f"{kind}.json"
    try:
        return Path(str(resources.files(__package__).joinpath(CATALOG_DIR_NAME, name)))
    except (ModuleNotFoundError, TypeError, ValueError, OSError):
        return Path(__file__).resolve().parent / CATALOG_DIR_NAME / name


def override_path(kind: str, *, base_dir: Path | None = None) -> Path:
    root = Path(base_dir) if base_dir is not None else app_data_dir() / CATALOG_DIR_NAME
    return root / f"{kind}.json"


def bundled_catalog(kind: str) -> MeterCatalog:
    if _KIND_RE.fullmatch(kind) is None:
        raise ValueError(f"unsafe meter catalog kind: {kind!r}")
    specs: list[MeterSpec] = []
    for raw in _meters_from_document(_read_json(bundled_path(kind))):
        spec = _spec_from_raw(raw)
        if spec is not None and not any(s.key == spec.key for s in specs):
            specs.append(spec)
    if not specs:
        # A packaging mistake, not a page change. Loud, because every meter for
        # this provider is now unreadable.
        log.error("meter catalog: bundled definitions for %s are missing", kind)
    return MeterCatalog(kind=kind, specs=tuple(specs))


def _apply_override(spec: MeterSpec, raw: dict[str, Any]) -> MeterSpec:
    """Overlay only the fields the override file actually names."""
    changes: dict[str, Any] = {}
    if "label" in raw:
        label = clean_label(raw.get("label"))
        if label:
            changes["label"] = label
    if "aliases" in raw:
        aliases = _coerce_str_tuple(raw.get("aliases"))
        if aliases:
            changes["aliases"] = aliases
    if "boundaries" in raw:
        changes["boundaries"] = _coerce_str_tuple(raw.get("boundaries"))
    if "window_seconds" in raw:
        changes["window"] = _coerce_window(raw.get("window_seconds"))
    if "primary" in raw:
        changes["primary"] = bool(raw.get("primary"))
    if "polarity" in raw:
        changes["polarity"] = _coerce_polarity(raw.get("polarity"))
    if "enabled" in raw:
        changes["enabled"] = raw.get("enabled") is not False
    return replace(spec, **changes) if changes else spec


def load_catalog(kind: str, *, base_dir: Path | None = None) -> MeterCatalog:
    """Bundled definitions with the app-data override file merged over them.

    Override entries are matched by ``key``: a known key updates only the
    fields it names (so ``{"key": "opus_only", "enabled": false}`` just turns
    that meter off), an unknown key is appended.
    """
    catalog = bundled_catalog(kind)
    path = override_path(kind, base_dir=base_dir)
    if not path.exists():
        return catalog
    specs = list(catalog.specs)
    by_key = {spec.key: index for index, spec in enumerate(specs)}
    for raw in _meters_from_document(_read_json(path)):
        key = str(raw.get("key") or "").strip().lower()
        if key in by_key:
            specs[by_key[key]] = _apply_override(specs[by_key[key]], raw)
            continue
        spec = _spec_from_raw(raw)
        if spec is None:
            continue
        by_key[spec.key] = len(specs)
        specs.append(spec)
    return MeterCatalog(kind=kind, specs=tuple(specs))


# --- reading a row into a metric -------------------------------------------


def unreadable_reason(card: Any, spec: MeterSpec | None = None) -> str | None:
    """Why this row cannot be turned into a gauge, or None if it can.

    Both cases produce a plausible wrong number rather than an error if left
    unchecked, which for a quota monitor is the worst available outcome:

    * ``ambiguous`` - the extractor found the label and several percentages in
      one container and could not say which belonged to this meter.
    * unknown ``kind`` - no "used"/"remaining" wording sat against the
      percentage, and ``normalize_percent`` resolves an unknown kind to
      *used*. A row meaning "42% left" would be shown as 42% consumed. A
      catalog entry may carry an explicit ``polarity`` for a page that renders
      a bare percentage; nothing ships with one.

    A row carrying no percentage at all is not unreadable; it is simply
    absent, and the idle/empty-panel paths handle that.
    """
    if not isinstance(card, dict):
        return None
    if card.get("ambiguous"):
        return "several meters share one container"
    if card.get("percent") is None:
        return None
    if card.get("kind") not in ("used", "remaining"):
        if spec is not None and spec.polarity:
            return None
        return "no used/remaining wording beside the percentage"
    return None


def metric_for_spec(
    spec: MeterSpec,
    card: dict[str, Any],
    *,
    resets_at: datetime | None = None,
) -> UsageMetric | None:
    """Build the metric for one catalog meter from its extracted row.

    Non-primary meters carry ``BREAKDOWN_TAG``: they are shown when a tile is
    expanded but never drive the tray colour, exactly like OpenRouter's
    per-model rows.
    """
    kind = str(card.get("kind") or "")
    if kind not in ("used", "remaining") and spec.polarity:
        kind = spec.polarity
    percent = normalize_percent(card.get("percent"), kind)
    if percent is None:
        return None
    resets_at, reset_label, idle_note = idle_reset_state(
        percent=percent,
        resets_at=resets_at,
        # A meter with no known period cannot have an "unused window" - pass a
        # window nothing can exceed so only the no-countdown case reads idle.
        window=spec.window if spec.window is not None else timedelta.max,
    )
    return UsageMetric(
        label=spec.label,
        percent_used=percent,
        resets_at=resets_at,
        reset_label=reset_label,
        note=idle_note or card.get("reset_text"),
        window=spec.window,
        tag=None if spec.primary else BREAKDOWN_TAG,
    )


# --- discovery / adoption --------------------------------------------------


def is_adoptable_label(label: Any, *, catalog: MeterCatalog | None = None) -> bool:
    """Whether a discovered row's label may become a new meter.

    Rejects page furniture: anything long, non-alphabetic, or carrying wording
    that never names a meter. A label the catalog already knows is not
    adoptable either — that is a match, not a discovery.
    """
    text = clean_label(label)
    if not text or len(text) > MAX_LABEL_CHARS:
        return False
    if _LABEL_RE.fullmatch(text) is None:
        return False
    letters = sum(1 for ch in text if ch.isalpha())
    digits = sum(1 for ch in text if ch.isdigit())
    if letters < 3 or digits > 4:
        return False
    normalized = normalize_label(text)
    if any(marker in normalized for marker in _NON_METER_MARKERS):
        return False
    if catalog is not None and catalog.spec_for_label(text) is not None:
        return False
    return True


def _adoptable_row(row: Any, *, catalog: MeterCatalog) -> str | None:
    """Return the label to adopt from a discovered row, or None."""
    if not isinstance(row, dict):
        return None
    percent = row.get("percent")
    if not isinstance(percent, (int, float)) or isinstance(percent, bool):
        return None
    # Either the row carries reset wording or the extractor found it inside the
    # recognized usage container. A bare percentage somewhere else on the page
    # is not a meter.
    if not row.get("reset_text") and not row.get("in_container"):
        return None
    label = clean_label(row.get("label"))
    if not is_adoptable_label(label, catalog=catalog):
        return None
    return label


def _adopted_key(label: str, taken: Iterable[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", normalize_label(label)).strip("_")[:48]
    base = base or "meter"
    taken = set(taken)
    key = base
    suffix = 2
    while key in taken:
        key = f"{base}_{suffix}"
        suffix += 1
    return key


def infer_window(label: str) -> timedelta | None:
    for pattern, window in _WINDOW_HINTS:
        if pattern.search(label):
            return window
    return None


def _spec_to_raw(spec: MeterSpec) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "key": spec.key,
        "label": spec.label,
        "aliases": list(spec.aliases),
        "window_seconds": (
            int(spec.window.total_seconds()) if spec.window is not None else None
        ),
        "primary": spec.primary,
        "enabled": spec.enabled,
    }
    if spec.boundaries:
        raw["boundaries"] = list(spec.boundaries)
    if spec.polarity:
        raw["polarity"] = spec.polarity
    if spec.adopted:
        raw["adopted"] = True
    return raw


def adopt_rows(
    kind: str,
    rows: Any,
    *,
    base_dir: Path | None = None,
) -> list[MeterSpec]:
    """Add unrecognized discovered rows to the override file.

    Adopted meters are always informational (``primary`` false): a page row the
    app has never seen must not be able to take over the tray colour. Returns
    the specs written, which is empty in the normal case where the page shows
    nothing new.
    """
    if not isinstance(rows, list) or not rows:
        return []
    catalog = load_catalog(kind, base_dir=base_dir)
    existing_adopted = sum(1 for spec in catalog.specs if spec.adopted)
    taken_keys = {spec.key for spec in catalog.specs}
    seen_labels = {normalize_label(alias) for alias in catalog.aliases()}

    adopted: list[MeterSpec] = []
    for row in rows:
        if existing_adopted + len(adopted) >= MAX_ADOPTED_METERS:
            log.warning(
                "meter catalog: %s already has %d adopted meters; ignoring the rest",
                kind,
                MAX_ADOPTED_METERS,
            )
            break
        label = _adoptable_row(row, catalog=catalog)
        if label is None or normalize_label(label) in seen_labels:
            continue
        seen_labels.add(normalize_label(label))
        key = _adopted_key(label, taken_keys)
        taken_keys.add(key)
        adopted.append(
            MeterSpec(
                key=key,
                label=label,
                aliases=(label,),
                window=infer_window(label),
                primary=False,
                adopted=True,
            )
        )

    if not adopted:
        return []
    if not _write_override(kind, adopted, base_dir=base_dir):
        return []
    for spec in adopted:
        log.info(
            "meter catalog: adopted %s meter key=%s label=%r window=%s",
            kind,
            spec.key,
            spec.label,
            spec.window,
        )
    return adopted


def _write_override(
    kind: str,
    new_specs: Sequence[MeterSpec],
    *,
    base_dir: Path | None = None,
) -> bool:
    """Append specs to the override file, preserving hand edits already in it."""
    path = override_path(kind, base_dir=base_dir)
    document: dict[str, Any] = {"version": 1, "kind": kind, "meters": []}
    if path.exists():
        existing = _read_json(path)
        if isinstance(existing, dict):
            document = existing
        meters = document.get("meters")
        if not isinstance(meters, list):
            meters = []
        document["meters"] = [item for item in meters if isinstance(item, dict)]
    document.setdefault("version", 1)
    document.setdefault("kind", kind)
    document["meters"].extend(_spec_to_raw(spec) for spec in new_specs)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    except OSError:
        log.exception("meter catalog: cannot write %s", path)
        return False
    return True


# --- scan schedule ---------------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def scan_due(config: Any, kind: str, *, now: datetime | None = None) -> bool:
    """Whether this refresh should ask the page for *all* of its rows.

    Never scanned (a fresh install, or the user asked for a re-scan) counts as
    due. So does a timestamp in the future, which is a clock change rather than
    a reason to stop scanning for a week.
    """
    if config is None:
        return False
    stamps = getattr(config, "meter_catalog_last_scan", None)
    last = _parse_iso(stamps.get(kind)) if isinstance(stamps, dict) else None
    if last is None:
        return True
    now = now or datetime.now()
    return last > now or now - last >= CATALOG_SCAN_INTERVAL


def record_scan(
    config: Any,
    kind: str,
    *,
    now: datetime | None = None,
    save: bool = True,
) -> None:
    if config is None:
        return
    stamps = getattr(config, "meter_catalog_last_scan", None)
    if not isinstance(stamps, dict):
        stamps = {}
    stamps[kind] = (now or datetime.now()).replace(microsecond=0).isoformat()
    config.meter_catalog_last_scan = stamps
    if not save:
        return
    try:
        config.save()
    except Exception:  # noqa: BLE001 - a scan stamp must never break a refresh
        log.exception("meter catalog: cannot persist the %s scan time", kind)


def clear_scans(config: Any) -> None:
    """Arm a re-scan on the next refresh of every provider kind."""
    if config is None:
        return
    config.meter_catalog_last_scan = {}


# --- extractor plumbing ----------------------------------------------------


def extractor_source(template: str, catalog: MeterCatalog, *, discover: bool) -> str:
    """Fill a provider's extractor template with the catalog it should read.

    The labels are data, so they travel as JSON literals rather than being
    spliced into the source: ``json.dumps`` escapes quotes and every non-ASCII
    character, so a catalog entry cannot become JavaScript.
    """
    return (
        template.replace("__AG_ROW_LABELS__", json.dumps(list(catalog.aliases())))
        .replace("__AG_CATALOG__", json.dumps(catalog.to_js()))
        .replace("__AG_DISCOVER__", "true" if discover else "false")
    )
