"""Render the app icon (Lucide "boxes" in the brand color) to a multi-size .ico."""

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "src" / "caspian" / "resources" / "icons" / "boxes.svg"
BRAND = "#0F766E"


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    painter.fillPath(path, QColor(BRAND))
    svg = SVG.read_text(encoding="utf-8").replace("currentColor", "#FFFFFF")
    margin = size * 0.18
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(margin, margin, size - 2 * margin,
                                                                  size - 2 * margin))
    painter.end()
    return image


def main(out: str) -> None:
    QGuiApplication(sys.argv[:1])
    # Qt's ICO writer stores one image per file; the 256px version is scaled by Windows.
    if not render(256).save(out, "ICO"):
        raise SystemExit("Could not write the icon (Qt ICO plugin missing?)")
    render(256).save(str(Path(out).with_suffix(".png")), "PNG")
    print(f"icon written: {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "packaging" / "caspian.ico"))
