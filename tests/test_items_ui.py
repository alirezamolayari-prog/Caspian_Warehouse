import asyncio
from decimal import Decimal

import pytest
from PySide6.QtCore import Qt

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.services import auth, items, master, users
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext
from caspian.ui.items_page import ItemDialog, ItemsPage
from caspian.ui.master_page import MasterDataPage, WarehouseDialog
from caspian.ui.theme import ThemeManager
from caspian.ui.widgets import QtyEdit


async def settle(dialog):
    for _ in range(50):
        await asyncio.sleep(0.02)
        if dialog.submit_button.isEnabled():
            break


@pytest.fixture
def make_ctx(qapp, db):
    def factory(actor):
        return AppContext(db, DbConfig(), Settings(), ThemeManager(qapp, "light"), actor)
    return factory


async def _units(db):
    return {u.name: u.id for u in await master.list_units(db)}


def test_qty_edit_accepts_persian_digits(qtbot):
    edit = QtyEdit()
    qtbot.addWidget(edit)
    # QTest can't synthesize Persian keystrokes; insert() still runs the validator.
    edit.insert("۱۲٫۵")
    assert edit.value() == Decimal("12.5")
    edit.clear()
    edit.insert("12")
    edit.insert("abc")
    assert edit.text() == "12"  # letters rejected by the validator
    edit.set_value(Decimal("24.0000"))
    assert edit.text() == "24"


async def test_item_dialog_creates_item(qtbot, db, admin, make_ctx):
    ctx = make_ctx(admin)
    units = await master.list_units(db)
    u = await _units(db)
    dlg = ItemDialog(ctx, units, [], suggested_code="1001")
    qtbot.addWidget(dlg)
    assert dlg.code.text() == "1001"
    dlg.name.setText("دریل بوش")
    dlg.reorder_point.setText("۱۰")
    combo, _eq, qty, _rm = dlg.alt_units.add_row()
    combo.setCurrentIndex(combo.findData(u["جعبه"]))
    qty.setText("24")
    edit = dlg.barcodes.add_row()[0]
    edit.setText("6260001")
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.result() == dlg.DialogCode.Accepted
    detail = await items.get_item(db, admin, dlg.saved_id)
    assert detail.input.reorder_point == Decimal(10)
    assert detail.input.units == [(u["جعبه"], Decimal(24))]
    assert detail.input.barcodes == [("6260001", None)]


async def test_item_dialog_shows_service_errors(qtbot, db, admin, make_ctx):
    ctx = make_ctx(admin)
    units = await master.list_units(db)
    dlg = ItemDialog(ctx, units, [], suggested_code="1")
    qtbot.addWidget(dlg)
    dlg.open()
    dlg.submit_button.click()  # empty name
    await settle(dlg)
    assert dlg.isVisible() and "نام کالا" in dlg.status.text()


async def test_barcode_enter_adds_row_instead_of_saving(qtbot, db, admin, make_ctx):
    ctx = make_ctx(admin)
    dlg = ItemDialog(ctx, await master.list_units(db), [], suggested_code="1")
    qtbot.addWidget(dlg)
    dlg.show()
    edit = dlg.barcodes.add_row()[0]
    edit.setFocus()
    qtbot.keyClicks(edit, "111")
    qtbot.keyClick(edit, Qt.Key.Key_Return)
    await asyncio.sleep(0.05)
    assert dlg.isVisible()
    assert len(dlg.barcodes.rows) == 2


async def test_base_unit_locked_when_item_has_movements(qtbot, db, admin, make_ctx, monkeypatch):
    ctx = make_ctx(admin)
    u = await _units(db)
    item_id = await items.create_item(db, admin, ItemInput("1", "x", u["عدد"]))
    detail = await items.get_item(db, admin, item_id)
    object.__setattr__(detail, "has_movements", True)
    dlg = ItemDialog(ctx, await master.list_units(db), [], detail)
    qtbot.addWidget(dlg)
    assert not dlg.base_unit.isEnabled()


async def test_items_page_lists_and_filters(qtbot, db, admin, make_ctx):
    u = await _units(db)
    await items.create_item(db, admin, ItemInput("1001", "دریل بوش", u["عدد"],
                                                 reorder_point=Decimal(3)))
    await items.create_item(db, admin, ItemInput("1002", "میز کار", u["عدد"]))
    page = ItemsPage(make_ctx(admin))
    qtbot.addWidget(page)
    await page.reload_all()
    assert page.table.rowCount() == 2
    assert page.count_label.text() == "۲ کالا"
    page.search.setText("دريل")
    page.search.returnPressed.emit()
    await asyncio.sleep(0.1)
    assert page.table.rowCount() == 1 and page.table.item(0, 1).text() == "دریل بوش"
    page.search.clear()
    page.low_only.setChecked(True)
    await asyncio.sleep(0.1)
    assert page.table.rowCount() == 1
    page.search.setText("zzz")
    page.search.returnPressed.emit()
    await asyncio.sleep(0.1)
    assert page.table.isHidden() and not page.empty.isHidden()


async def test_viewer_has_no_edit_buttons(qtbot, db, admin, make_ctx):
    await users.create_user(db, admin, "neda", "", "Neda#2026", "viewer")
    viewer = (await auth.login(db, "neda", "Neda#2026")).actor
    page = ItemsPage(make_ctx(viewer))
    qtbot.addWidget(page)
    for button in (page.new_button, page.edit_button, page.active_button, page.delete_button):
        assert button.isHidden()


async def test_master_page_tabs(qtbot, db, admin, make_ctx):
    ctx = make_ctx(admin)
    page = MasterDataPage(ctx)
    qtbot.addWidget(page)
    for tab in (page.persons, page.warehouses, page.categories, page.units):
        await tab.refresh()
    assert page.warehouses.table.rowCount() == 1
    assert page.units.table.rowCount() >= 10

    dlg = WarehouseDialog(ctx)
    qtbot.addWidget(dlg)
    dlg.code.setText("02")
    dlg.name.setText("انبار دوم")
    dlg.submit_button.click()
    await settle(dlg)
    await page.warehouses.refresh()
    assert page.warehouses.table.rowCount() == 2
