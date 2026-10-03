"""QA round 1, Phase 3: polish (#18 errors, #19 digits, #20 bidi, #21 menu, #22 backups, #23 toast)."""

import pytest
from PySide6.QtCore import Qt

from caspian.core.settings import Settings
from caspian.core.text import ltr
from caspian.db.database import DbConfig
from caspian.services import backup, items, master
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext
from helpers import settle, wait_until


@pytest.fixture
async def ctx(themes, db, admin):
    return AppContext(db, DbConfig(), Settings(), themes, admin)


async def test_error_clears_on_edit_and_keeps_its_place(qtbot, ctx):
    """«کد 1006 قبلاً…» stayed after fixing the field and the Save button jumped (#18)."""
    from caspian.ui.items_page import ItemDialog

    units = await master.list_units(ctx.db)
    await items.create_item(ctx.db, ctx.actor, ItemInput("1006", "جارو", units[0].id))
    dlg = ItemDialog(ctx, units, [], suggested_code="1006")
    qtbot.addWidget(dlg)
    dlg.show()
    assert dlg.status.sizePolicy().retainSizeWhenHidden()
    dlg.name.setText("سطل")
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.status.isVisible() and "1006" in dlg.status.text()
    qtbot.keyClick(dlg.code, Qt.Key.Key_Backspace)  # the user edits the field
    assert not dlg.status.isVisible()


async def test_codes_and_notes_use_persian_digits(qtbot, ctx):
    """Latin digits next to Persian ones: codes in tables, import notes (#19)."""
    from caspian.ui.items_page import ItemsPage

    unit = (await master.list_units(ctx.db))[0].id
    await items.create_item(ctx.db, ctx.actor, ItemInput("1006", "جارو", unit))
    page = ItemsPage(ctx)
    qtbot.addWidget(page)
    await page.refresh()
    assert page.table.item(0, 0).text() == "۱۰۰۶"
    assert (await items.search_items(ctx.db, ctx.actor, "1006"))[0].code == "1006"  # stored as is


def test_ltr_isolates_and_window_titles_are_rtl(qtbot, ctx):
    """««alireza» تغییر نقش» (#20)."""
    from caspian.services.users import UserRow
    from caspian.ui.users_page import ChangeRoleDialog

    assert ltr("alireza") == "⁦alireza⁩" and ltr("") == ""
    row = UserRow(1, "alireza", "علیرضا", "admin", "مدیر سیستم", True, True, None)
    dlg = ChangeRoleDialog(ctx, row, [])
    qtbot.addWidget(dlg)
    assert dlg.windowTitle().startswith("‏تغییر نقش «⁦alireza⁩»")


async def test_new_document_menu_opens_inside_the_window(qtbot, ctx):
    """The «سند جدید» menu opened outside the window on the left (#21)."""
    from caspian.ui.documents_page import DocumentsPage

    page = DocumentsPage(ctx)
    qtbot.addWidget(page)
    page.resize(900, 600)
    page.show()
    lst = page.documents
    lst.new_button.click()
    assert await wait_until(lambda: lst.new_menu.isVisible())
    menu, window = lst.new_menu.geometry(), page.window().geometry()
    assert window.left() <= menu.left() and menu.right() <= window.right()
    lst.new_menu.close()


def test_backup_sizes_and_file_names():
    """«۰٫۰ MB» for small backups; Persian labels in file names (#22)."""
    assert backup.format_size(12_000) == "۱۲ KB"
    assert backup.format_size(300) == "۱ KB"
    assert backup.format_size(3_400_000) == "۳٫۲ MB"


async def test_backup_file_name_is_ascii_and_label_kept(db, admin, tmp_path):
    from sqlalchemy.engine import make_url

    dumper = backup.SqliteDumper(make_url(str(db.url)).database)
    first = await backup.create_backup(db, admin, dumper, None, tmp_path, "دستی")
    second = await backup.create_backup(db, admin, dumper, None, tmp_path, "دستی")
    for info in (first, second):
        assert info.name.isascii() and info.label == "دستی" and backup.FILE_PATTERN.match(info.name)
    assert first.name != second.name


async def test_saving_a_new_item_shows_a_toast(qtbot, ctx, monkeypatch):
    """No feedback after saving a new item (#23)."""
    from caspian.ui import items_page
    from caspian.ui.widgets import Toast

    page = items_page.ItemsPage(ctx)
    qtbot.addWidget(page)
    page.resize(800, 500)
    page.show()

    async def accept(dialog):
        dialog.name.setText("سطل")
        await dialog.submit()
        return 1

    monkeypatch.setattr(items_page, "exec_dialog", accept)
    await page._open_editor(None)
    toasts = page.findChildren(Toast)
    assert toasts and "سطل" in toasts[0].text() and toasts[0].isVisible()
