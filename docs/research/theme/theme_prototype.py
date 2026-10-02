"""PROTOTYPE - colour tokens per scheme, and stylesheets regenerated on change.

Every inline stylesheet is written as a ``string.Template`` with ``$token``
placeholders (QSS uses braces, never ``$``) and applied through ``styled()``,
which remembers the template on the widget as a dynamic property. A scheme
change re-renders every remembered template and repaints every widget; custom
painters read ``tok()`` at paint time.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from string import Template

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

_PROP = "aigauge_qss"


@dataclass(frozen=True)
class Tokens:
    name: str
    panel: str; panel_edge: str; dialog: str; field: str
    control: str; control_hover: str; control_pressed: str; button: str; button_hover: str
    track: str; track_skeleton: str; chunk_skeleton: str
    border: str; border_strong: str
    text_strong: str; text: str; text_label: str; text_secondary: str; text_muted: str
    on_accent: str; accent: str; accent_strong: str; focus: str; link: str
    error: str; warning: str; warning_fill: str
    scroll_track: str; scroll_handle: str; scroll_handle_hover: str; scroll_handle_pressed: str
    chip_base: str; chip_border: str; chip_neutral: str; chip_text: str
    chip_darker: int
    pace_tick: tuple; pace_shadow: tuple; chip_notch: tuple
    band_green: str; band_yellow: str; band_orange: str; band_red: str


DARK = Tokens(
    "dark",
    panel="#111827", panel_edge="#1f2937", dialog="#1f2937", field="#111827",
    control="#374151", control_hover="#4b5563", control_pressed="#6b7280",
    button="#4b5563", button_hover="#6b7280",
    track="#374151", track_skeleton="#1f2937", chunk_skeleton="#4b5563",
    border="#374151", border_strong="#4b5563",
    text_strong="#f3f4f6", text="#e5e7eb", text_label="#d1d5db",
    text_secondary="#9ca3af", text_muted="#6b7280",
    on_accent="#f3f4f6", accent="#2563eb", accent_strong="#1d4ed8", focus="#3b82f6",
    link="#60a5fa",
    error="#ef4444", warning="#f59e0b", warning_fill="#92400e",
    scroll_track="#1f2937", scroll_handle="#4b5563", scroll_handle_hover="#6b7280",
    scroll_handle_pressed="#9ca3af",
    chip_base="#1f2937", chip_border="#374151", chip_neutral="#374151", chip_text="#f9fafb",
    chip_darker=135,
    pace_tick=(243, 244, 246, 180), pace_shadow=(17, 24, 39, 120),
    chip_notch=(229, 231, 235, 230),
    band_green="#22c55e", band_yellow="#f59e0b", band_orange="#f97316", band_red="#ef4444",
)

LIGHT = Tokens(
    "light",
    panel="#f9fafb", panel_edge="#d1d5db", dialog="#f3f4f6", field="#ffffff",
    control="#e5e7eb", control_hover="#d1d5db", control_pressed="#9ca3af",
    button="#e5e7eb", button_hover="#d1d5db",
    track="#e5e7eb", track_skeleton="#f3f4f6", chunk_skeleton="#d1d5db",
    border="#d1d5db", border_strong="#9ca3af",
    text_strong="#111827", text="#1f2937", text_label="#374151",
    text_secondary="#4b5563", text_muted="#6b7280",
    on_accent="#ffffff", accent="#2563eb", accent_strong="#1d4ed8", focus="#2563eb",
    link="#1d4ed8",
    error="#b91c1c", warning="#b45309", warning_fill="#b45309",
    scroll_track="#e5e7eb", scroll_handle="#9ca3af", scroll_handle_hover="#6b7280",
    scroll_handle_pressed="#4b5563",
    chip_base="#ffffff", chip_border="#d1d5db", chip_neutral="#e5e7eb", chip_text="#111827",
    chip_darker=100,
    pace_tick=(17, 24, 39, 200), pace_shadow=(255, 255, 255, 170),
    chip_notch=(17, 24, 39, 200),
    band_green="#15803d", band_yellow="#a16207", band_orange="#c2410c", band_red="#b91c1c",
)

_current = DARK
_DARK_BAND_DEFAULTS = {
    "green_color": DARK.band_green, "yellow_color": DARK.band_yellow,
    "orange_color": DARK.band_orange, "red_color": DARK.band_red,
}


def tok() -> Tokens:
    return _current


def qss(template: str) -> str:
    values = {f.name: getattr(_current, f.name) for f in fields(Tokens)}
    return Template(template).substitute(values)


def styled(widget: QWidget, template: str) -> None:
    widget.setProperty(_PROP, template)
    widget.setStyleSheet(qss(template))


def band_color(field_name: str, stored: str) -> str:
    """A colour the user never changed follows the theme; one they chose does not."""
    if stored.lower() == _DARK_BAND_DEFAULTS[field_name]:
        return getattr(_current, "band_" + field_name.removesuffix("_color"))
    return stored


class _Notifier(QObject):
    changed = pyqtSignal(str)


notifier = _Notifier()


def set_scheme(name: str) -> None:
    global _current
    new = LIGHT if name == "light" else DARK
    if new is _current:
        return
    _current = new
    app = QApplication.instance()
    if app is not None:
        for w in app.allWidgets():
            template = w.property(_PROP)
            if template:
                w.setStyleSheet(qss(template))
        notifier.changed.emit(new.name)
        for w in app.allWidgets():
            w.update()
