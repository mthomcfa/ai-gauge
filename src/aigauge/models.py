from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class SnapshotStatus(str, Enum):
    OK = "ok"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


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
