"""Measure the panel's header hard floor at each zoom step (zoom-toggle.md section 5).

Usage: python3 floor.py <patched-tree> [0.75,1,1.25,...]
<patched-tree> is main at 5f09a55 with prototype-widget.diff applied:
    mkdir proto && git archive 5f09a55 | tar -x -C proto
    patch -p1 -d proto < docs/research/zoom/prototype-widget.diff
Runs offscreen; the config directory is redirected to a temporary folder on every OS.
Pass one zoom per process for the table: several steps in one process measured 30-55 px low
from 110% up, and the same step in a fresh process reproduces exactly.
"""
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
tree = os.path.abspath(sys.argv[1]); sys.path.insert(0, os.path.join(tree, "src")); sys.dont_write_bytecode = True
import aigauge; assert aigauge.__version__ == "1.4.2+cfa.10" and aigauge.__file__.startswith(tree)
# Never the real config: app_data_dir() ignores APPDATA/XDG on macOS, so patch the platform class.
import tempfile; from pathlib import Path; from aigauge.platforms import get_platform
_sandbox = Path(tempfile.mkdtemp(prefix="aigauge-zoom-")); type(get_platform()).app_data_dir = lambda self: _sandbox
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv)
from aigauge.config import Config
from aigauge.widget import UsageWidget
from aigauge.models import UsageSnapshot, UsageMetric, SnapshotStatus
from datetime import datetime, timedelta
now = datetime.now()
def snap(p): return UsageSnapshot(p, SnapshotStatus.OK, [UsageMetric("Session", 40.0, now + timedelta(hours=2)), UsageMetric("Weekly", 20.0, now + timedelta(days=3))], now - timedelta(seconds=1))
zooms = [float(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [1.0]
for z in zooms:
    c = Config(); c.window.ui_scale = z
    w = UsageWidget(c)
    for p in ("claude", "codex", "copilot"):
        w.update_snapshot(snap(p), p)
    w.set_refresh_state(True, 5, now + timedelta(minutes=4))
    w.show(); app.processEvents()
    hard = None; row_fit = None
    for W in range(700, 100, -1):
        w.setMinimumSize(50, 50); w.resize(W, 300); app.processEvents()
        w.layout().activate()
        hdr = w._header_widget
        ok = w.age_label.width() >= w.age_label.sizeHint().width() and w.close_btn.geometry().right() < hdr.width()
        if ok: hard = W
        r = w._tiles["claude"]._rows[0]
        if w._tile_scroll.horizontalScrollBar().maximum() == 0: row_fit = W
    floor = getattr(w, "_z", lambda n: n)(260)
    print(f"zoom {z}: header hard floor {hard} px (= {hard/z:.0f} at 100%), plain rows fit without h-scroll down to {row_fit} px; scaled 260 floor = {floor}")
