from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class SnapshotStatus(str, Enum):
    OK = "ok"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


# How long a **page-derived** ``note`` may be by the time it reaches the
# model. A note is only ever shown in a tooltip, and a tooltip is clipped to
# the same 280 characters (``widget._TOOLTIP_ERROR_CHARS``) before it is
# escaped - so nothing reaches the user that this drops, and a 200 kB
# "Resets ..." string lifted off a usage page stops travelling through the app
# to be re-clipped by each of the six tooltips that carry it.
MAX_NOTE_CHARS = 280


def bounded_note(text: str | None) -> str | None:
    """Clip a note a provider read off a page or an API to ``MAX_NOTE_CHARS``.

    The bound lives here, with the constant and beside the field it bounds,
    rather than being re-decided at each place a note is built: it was
    enforced at ``catalog.metric_for_spec`` alone, and ``opencode_go`` builds
    its own ``UsageMetric`` from one element's text and from a regex group
    over a whole page body, so 200 010 characters of either reached the model
    and were carried for the life of the tile.

    Deliberately **not** a check in ``UsageMetric.__post_init__``: most notes
    are composed by this app - Azure alone writes several sentences of period,
    lag and gross-of-credits explanation into one - and a blanket 280 would
    truncate our own prose to bound a provider's. What needs bounding is text
    that came from outside, which is exactly what the callers of this know.
    """
    if isinstance(text, str) and len(text) > MAX_NOTE_CHARS:
        return text[:MAX_NOTE_CHARS]
    return text


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
