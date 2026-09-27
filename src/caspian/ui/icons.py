"""Theme-aware icons from the bundled Lucide SVG set (ISC license).

SVGs use `currentColor`; we substitute the requested color and rasterize at
several sizes so icons stay crisp on high-DPI screens.
"""

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from caspian.resources import ICONS_DIR

_SIZES = (16, 20, 24, 32, 48)


@lru_cache(maxsize=64)
def _svg_source(name: str) -> str:
    return (ICONS_DIR / f"{name}.svg").read_text(encoding="utf-8")


@lru_cache(maxsize=512)
def icon(name: str, color: str) -> QIcon:
    data = _svg_source(name).replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(data.encode("utf-8")))
    result = QIcon()
    for size in _SIZES:
        pixmap = QPixmap(QSize(size, size) * 2)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        result.addPixmap(pixmap)
    return result
