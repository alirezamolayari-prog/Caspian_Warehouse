"""Bug list round 2 #11: the «اتصال به سرور» tab says plainly that it may stay empty — the app works
fully without Caspian Server (go-live before the server is ready)."""

from PySide6.QtWidgets import QLabel

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.ui.app_context import AppContext
from caspian.ui.settings_page import ServerTab


async def test_server_tab_says_it_can_stay_empty(qtbot, themes, db, admin):
    tab = ServerTab(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(tab)
    text = " ".join(label.text() for label in tab.findChildren(QLabel))
    assert "خالی بگذارید" in text and "بدون سرور" in text
