from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class SnapshotStatus(str, Enum):
    OK = "ok"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


# How long a page-derived ``note`` may be by the time it reaches the model.
# A note is only ever shown in a tooltip, and a tooltip is clipped to the same
# 280 characters (``widget._TOOLTIP_ERROR_CHARS``) before it is escaped - so
# nothing reaches the user that this drops, and a 200 kB "Resets ..." string
# lifted off a usage page stops travelling through the app to be re-clipped by
# each of the six tooltips that carry it.
MAX_NOTE_CHARS = 280


@dataclass
class UsageMetric:
    """A single percent-used reading with a reset time."""

    label: str
    percent_used: float | None = None
    resets_at: datetime | None = None
    reset_label: str | None = None
    note: str | None = None
    window: timedelta | None = None
    tag: str | None = None


@dataclass
class UsageSnapshot:
    """Result returned by Provider.refresh() — one per provider per refresh cycle."""

    provider: str
    status: SnapshotStatus
    metrics: list[UsageMetric] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    error: str | None = None
    # Why this snapshot failed, in one word, for the scheduler only.
    # ``None`` means an ordinary failure: retry it. A named class marks a
    # failure the scheduler must not treat as one - a provider deliberately
    # waiting on its own throttle, or a timeout measured across a machine
    # suspend. Never shown to the user; the message in ``error`` is.
    error_class: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
