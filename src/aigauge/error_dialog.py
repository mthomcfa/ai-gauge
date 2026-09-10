from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from . import __version__
from .logging_setup import log_path
from .models import UsageSnapshot

_DARK_STYLESHEET = """
QDialog { background:#1f2937; color:#e5e7eb; }
QLabel { color:#e5e7eb; background:transparent; }
QPlainTextEdit {
    background:#111827; color:#f3f4f6;
    border:1px solid #374151; border-radius:4px;
    padding:6px;
    font-family: Consolas, 'Courier New', monospace;
    font-size: 11px;
}
QPushButton {
    background:#374151; color:#f3f4f6;
    border:1px solid #4b5563; border-radius:4px;
    padding:5px 12px; min-height:22px;
}
QPushButton:hover { background:#4b5563; }
QPushButton:default { background:#2563eb; border-color:#1d4ed8; }
"""


# The scraped page text in snapshot.raw can carry the account holder's name,
# email, or org as rendered on the provider page. Diagnostics are copied to the
# clipboard for bug reports, so redact email-shaped strings and cap the raw page
# text before it can leave the machine.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Azure identifiers. The Azure provider already builds its snapshot.raw as an
# allowlist that contains no ids at all, so this is defence in depth rather
# than the only guard - but an error string, a request URL echoed by requests,
# or a future provider can all still carry one, and a subscription or tenant
# GUID is an account identifier the way an email address is.
# \b is not usable as the left edge here: an encoded separator ends in a hex
# digit ("%2F"), so there is no word boundary between it and the id that
# follows - the resource names in an encoded path were redacted while the
# subscription id in the same string was not.
_ID_START = r"(?:(?<=%2F)|(?<=%2f)|(?<![0-9A-Za-z]))"
_ID_END = r"(?![0-9A-Za-z])"
_GUID_RE = re.compile(
    _ID_START + r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}" + _ID_END
)
# The same id written without its hyphens. Azure accepts and emits both forms,
# and the dashed pattern does not match this one at all.
_GUID_COMPACT_RE = re.compile(_ID_START + r"[0-9a-fA-F]{32}" + _ID_END)
# A path separator, as written or as requests echoes it back out of an encoded
# URL. A name may not contain either.
_SEP = r"(?:/|%2[Ff])"
_SEGMENT = r"(?:(?!%2[Ff])[^/\s\"'])+"
# Resource-group and resource names are chosen by the account holder and often
# name a client, a project, or a person. The shape is kept so a reader can still
# tell what kind of resource was involved.
_RESOURCE_GROUP_RE = re.compile(rf"(?i)({_SEP}resourceGroups{_SEP}){_SEGMENT}")
# Every type/name pair below /providers/<namespace>, not just the first: a
# Foundry project (accounts/<acct>/projects/<proj>) is a child resource, and a
# project name is exactly the customer-identifying string this pass removes.
_RESOURCE_NAME_RE = re.compile(
    rf"(?i)({_SEP}providers{_SEP}[A-Za-z0-9.]+)((?:{_SEP}[A-Za-z0-9]+{_SEP}{_SEGMENT})+)"
)
_RESOURCE_PAIR_RE = re.compile(rf"(?i)({_SEP}[A-Za-z0-9]+{_SEP}){_SEGMENT}")
_BODY_TEXT_LIMIT = 500
# Every other string in the payload is capped too, not just the key that
# happens to be named body_text. Snapshots now carry page- and Chromium-
# supplied strings (title, load_error_string, per-row raw text) whose length
# nothing on this side controls, and a name-based allowlist silently stops
# covering the payload the moment a new field is added.
_STRING_LIMIT = 2000
# Breadth, not just depth of a single value. Every field in snapshot.raw comes
# from a provider page, so a page that returns tens of thousands of keys - or a
# third-party script on it that plants them - would otherwise put megabytes of
# its own choosing into the blob users are told to paste into bug reports.
_MAX_KEYS = 200
_MAX_ITEMS = 50


def _redact_emails(text: str) -> str:
    return _EMAIL_RE.sub("[redacted-email]", text)


def _redact_azure_ids(text: str) -> str:
    """Strip Azure identifiers while leaving the resource *shape* readable.

    ``/subscriptions/<guid>/resourceGroups/<redacted>/providers/
    Microsoft.CognitiveServices/accounts/<redacted>`` still tells a reader
    which provider and resource type was involved, which is the part that helps
    with a bug report, without naming the subscription or the resource.

    Order matters only in one direction: the GUID passes run last, so a name
    that happens to look like a GUID is already gone and the subscription id -
    which is not part of any name - is still there to match.
    """
    text = _RESOURCE_NAME_RE.sub(
        lambda match: match.group(1)
        + _RESOURCE_PAIR_RE.sub(r"\1<redacted>", match.group(2)),
        text,
    )
    text = _RESOURCE_GROUP_RE.sub(r"\1<redacted>", text)
    text = _GUID_RE.sub("<guid>", text)
    return _GUID_COMPACT_RE.sub("<guid>", text)


def _sanitize_raw(raw: Any, *, limit: int = _STRING_LIMIT) -> Any:
    """Cap every string in the payload, wherever it sits.

    The cap used to apply only to a string that was a *direct* value of a dict,
    so anything inside a list walked past it - and page- and API-supplied
    strings sit in lists (``raw["buckets"]`` is [[ServiceName, cost], …],
    ``raw["notes"]`` is a list of strings). The recursion handles the string
    case itself now, which is the only place that covers every position.
    """
    if isinstance(raw, str):
        return raw[:limit] + "…[truncated]" if len(raw) > limit else raw
    if isinstance(raw, dict):
        sanitized: dict[str, Any] = {}
        if len(raw) > _MAX_KEYS:
            sanitized["…[truncated]"] = f"{len(raw) - _MAX_KEYS} more keys"
        for key, value in list(raw.items())[:_MAX_KEYS]:
            sanitized[key] = _sanitize_raw(
                value,
                limit=_BODY_TEXT_LIMIT if key == "body_text" else _STRING_LIMIT,
            )
        return sanitized
    if isinstance(raw, list):
        capped = [_sanitize_raw(item, limit=limit) for item in raw[:_MAX_ITEMS]]
        if len(raw) > _MAX_ITEMS:
            capped.append(f"…[truncated] {len(raw) - _MAX_ITEMS} more items")
        return capped
    return raw


def _format_diagnostics(provider: str, snapshot: UsageSnapshot) -> str:
    payload: dict[str, Any] = {
        # Fork and upstream ship overlapping release numbers, so a diagnostics
        # paste is ambiguous without the full version string (0.6.5+cfa.1).
        "app_version": __version__,
        "provider": provider,
        "status": snapshot.status.value,
        "fetched_at": snapshot.fetched_at.isoformat(timespec="seconds"),
        # snapshot.error is as API-supplied as raw is: an AAD error code and a
        # requests exception message both land here, and _redact_emails is
        # quadratic on a long non-matching string.
        "error": _sanitize_raw(snapshot.error),
        "raw": _sanitize_raw(snapshot.raw),
    }
    return _redact_azure_ids(
        _redact_emails(json.dumps(payload, indent=2, default=str))
    )


def reveal_path(path) -> None:
    """Open the OS file browser at this file/directory."""
    try:
        if sys.platform.startswith("win"):
            target = str(path)
            if os.path.isfile(target):
                subprocess.Popen(["explorer", "/select,", target])
            else:
                os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent if os.path.isfile(path) else path)])
    except Exception:  # noqa: BLE001
        pass


class ErrorDetailsDialog(QDialog):
    """Read-only window showing the last snapshot's error and raw payload.

    Provides Copy and "Open log folder" affordances so a user reporting a
    problem has something concrete to attach.
    """

    def __init__(self, provider: str, display_name: str, snapshot: UsageSnapshot, parent=None):
        super().__init__(None)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowTitle(f"{display_name} — error details")
        self.resize(560, 460)
        self.setStyleSheet(_DARK_STYLESHEET)

        # The header is RichText, so the error string is markup unless it is
        # escaped, and it is not redacted anywhere else on this path: the
        # clipboard blob below is, ai-gauge.log is, this label was not.
        error_text = _redact_azure_ids(
            html.escape(_sanitize_raw(snapshot.error) or "unknown error")
        )
        self._header_text = (
            f"<b>{html.escape(display_name)}</b> last refresh failed at "
            f"{snapshot.fetched_at.strftime('%Y-%m-%d %H:%M:%S')}.<br/>"
            f"<span style='color:#ef4444;'>{error_text}</span>"
        )
        header = QLabel(self._header_text)
        header.setWordWrap(True)
        header.setTextFormat(Qt.TextFormat.RichText)

        hint = QLabel(
            "If this keeps happening, try <b>Refresh now</b> first. If it persists, "
            "use <b>Paste cookie</b> in Settings to refresh the session — Claude/ChatGPT "
            "expire tokens periodically. <b>Copy diagnostics</b> if you want to share "
            "what the page returned."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9ca3af; font-size:11px;")

        diagnostics = _format_diagnostics(provider, snapshot)
        self._diagnostics_text = diagnostics
        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setPlainText(diagnostics)

        copy_btn = QPushButton("Copy diagnostics")
        copy_btn.clicked.connect(self._copy)
        log_btn = QPushButton("Open log folder")
        log_btn.clicked.connect(lambda: reveal_path(log_path()))

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        close_box.accepted.connect(self.accept)

        action_row = QHBoxLayout()
        action_row.addWidget(copy_btn)
        action_row.addWidget(log_btn)
        action_row.addStretch(1)
        action_row.addWidget(close_box)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)
        layout.addWidget(header)
        layout.addWidget(hint)
        layout.addWidget(body, 1)
        layout.addLayout(action_row)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self._diagnostics_text)
