import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.resources import ICONS_DIR
from caspian.services import auth, users
from caspian.ui.app_context import AppContext
from caspian.ui.fonts import FONT_FAMILY, load_fonts
from caspian.ui.main_window import MainWindow
from caspian.ui.pages import PAGES
from caspian.ui.theme import DARK, LIGHT, ThemeManager

ADMIN_PW = "Str0ngPass"


@pytest.fixture
async def admin(db):
    actor = (await auth.login(db, "admin", "admin")).actor
    await auth.change_password(db, actor, "admin", ADMIN_PW)
    await auth.set_pin(db, actor, ADMIN_PW, "4826")
    return actor


@pytest.fixture
def make_window(qtbot, qapp, tmp_path, monkeypatch, db):
    monkeypatch.setattr("caspian.core.settings.settings_path", lambda: tmp_path / "settings.json")

    def factory(actor):
        themes = ThemeManager(qapp, "light")
        ctx = AppContext(db, DbConfig(), Settings(), themes, actor)
        win = MainWindow(ctx)
        qtbot.addWidget(win)
        return win

    return factory


@pytest.fixture
def window(make_window, admin):
    return make_window(admin)


def test_vazirmatn_loads(qapp):
    assert FONT_FAMILY in load_fonts()
    assert FONT_FAMILY in QFontDatabase.families()


def test_all_page_icons_bundled():
    for spec in PAGES:
        assert (ICONS_DIR / f"{spec.icon}.svg").exists(), spec.icon


def test_admin_sees_every_page(window):
    for spec in PAGES:
        window.navigate(spec.key)
        assert window.current_page == spec.key
        assert window._page_title.text() == spec.title
        assert not window._nav_buttons[spec.key].isHidden()


def test_nav_button_click(window, qtbot):
    qtbot.mouseClick(window._nav_buttons["items"], Qt.MouseButton.LeftButton)
    assert window.current_page == "items"


async def test_viewer_sees_only_permitted_pages(make_window, db, admin):
    await users.create_user(db, admin, "neda", "ندا", "Neda#2026", "viewer")
    viewer = (await auth.login(db, "neda", "Neda#2026")).actor
    win = make_window(viewer)
    assert win._nav_buttons["items"].isHidden() is False
    for key in ("users", "imports", "stocktake", "assistant"):
        assert win._nav_buttons[key].isHidden(), key
    win.navigate("users")  # blocked -> falls back to dashboard
    assert win.current_page == "dashboard"
    assert win._user_button.text() == "ندا"
    assert not win._pin_action.isVisible()


async def test_switching_user_updates_window(window, db, admin):
    await users.create_user(db, admin, "neda", "ندا", "Neda#2026", "viewer")
    window.navigate("users")
    viewer = (await auth.login(db, "neda", "Neda#2026")).actor
    window.ctx.set_actor(viewer)
    assert window.current_page == "dashboard"
    assert window._nav_buttons["users"].isHidden()
    assert window._user_button.text() == "ندا"


def test_theme_toggle(window, qapp):
    themes = window.ctx.themes
    assert themes.current is LIGHT
    themes.toggle()
    assert themes.current is DARK
    assert themes.mode == "dark"
    assert DARK.bg.lower() in qapp.styleSheet().lower()
    themes.toggle()
    assert themes.current is LIGHT


def test_settings_page_changes_theme(window):
    themes = window.ctx.themes
    page = window._pages["settings"]
    page.theme_combo.setCurrentIndex(page.theme_combo.findData("dark"))
    assert themes.current is DARK
    themes.set_mode("light")
    assert page.theme_combo.currentData() == "light"


def test_close_saves_settings(window, tmp_path):
    window.ctx.themes.set_mode("dark")
    window.close()
    assert Settings.load(tmp_path / "settings.json").theme_mode == "dark"


async def test_users_page_lists_users(window, db, admin):
    await users.create_user(db, admin, "neda", "ندا", "Neda#2026", "viewer")
    page = window._pages["users"]
    await page.refresh()
    assert page.table.rowCount() == 2
    assert {page.table.item(r, 0).text() for r in range(2)} == {"admin", "neda"}
    page.table.selectRow(0)
    assert page.selected().username == "admin"
    assert not page.active_button.isEnabled()  # can't deactivate yourself
