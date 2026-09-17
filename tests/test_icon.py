"""The app icon: the committed assets parse, match the generator, and get set.

The generator and its output are both committed, so the risk is drift - a
change to the drawing that nobody re-ran the script for. These regenerate into
a temp directory and compare.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import make_icon  # noqa: E402

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ICO_PATH = REPO_ROOT / "assets" / "icon" / "ai-gauge.ico"
ICNS_PATH = REPO_ROOT / "assets" / "icon" / "ai-gauge.icns"
PNG_PATH = REPO_ROOT / "assets" / "icon" / "ai-gauge-256.png"
PACKAGE_PNG_PATH = REPO_ROOT / "src" / "aigauge" / "assets" / "ai-gauge-256.png"


def _ico_entries(data: bytes) -> list[tuple[int, int, int, int]]:
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    entries = []
    for index in range(count):
        head = data[6 + 16 * index : 22 + 16 * index]
        width, height, _palette, _res, _planes, bpp, length, offset = struct.unpack(
            "<BBBBHHII", head
        )
        entries.append((width or 256, height or 256, length, offset))
        assert bpp == 32
    return entries


def _icns_chunks(data: bytes) -> list[tuple[bytes, int]]:
    assert data[:4] == b"icns"
    assert struct.unpack(">I", data[4:8])[0] == len(data), "declared length is wrong"
    chunks = []
    cursor = 8
    while cursor < len(data):
        ostype = data[cursor : cursor + 4]
        length = struct.unpack(">I", data[cursor + 4 : cursor + 8])[0]
        assert length >= 8
        chunks.append((ostype, length))
        assert data[cursor + 8 : cursor + 16] == PNG_SIGNATURE
        cursor += length
    assert cursor == len(data), "the chunks do not fill the file"
    return chunks


def test_the_ico_holds_a_png_at_every_declared_size():
    entries = _ico_entries(ICO_PATH.read_bytes())
    assert [w for w, _h, _len, _off in entries] == list(make_icon.ICO_SIZES)
    data = ICO_PATH.read_bytes()
    for width, height, length, offset in entries:
        assert width == height
        assert data[offset : offset + 8] == PNG_SIGNATURE, f"{width}px is not a PNG"
        assert offset + length <= len(data)


def test_the_icns_holds_every_declared_type():
    chunks = _icns_chunks(ICNS_PATH.read_bytes())
    assert [ostype for ostype, _len in chunks] == [
        ostype for ostype, _size in make_icon.ICNS_TYPES
    ]


def test_the_256_png_is_a_png_and_the_two_copies_match():
    data = PNG_PATH.read_bytes()
    assert data[:8] == PNG_SIGNATURE
    assert PACKAGE_PNG_PATH.read_bytes() == data, (
        "the packaged runtime copy has drifted from the build-time one"
    )


def test_the_committed_assets_match_the_generator(qapp, tmp_path):
    """Byte-equality, not a pixel tolerance.

    Qt's PNG encoder and its antialiasing are deterministic for a fixed input
    on a fixed Qt build, and the offscreen platform takes the display out of
    it, so a byte compare is available and says more than a tolerance would.
    If this ever fails on a platform rather than on a change, the honest fix is
    to compare decoded pixels within a tolerance and say so here.
    """
    ico = tmp_path / "ai-gauge.ico"
    icns = tmp_path / "ai-gauge.icns"
    png = tmp_path / "ai-gauge-256.png"
    make_icon.generate(ico, icns, [png])

    assert ico.read_bytes() == ICO_PATH.read_bytes(), "run tools/make_icon.py"
    assert icns.read_bytes() == ICNS_PATH.read_bytes(), "run tools/make_icon.py"
    assert png.read_bytes() == PNG_PATH.read_bytes(), "run tools/make_icon.py"


def test_the_icon_uses_the_apps_own_band_colours(qapp):
    """The icon and the tiles read the same ColorThresholds, so a maintainer
    who changes the default green changes both or neither."""
    from aigauge.config import ColorThresholds

    bands = ColorThresholds()
    image = make_icon.render(256)
    # The first bar is filled to 47%: sample inside the fill and inside the
    # track beyond it, on the same scan line.
    bar_h = 256 * 0.14
    gap = 256 * 0.09
    y = int(256 * 0.5 - (3 * bar_h + 2 * gap) / 2 + bar_h / 2)
    margin = 256 * 0.17
    width = 256 - 2 * margin
    assert image.pixelColor(int(margin + width * 0.25), y).name() == bands.green_color
    assert image.pixelColor(int(margin + width * 0.75), y).name() == "#374151"


def test_the_window_icon_is_set_from_a_package_relative_path(qapp):
    """The lookup has to answer in a source checkout as well as in a bundle."""
    from aigauge import app as app_module

    path = app_module.app_icon_path()
    assert path.is_file(), path
    assert path.name == app_module.APP_ICON_NAME == make_icon.APP_ICON_NAME
    assert path.parent.name == app_module.ASSET_DIR_NAME
    assert make_icon.PACKAGE_ASSET_DIR.name == app_module.ASSET_DIR_NAME

    QApplication.setWindowIcon(app_module.QIcon())
    assert QApplication.windowIcon().isNull()

    assert app_module.apply_app_icon() is True
    assert QApplication.windowIcon().isNull() is False
    assert QApplication.windowIcon().availableSizes(), "the icon carries no pixmap"


@pytest.mark.parametrize(
    "script,needle",
    [
        ("build.ps1", "assets\\icon\\ai-gauge.ico"),
        ("build.ps1", "aigauge\\assets"),
        ("build.sh", "assets/icon/ai-gauge.icns"),
        ("build.sh", "assets/icon/ai-gauge-256.png"),
        ("build.sh", "aigauge/assets"),
    ],
    ids=["ps1-icon", "ps1-data", "sh-icns", "sh-png", "sh-data"],
)
def test_the_build_scripts_reference_the_icon(script, needle):
    assert needle in (REPO_ROOT / script).read_text(encoding="utf-8")


def test_the_build_output_directories_stay_ignored():
    """The generated icons are committed; dist/ and build/ are not."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "dist/" in ignored
    assert "build/" in ignored
    assert not any(line.strip().startswith("assets") for line in ignored)
