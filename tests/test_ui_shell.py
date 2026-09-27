import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase

from caspian.core.settings import Settings
from caspian.resources import ICONS_DIR
from caspian.ui.fonts import FONT_FAMILY, load_fonts
from caspian.ui.main_window import MainWindow
from caspian.ui.pages import PAGES
from caspian.ui.theme import DARK, LIGHT, ThemeManager


@pytest.fixture
def window(qtbot, qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "caspian.core.settings.settings_path", lambda: tmp_path / "settings.json"
    )
    themes = ThemeManager(qapp, "light")
    win = MainWindow(themes, Settings())
    qtbot.addWidget(win)
    return win, themes


def test_vazirmatn_loads(qapp):
    assert FONT_FAMILY in load_fonts()
    assert FONT_FAMILY in QFontDatabase.families()


def test_all_page_icons_bundled():
    for spec in PAGES:
        assert (ICONS_DIR / f"{spec.icon}.svg").exists(), spec.icon


def test_navigation(window):
    win, _ = window
    for spec in PAGES:
        win.navigate(spec.key)
        assert win.current_page == spec.key
        assert win._page_title.text() == spec.title


def test_nav_button_click(window, qtbot):
    win, _ = window
    qtbot.mouseClick(win._nav_buttons["items"], Qt.MouseButton.LeftButton)
    assert win.current_page == "items"


def test_theme_toggle(window, qapp):
    _, themes = window
    assert themes.current is LIGHT
    themes.toggle()
    assert themes.current is DARK
    assert themes.mode == "dark"
    assert DARK.bg.lower() in qapp.styleSheet().lower()
    themes.toggle()
    assert themes.current is LIGHT


def test_settings_page_changes_theme(window):
    win, themes = window
    page = win._pages["settings"]
    page.theme_combo.setCurrentIndex(page.theme_combo.findData("dark"))
    assert themes.current is DARK
    themes.set_mode("light")
    assert page.theme_combo.currentData() == "light"


def test_close_saves_settings(window, tmp_path):
    win, themes = window
    themes.set_mode("dark")
    win.close()
    assert Settings.load(tmp_path / "settings.json").theme_mode == "dark"
