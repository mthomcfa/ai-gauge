"""Draw the AI Gauge app icon and write the ICO, ICNS and PNG the builds use.

Run from the repo root:

    QT_QPA_PLATFORM=offscreen python3 tools/make_icon.py

Stdlib plus PyQt6, which the app already depends on - no Pillow, no icon
toolchain, nothing fetched. The ICO and ICNS containers are written here
because both are short, well-specified formats whose payload is a PNG, and a
dependency added to draw one picture is a dependency in the shipped binary's
supply chain for the life of the project.

**The drawing is candidate B**, the one that was chosen from three rendered
offscreen: three stacked pill bars at 47 %, 72 % and 92 % on the app's own
rounded dark panel. It reads at 16 px, where a dial's needle does not, and it
is the compact chip row the panel already shows. The band colours come from
``ColorThresholds``, so the icon and the tiles cannot drift apart.

**Two destinations, on purpose.** ``assets/icon/`` is the build-time set that
``build.ps1`` and ``build.sh`` point PyInstaller at. ``src/aigauge/assets/`` is
the runtime copy that travels *inside the package*, so one package-relative
lookup finds it in a source checkout, in a wheel and in a frozen bundle alike -
the same arrangement the meter catalog uses. They are byte-identical and a test
regenerates both and compares.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter

REPO_ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = REPO_ROOT / "assets" / "icon"
PACKAGE_ASSET_DIR = REPO_ROOT / "src" / "aigauge" / "assets"

# Written into the ICO directory; 256 is stored as 0 in the byte field, which
# is what the format uses for "256 or larger".
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# ICNS type -> pixel size. The four at the end are the @2x variants; the
# container carries no separate scale field, so a Retina entry is simply a
# different OSType holding a bigger PNG.
ICNS_TYPES = (
    (b"icp4", 16),
    (b"icp5", 32),
    (b"icp6", 64),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
    (b"ic11", 32),
    (b"ic12", 64),
    (b"ic13", 256),
    (b"ic14", 512),
)

PNG_SIZE = 256
# Must match app.APP_ICON_NAME: that is the name the runtime looks for.
APP_ICON_NAME = "ai-gauge-256.png"

BASE = QColor("#1f2937")
BORDER = QColor("#374151")


_BAND_CACHE: tuple[QColor, QColor, QColor] | None = None


def _band_colors() -> tuple[QColor, QColor, QColor]:
    """The app's own default green / yellow / red, not a second set here."""
    global _BAND_CACHE
    if _BAND_CACHE is None:
        if str(REPO_ROOT / "src") not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "src"))
        from aigauge.config import ColorThresholds

        bands = ColorThresholds()
        _BAND_CACHE = (
            QColor(bands.green_color),
            QColor(bands.yellow_color),
            QColor(bands.red_color),
        )
    return _BAND_CACHE


def render(size: int) -> QImage:
    """Candidate B: three stacked pill bars at different fills.

    Every measurement is a fraction of the icon's own size, so the 16 px entry
    is the 1024 px entry scaled rather than a second drawing that drifts.
    """
    green, yellow, red = _band_colors()
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    radius = size * 0.22
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(BASE)
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    margin = size * 0.17
    bar_h = size * 0.14
    gap = size * 0.09
    top = size * 0.5 - (3 * bar_h + 2 * gap) / 2
    for index, (fraction, color) in enumerate(
        ((0.47, green), (0.72, yellow), (0.92, red))
    ):
        y = top + index * (bar_h + gap)
        track = QRectF(margin, y, size - 2 * margin, bar_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(BORDER)
        painter.drawRoundedRect(track, bar_h / 2, bar_h / 2)
        fill = QRectF(margin, y, (size - 2 * margin) * fraction, bar_h)
        painter.setBrush(color)
        painter.drawRoundedRect(fill, bar_h / 2, bar_h / 2)
    painter.end()
    return image


def png_bytes(size: int) -> bytes:
    """One rendered size as a PNG blob, which is what both containers hold."""
    # The QByteArray is bound to a name on purpose: a temporary handed to
    # QBuffer is collected out from under it and the encode segfaults.
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not render(size).save(buffer, "PNG"):
        raise SystemExit(f"could not encode a {size}px PNG")
    buffer.close()
    return bytes(data)


def build_ico(blobs: dict[int, bytes]) -> bytes:
    """A 6-byte header, one 16-byte directory entry per size, then the PNGs.

    Vista and later accept a PNG payload in place of a DIB, which is what
    keeps a 256 px entry from costing 256 KiB of uncompressed bitmap. The
    width/height bytes are 0 for 256 - the field is one byte and 256 does not
    fit in it.
    """
    count = len(blobs)
    header = struct.pack("<HHH", 0, 1, count)  # reserved, type 1 = icon, count
    offset = len(header) + 16 * count
    directory = b""
    payload = b""
    for size in sorted(blobs):
        blob = blobs[size]
        directory += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # width
            size if size < 256 else 0,  # height
            0,  # palette entries; 0 for a true-colour image
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(blob),
            offset,
        )
        payload += blob
        offset += len(blob)
    return header + directory + payload


def build_icns(blobs: dict[int, bytes]) -> bytes:
    """Big-endian 'icns' magic, total length, then OSType/length/PNG chunks.

    Each chunk's length field counts the 8-byte header itself, and so does the
    file's, which is why the total is computed after the chunks are built.
    """
    chunks = b""
    for ostype, size in ICNS_TYPES:
        blob = blobs[size]
        chunks += ostype + struct.pack(">I", 8 + len(blob)) + blob
    return b"icns" + struct.pack(">I", 8 + len(chunks)) + chunks


def generate(ico_path: Path, icns_path: Path, png_paths: list[Path]) -> None:
    """Render every size once and write all three artefacts from it."""
    sizes = set(ICO_SIZES) | {size for _type, size in ICNS_TYPES} | {PNG_SIZE}
    blobs = {size: png_bytes(size) for size in sorted(sizes)}
    ico_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.write_bytes(build_ico({size: blobs[size] for size in ICO_SIZES}))
    icns_path.parent.mkdir(parents=True, exist_ok=True)
    icns_path.write_bytes(build_icns(blobs))
    for path in png_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blobs[PNG_SIZE])


def main() -> int:
    from PyQt6.QtGui import QGuiApplication

    # QImage and QPainter need a GUI application object; offscreen is enough
    # and is what CI and the drift test use.
    app = QGuiApplication(sys.argv)  # noqa: F841 - kept alive for the painter
    ico = ICON_DIR / "ai-gauge.ico"
    icns = ICON_DIR / "ai-gauge.icns"
    pngs = [ICON_DIR / APP_ICON_NAME, PACKAGE_ASSET_DIR / APP_ICON_NAME]
    generate(ico, icns, pngs)
    for path in (ico, icns, *pngs):
        print(f"wrote {path.relative_to(REPO_ROOT)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
