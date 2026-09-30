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
from caspian.ui.theme import DARK, LIGHT

ADMIN_PW = "Str0ngPass"


@pytest.fixture
async def admin(db):
    actor = (await auth.login(db, "admin", "admin")).actor
    await auth.change_password(db, actor, "admin", ADMIN_PW)
    await auth.set_pin(db, actor, ADMIN_PW, "4826")
    return actor


@pytest.fixture
def make_window(qtbot, themes, tmp_path, monkeypatch, db):
    monkeypatch.setattr("caspian.core.settings.settings_path", lambda: tmp_path / "settings.json")

    def factory(actor):
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


def test_dashboard_cards_navigate(window):
    dashboard = window._pages["dashboard"]
    dashboard.cards["low_stock"].clicked.emit()
    assert window.current_page == "items"
    assert window._pages["items"].low_only.isChecked()
    window.navigate("dashboard")
    dashboard.cards["loans"].clicked.emit()
    documents = window._pages["documents"]
    assert window.current_page == "documents" and documents.tabs.currentWidget() is documents.loans


def test_about_dialog(qtbot):
    from PySide6.QtWidgets import QLabel

    from caspian import __version__
    from caspian.ui.about import AboutDialog

    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert __version__ in text and "Vazirmatn" in text and "MariaDB" in text


async def test_admin_menu_offers_user_management(make_window, db, admin):
    """Clicking «مدیر سیستم» opens profile / password / users / new user / logout (#15)."""
    win = make_window(admin)
    texts = [a.text() for a in win._user_menu.actions() if a.text()]
    for expected in ("پروفایل من", "تغییر رمز عبور", "مدیریت کاربران", "کاربر جدید", "خروج از حساب"):
        assert expected in texts, expected
    assert win._users_action.isVisible() and win._new_user_action.isVisible()
    win._users_action.trigger()
    assert win.current_page == "users"

    await users.create_user(db, admin, "neda", "ندا", "Neda#2026", "viewer")
    win.ctx.set_actor((await auth.login(db, "neda", "Neda#2026")).actor)
    assert not win._users_action.isVisible() and not win._new_user_action.isVisible()


async def test_profile_dialog_shows_the_signed_in_user(qtbot, themes, db, admin):
    from caspian.ui.main_window import ProfileDialog

    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    dlg = ProfileDialog(ctx)
    qtbot.addWidget(dlg)
    labels = [dlg.form.itemAt(i).widget().text() for i in range(dlg.form.count())
              if dlg.form.itemAt(i).widget() is not None]
    assert "admin" in labels and "مدیر سیستم" in labels


async def test_rename_user_from_users_page(qtbot, window, db, admin):
    from caspian.ui.users_page import EditNameDialog, NewUserDialog
    from helpers import settle, wait_until

    await users.create_user(db, admin, "neda", "ندا", "Neda#2026", "viewer")
    [neda] = [u for u in await users.list_users(db, admin) if u.username == "neda"]
    dlg = EditNameDialog(window.ctx, neda)
    qtbot.addWidget(dlg)
    dlg.username.setText("neda.r")
    dlg.submit_button.click()
    await settle(dlg)
    assert (await auth.login(db, "neda.r", "Neda#2026")).actor.user_id == neda.id

    [me] = [u for u in await users.list_users(db, admin) if u.username == "admin"]
    dlg = EditNameDialog(window.ctx, me)
    qtbot.addWidget(dlg)
    dlg.username.setText("boss")
    dlg.submit_button.click()
    await settle(dlg)
    assert window.ctx.actor.username == "boss"  # header follows a rename of yourself

    new = NewUserDialog(window.ctx, await users.list_roles(db))
    qtbot.addWidget(new)
    new.username.setText("NEDA.R")
    await new.check_username()
    assert await wait_until(lambda: new.status.text() == users.TAKEN)


async def test_dashboard_drafts_card_opens_the_drafts(window, db, admin):
    """#25: one source for value and hint; the card leads to the draft documents."""
    import datetime as dt
    from decimal import Decimal

    from caspian.db.models import DocStatus, DocType
    from caspian.services import documents as docs
    from caspian.services import items, master
    from caspian.services.items import ItemInput
    from helpers import wait_until

    unit = (await master.list_units(db))[0].id
    item = await items.create_item(db, admin, ItemInput("1", "x", unit))
    wh = (await master.list_warehouses(db))[0].id
    await docs.create_document(db, admin, docs.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [docs.LineInput(item, unit, Decimal(1))]))
    dash = window._pages["dashboard"]
    await dash.refresh()
    card = dash.cards["drafts"]
    assert card.value.text() == "۱" and card.hint.text() == "۱ سند، ۰ ورود اطلاعات"
    card.clicked.emit()
    assert await wait_until(lambda: window.current_page == "documents")
    lst = window._pages["documents"].documents
    assert lst.status_filter.currentData() == DocStatus.DRAFT


async def test_low_stock_filter_resets_after_leaving(window):
    """#30: the filter the dashboard switched on must not stick."""
    window.open_from_dashboard("items", "low_stock")
    items_page = window._pages["items"]
    assert items_page.low_only.isChecked()
    window.navigate("dashboard")
    assert not items_page.low_only.isChecked()
