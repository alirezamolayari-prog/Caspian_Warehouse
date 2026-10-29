"""4.3: leaving a settings tab or the settings page with unsaved edits asks first."""

from PySide6.QtTest import QTest

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.ui import settings_page
from caspian.ui.app_context import AppContext
from caspian.ui.settings_page import SettingsPage
from helpers import wait_until


async def test_switching_tab_with_unsaved_edits_asks(qtbot, themes, db, admin, monkeypatch):
    answers, asked = [False, True], []

    async def confirm(parent, text, *args, **kwargs):
        asked.append(text)
        return answers.pop(0)

    monkeypatch.setattr(settings_page, "confirm", confirm)
    page = SettingsPage(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(page)
    page.show()
    page.tabs.setCurrentIndex(page.server_index)
    QTest.keyClicks(page.server.username, "x")  # the user edits a field
    assert page.has_unsaved()
    page.tabs.setCurrentIndex(page.backup_index)
    assert await wait_until(lambda: page.tabs.currentIndex() == page.server_index)  # «ماندن»
    assert asked and "ذخیره نشده" in asked[0]
    page.tabs.setCurrentIndex(page.backup_index)
    assert await wait_until(lambda: not page.has_unsaved())  # «ادامه بدون ذخیره»
    assert page.tabs.currentIndex() == page.backup_index


async def test_saving_clears_the_question(qtbot, themes, db, admin, monkeypatch):
    page = SettingsPage(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(page)
    page.show()
    page.tabs.setCurrentIndex(page.server_index)
    QTest.keyClicks(page.server.username, "x")
    monkeypatch.setattr(page.server, "on_save", lambda: None)
    page.server.save_button.click()
    assert not page.has_unsaved() and await page.confirm_leave()
