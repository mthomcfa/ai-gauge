"""The one scroll-bar stylesheet the whole app paints.

Two surfaces scroll: the floating widget's tile stack (panel ``#111827``) and
every Settings tab (panel ``#1f2937``). They had nothing in common - the
widget drew a 6 px handle on a track the same colour as the panel, so the bar
read as a floating sliver with no trough at all, and the dialog drew whatever
Qt's default style felt like. A scroll bar is the only thing on screen that
says "there is more below", so it has to be visible before it is subtle.

The fragment is a function of the surface rather than a constant because the
track has to be *lighter than the panel it sits on* and the two panels differ;
``SCROLLBAR_STYLESHEET`` is the widget's, i.e. the common case.

The rules cover both axes. Neither surface scrolls horizontally today - the
widget's tile rows compress and the dialog's pages are told never to - but a
bar that only exists on one axis is how a half-styled horizontal bar appears
the first time a window is narrow enough, in Qt's default grey.
"""
from __future__ import annotations

# Panel colours the two surfaces paint themselves with.
WIDGET_PANEL = "#111827"
DIALOG_PANEL = "#1f2937"

# The track is one step up the same grey ramp from whichever panel it is on.
WIDGET_TRACK = "#1f2937"
DIALOG_TRACK = "#374151"

SCROLLBAR_WIDTH = 10
SCROLLBAR_MIN_HANDLE = 24
# Inset each side of the handle so the track shows around it. The radius is
# half what is left, which is what makes the handle a pill rather than a
# rounded rectangle.
_HANDLE_MARGIN = 2
_HANDLE_RADIUS = (SCROLLBAR_WIDTH - 2 * _HANDLE_MARGIN) // 2

HANDLE = "#4b5563"
HANDLE_HOVER = "#6b7280"
HANDLE_PRESSED = "#9ca3af"


def scrollbar_stylesheet(*, panel: str, track: str) -> str:
    """Scroll-bar rules for a scroll area painted ``panel``.

    ``panel`` is also what the corner square between the two bars is painted,
    so a scroll area showing both does not expose Qt's default grey there.
    """
    return f"""
QScrollBar:vertical {{
    background: {track};
    width: {SCROLLBAR_WIDTH}px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {HANDLE};
    border-radius: {_HANDLE_RADIUS}px;
    min-height: {SCROLLBAR_MIN_HANDLE}px;
    margin: {_HANDLE_MARGIN}px;
}}
QScrollBar::handle:vertical:hover {{ background: {HANDLE_HOVER}; }}
QScrollBar::handle:vertical:pressed {{ background: {HANDLE_PRESSED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
    width: 0;
    border: none;
    background: none;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: {track};
    height: {SCROLLBAR_WIDTH}px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {HANDLE};
    border-radius: {_HANDLE_RADIUS}px;
    min-width: {SCROLLBAR_MIN_HANDLE}px;
    margin: {_HANDLE_MARGIN}px;
}}
QScrollBar::handle:horizontal:hover {{ background: {HANDLE_HOVER}; }}
QScrollBar::handle:horizontal:pressed {{ background: {HANDLE_PRESSED}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    height: 0;
    width: 0;
    border: none;
    background: none;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}
QAbstractScrollArea::corner {{ background: {panel}; border: none; }}
"""


SCROLLBAR_STYLESHEET = scrollbar_stylesheet(panel=WIDGET_PANEL, track=WIDGET_TRACK)
DIALOG_SCROLLBAR_STYLESHEET = scrollbar_stylesheet(
    panel=DIALOG_PANEL, track=DIALOG_TRACK
)
