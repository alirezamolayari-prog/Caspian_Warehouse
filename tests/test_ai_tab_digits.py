"""Bug list round 2 #10: the AI tab's explanation has no Latin digits («(429)» → «(۴۲۹)»)."""

import re

from PySide6.QtWidgets import QLabel

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.ui.app_context import AppContext
from caspian.ui.settings_page import AITab


async def test_ai_intro_uses_persian_digits(qtbot, themes, db, admin):
    tab = AITab(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(tab)
    [intro] = [label.text() for label in tab.findChildren(QLabel) if "اولویت امتحان" in label.text()]
    assert "۴۲۹" in intro and not re.search("[0-9]", intro)
