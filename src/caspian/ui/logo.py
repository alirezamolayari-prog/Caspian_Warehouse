"""The installation's logo (caspian.branding) on screen: login dialog and sidebar."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
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
    scaled.setDevicePixelRatio(ratio)
    return scaled


class LogoLabel(QLabel):
    """The logo on a small white card, so a logo with a white background also looks right in the
    dark theme. Hidden when there is no logo file."""

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
