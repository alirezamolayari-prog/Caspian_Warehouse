import logging

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from caspian.resources import FONTS_DIR

log = logging.getLogger(__name__)

FONT_FAMILY = "Vazirmatn UI"


def load_fonts() -> list[str]:
    """Register the bundled Vazirmatn fonts. Returns the family names loaded."""
    families: set[str] = set()
    for path in sorted(FONTS_DIR.glob("*.ttf")):
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            log.error("Failed to load font %s", path.name)
            continue
        families.update(QFontDatabase.applicationFontFamilies(font_id))
    return sorted(families)


def apply_app_font(app: QApplication, point_size: float = 10) -> None:
    families = load_fonts()
    family = FONT_FAMILY if FONT_FAMILY in families else (families[0] if families else "")
    if not family:
        log.warning("Bundled font unavailable; using system default")
        return
    font = QFont(family)
    font.setPointSizeF(point_size)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
