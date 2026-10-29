"""The installation's logo (caspian.branding) on screen: login dialog and sidebar."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import QLabel, QWidget

from caspian import branding


def logo_pixmap(height: int) -> QPixmap | None:
    """The logo scaled to `height` logical pixels (sharp on high-DPI screens), or None."""
    path = branding.logo_path()
    if path is None:
        return None
    source = QPixmap(str(path))
    if source.isNull():
        return None
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    scaled = source.scaledToHeight(round(height * ratio), Qt.TransformationMode.SmoothTransformation)
    scaled = QPixmap.fromImage(without_white_margin(scaled.toImage()))
    scaled.setDevicePixelRatio(ratio)
    return scaled


WHITE_LIMIT = 235  # r, g and b all above this count as the logo's white background


def without_white_margin(image: QImage) -> QImage:
    """The white background around a logo made transparent (4.4: no white box in the dark theme).
    Only white connected to the edges goes; white inside the logo (letters, shapes) stays."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()

    def white(x: int, y: int) -> bool:
        c = image.pixelColor(x, y)
        return c.alpha() > 0 and min(c.red(), c.green(), c.blue()) > WHITE_LIMIT

    todo = [(x, y) for x in range(width) for y in (0, height - 1)]
    todo += [(x, y) for y in range(height) for x in (0, width - 1)]
    seen = set()
    while todo:
        x, y = todo.pop()
        if (x, y) in seen or not (0 <= x < width and 0 <= y < height) or not white(x, y):
            continue
        seen.add((x, y))
        image.setPixelColor(x, y, QColor(0, 0, 0, 0))
        todo += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    return image


class LogoLabel(QLabel):
    """The logo with its white background made transparent, on the theme's own surface (no white
    box in the dark theme). Hidden when there is no logo file."""

    def __init__(self, height: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LogoCard")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = logo_pixmap(height)
        if pixmap is None:
            self.hide()
        else:
            self.setPixmap(pixmap)
        self.has_logo = pixmap is not None
