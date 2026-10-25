"""Bug list round 1 #3: the version is shown with Persian digits («نسخه ۱.۱.۰»)."""

import re

from PySide6.QtWidgets import QLabel

from caspian import __version__
from caspian.core.settings import Settings
from caspian.core.text import to_persian_digits
from caspian.db.database import DbConfig
from caspian.ui.about import AboutDialog
from caspian.ui.app_context import AppContext
from caspian.ui.main_window import MainWindow


def _texts(widget) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel)]


async def test_status_bar_version_is_persian(qtbot, themes, db, admin):
    window = MainWindow(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(window)
    texts = _texts(window.statusBar())
    assert f"نسخه {to_persian_digits(__version__)}" in texts
    assert not any("نسخه" in t and re.search("[0-9]", t) for t in texts)


def test_about_dialog_version_is_persian(qtbot):
    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    versions = [t for t in _texts(dialog) if "نسخه" in t]
    assert versions and not any(re.search("[0-9]", t) for t in versions)
