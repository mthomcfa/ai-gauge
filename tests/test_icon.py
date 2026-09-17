"""The app icon: the committed assets parse, match the generator, and get set.

The generator and its output are both committed, so the risk is drift - a
change to the drawing that nobody re-ran the script for. These regenerate into
a temp directory and compare: the containers by structure, the pictures by
decoded pixels, and the flat colours exactly.
"""
from __future__ import annotations

import operator
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import make_icon  # noqa: E402

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Decoded pixels, not bytes. The first cut compared bytes and failed on all
# three CI runners against assets generated elsewhere: Qt's PNG encoder does
# not produce the same stream on every build for the same picture. A channel
# may differ by this much before the assets count as drifted - antialiasing
# rounds by one or two; a moved edge or a changed colour lands at 255.
PIXEL_TOLERANCE = 8
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


def _ico_payloads(data: bytes) -> list[tuple[int, bytes]]:
    return [
        (width, data[offset : offset + length])
        for width, _height, length, offset in _ico_entries(data)
    ]


def _icns_payloads(data: bytes) -> list[tuple[bytes, bytes]]:
    payloads = []
    cursor = 8
    for ostype, length in _icns_chunks(data):
        payloads.append((ostype, data[cursor + 8 : cursor + length]))
        cursor += length
    return payloads


def _rgba(png: bytes) -> tuple[tuple[int, int], bytes]:
    image = QImage.fromData(png, "PNG")
    assert not image.isNull(), "not a decodable PNG"
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = bytes(image.constBits().asarray(image.sizeInBytes()))
    return (image.width(), image.height()), pixels


def _assert_same_picture(expected: bytes, actual: bytes, label: str) -> None:
    expected_size, expected_pixels = _rgba(expected)
    actual_size, actual_pixels = _rgba(actual)
    assert actual_size == expected_size, label
    if actual_pixels == expected_pixels:
        return
    worst = max(map(abs, map(operator.sub, expected_pixels, actual_pixels)))
    assert worst <= PIXEL_TOLERANCE, (
        f"{label}: a channel differs by {worst}; run tools/make_icon.py"
    )


def _bar_centre_line(size: int, index: int) -> int:
    bar_h = size * 0.14
    gap = size * 0.09
    top = size * 0.5 - (3 * bar_h + 2 * gap) / 2
    return int(top + index * (bar_h + gap) + bar_h / 2)


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
    """Structure exactly, pictures within ``PIXEL_TOLERANCE``.

    Every entry of both containers is decoded and compared to a fresh render,
    in order, size by size and type by type, so a drawing change that nobody
    re-ran the script for fails here on the platform that made it and on
    every other. What is *not* compared is the PNG stream, which is the
    encoder's business and differed on every CI runner (see the tolerance).
    """
    ico = tmp_path / "ai-gauge.ico"
    icns = tmp_path / "ai-gauge.icns"
    png = tmp_path / "ai-gauge-256.png"
    make_icon.generate(ico, icns, [png])

    committed_ico = _ico_payloads(ICO_PATH.read_bytes())
    fresh_ico = _ico_payloads(ico.read_bytes())
    assert [size for size, _ in fresh_ico] == [size for size, _ in committed_ico]
    for (size, want), (_, got) in zip(committed_ico, fresh_ico):
        _assert_same_picture(want, got, f"ico {size}px")

    committed_icns = _icns_payloads(ICNS_PATH.read_bytes())
    fresh_icns = _icns_payloads(icns.read_bytes())
    assert [t for t, _ in fresh_icns] == [t for t, _ in committed_icns]
    for (ostype, want), (_, got) in zip(committed_icns, fresh_icns):
        _assert_same_picture(want, got, f"icns {ostype!r}")

    _assert_same_picture(PNG_PATH.read_bytes(), png.read_bytes(), "256 png")


def test_the_committed_png_carries_the_apps_own_colours(qapp):
    """Exact, on the committed file, where the tolerance above is not.

    A flat interior is the same on every platform - antialiasing touches
    edges only - so the base, the track and each band's fill are read from
    the shipped 256 px PNG and compared exactly to ``ColorThresholds``. This
    is what catches a colour nudged by less than ``PIXEL_TOLERANCE``.
    """
    from aigauge.config import ColorThresholds

    bands = ColorThresholds()
    image = QImage.fromData(PNG_PATH.read_bytes(), "PNG")
    assert (image.width(), image.height()) == (256, 256)
    margin = 256 * 0.17
    width = 256 - 2 * margin
    assert image.pixelColor(128, 8).name() == "#1f2937", "the base"
    for index, expected in enumerate(
        (bands.green_color, bands.yellow_color, bands.red_color)
    ):
        y = _bar_centre_line(256, index)
        assert image.pixelColor(int(margin + width * 0.10), y).name() == expected
        assert image.pixelColor(int(margin + width * 0.97), y).name() == "#374151", (
            "the track beyond every fill"
        )


def test_the_icon_uses_the_apps_own_band_colours(qapp):
    """The icon and the tiles read the same ColorThresholds, so a maintainer
    who changes the default green changes both or neither."""
    from aigauge.config import ColorThresholds

    bands = ColorThresholds()
    image = make_icon.render(256)
    # The first bar is filled to 47%: sample inside the fill and inside the
    # track beyond it, on the same scan line.
    y = _bar_centre_line(256, 0)
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


def test_the_generator_asks_for_no_bytecode_before_it_imports_the_app(monkeypatch):
    """`_band_colors()` imports `aigauge.config` so the icon's bands and the
    tiles' cannot drift, and without the flag that import left five `.pyc`
    files under `src/` - from a tool whose job is to write four assets. The
    README says the script writes nothing but those four files; nothing
    enforced it."""
    monkeypatch.setattr(make_icon, "_BAND_CACHE", None)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)

    make_icon._band_colors()

    assert sys.dont_write_bytecode is True


def test_the_flag_is_set_before_any_aigauge_module_is_imported():
    """The order is the guarantee: a flag set after the import is a flag set
    after the `.pyc` has been written. Read in a subprocess, because this one
    imported `aigauge` long before the generator was asked for anything."""
    probe = (
        "import sys\n"
        "sys.dont_write_bytecode = False\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'tools')!r})\n"
        "import make_icon\n"
        "before = (sys.dont_write_bytecode,\n"
        "          any(m == 'aigauge' or m.startswith('aigauge.') for m in sys.modules))\n"
        "make_icon._band_colors()\n"
        "print(before[0], before[1], sys.dont_write_bytecode,\n"
        "      'aigauge.config' in sys.modules)\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    flag_before, aigauge_before, flag_after, imported = result.stdout.split()[-4:]
    assert flag_before == "False", "importing the module alone asked for it"
    assert aigauge_before == "False", "the module pulls in aigauge at import time"
    assert imported == "True", "_band_colors() never imported aigauge.config"
    assert flag_after == "True", "the import ran without the flag"
