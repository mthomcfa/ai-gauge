"""Zoom round trips for the window rules in zoom-toggle.md section 5.

Usage: python3 roundtrip.py <patched-tree>
<patched-tree> is main at 5f09a55 with prototype-widget.diff applied:
    P=$(mktemp -d) && git archive 5f09a55 | tar -x -C "$P"
    patch -p1 -d "$P" < docs/research/zoom/prototype-widget.diff
Runs offscreen; the config directory is redirected to a temporary folder on every OS.
"""
import os, sys, json
os.environ["QT_QPA_PLATFORM"] = "offscreen"
tree = os.path.abspath(sys.argv[1]); sys.path.insert(0, os.path.join(tree, "src")); sys.dont_write_bytecode = True
import aigauge; assert aigauge.__version__ == "1.4.2+cfa.10" and aigauge.__file__.startswith(tree)
# A throwaway config folder per run, outside the repo, so a run never writes next to the script.
import tempfile; from pathlib import Path; from aigauge.platforms import get_platform
_sandbox = Path(tempfile.mkdtemp(prefix="aigauge-zoom-")); type(get_platform()).app_data_dir = lambda self: _sandbox
import atexit, shutil; atexit.register(shutil.rmtree, _sandbox, True)
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv)
from aigauge.config import Config, config_path
from aigauge.widget import UsageWidget
from aigauge.models import UsageSnapshot, UsageMetric, SnapshotStatus
from datetime import datetime, timedelta
now = datetime.now()
def snap(p): return UsageSnapshot(p, SnapshotStatus.OK, [UsageMetric("Session", 40.0, now + timedelta(hours=2)), UsageMetric("Weekly", 20.0, now + timedelta(days=3))], now)
def run(label, user_sized, size, pos, steps, collapsed=False):
    c = Config()
    if user_sized:
        c.window.width, c.window.height, c.window.user_sized = size[0], size[1], True
    c.window.x, c.window.y = pos
    c.window.collapsed = collapsed
    w = UsageWidget(c)
    for p in ("claude", "codex", "copilot"):
        w.update_snapshot(snap(p), p)
    w.show(); app.processEvents(); w._do_refit_height(); app.processEvents()
    out = [f"{label}: start {w.width()}x{w.height()} at ({w.x()},{w.y()})"]
    for z in steps:
        w.set_zoom(z); app.processEvents()
        out.append(f"z{z}: {w.width()}x{w.height()} @({w.x()},{w.y()}) br=({w.x()+w.width()},{w.y()+w.height()}) min={w.minimumWidth()} saved={c.window.width}x{c.window.height} ui_scale={c.window.ui_scale}")
    print("\n  ".join(out))
    if os.path.exists(config_path()): os.remove(config_path())
run("auto-fit, parked bottom-right", False, None, (440, 480), [1.25, 1.5, 2.0, 1.0])
run("user-sized 400x500", True, (400, 500), (100, 100), [1.25, 1.5, 2.0, 1.0, 0.75, 1.0])
run("collapsed", False, None, (300, 600), [1.5, 1.0], collapsed=True)
run("user-sized 400x500 parked top-left", True, (400, 500), (20, 20), [1.25, 1.5, 2.0, 1.0])
run("auto-fit parked top-right", False, None, (440, 20), [1.5, 2.0, 0.75, 1.0])
