"""4.4: chat bubbles, the logo and toasts follow the theme; nothing stays light in dark mode."""

from PySide6.QtGui import QColor, QImage

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.ui.app_context import AppContext
from caspian.ui.assistant_page import AssistantPage
from caspian.ui.logo import without_white_margin
from caspian.ui.theme import DARK, LIGHT


async def test_assistant_bubbles_are_redrawn_in_the_dark_theme(qtbot, themes, db, admin):
    page = AssistantPage(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(page)
    assert LIGHT.surface_alt.lower() in page.transcript.toHtml().lower()
    themes.set_mode("dark")
    try:
        html = page.transcript.toHtml().lower()
        assert DARK.surface_alt.lower() in html and LIGHT.surface_alt.lower() not in html
    finally:
        themes.set_mode("light")


def test_logo_white_background_becomes_transparent_but_inner_white_stays():
    image = QImage(20, 20, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    for x in range(5, 15):
        for y in range(5, 15):
            image.setPixelColor(x, y, QColor("#0a4d8c"))  # the logo's shape
    image.setPixelColor(10, 10, QColor("white"))  # white inside the shape
    out = without_white_margin(image)
    assert out.pixelColor(0, 0).alpha() == 0 and out.pixelColor(19, 19).alpha() == 0
    assert out.pixelColor(6, 6).alpha() == 255
    assert out.pixelColor(10, 10).alpha() == 255 and out.pixelColor(10, 10).red() == 255
