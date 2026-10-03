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
from caspian.ui.widgets import QtyEdit
from helpers import settle, wait_until


@pytest.fixture
def make_ctx(themes, db):
    def factory(actor):
        return AppContext(db, DbConfig(), Settings(), themes, actor)
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
    assert edit.text() == "۲۴" and edit.value() == Decimal(24)  # shown like the tables (#28)


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
    await wait_until(lambda: len(dlg.barcodes.rows) == 2)
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
    assert await wait_until(lambda: page.table.rowCount() == 1)
    assert page.table.rowCount() == 1 and page.table.item(0, 1).text() == "دریل بوش"
    page.search.clear()
    page.search.returnPressed.emit()
    page.low_only.setChecked(True)
    await wait_until(lambda: page.count_label.text() == "۱ کالا")
    await asyncio.sleep(0.4)  # the cleared search box's debounce fires a refresh too
    assert page.table.rowCount() == 1
    page.search.setText("zzz")
    page.search.returnPressed.emit()
    assert await wait_until(lambda: page.table.isHidden())
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


async def test_new_item_with_opening_stock_posts_an_opening_document(qtbot, db, admin, make_ctx):
    """«موجودی اولیه» on the item form becomes a posted OPENING document (#12)."""
    from caspian.db.models import DocType
    from caspian.services import documents as docs

    ctx = make_ctx(admin)
    warehouses = await master.list_warehouses(db)
    dlg = ItemDialog(ctx, await master.list_units(db), [], suggested_code="5001", warehouses=warehouses)
    qtbot.addWidget(dlg)
    dlg.name.setText("میز جلسه")
    dlg.opening_qty.setText("۴")
    dlg.opening_price.setText("2500000")
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.result() == dlg.DialogCode.Accepted and dlg.result_message == ""
    [(_wh, qty)] = await docs.stock_by_warehouse(db, dlg.saved_id)
    assert qty == Decimal(4)
    [opening] = await docs.list_documents(db, admin, DocType.OPENING)
    assert opening.status.value == "POSTED"


async def test_edit_form_has_no_opening_stock(qtbot, db, admin, make_ctx):
    ctx = make_ctx(admin)
    item_id = await items.create_item(db, admin, ItemInput("5002", "صندلی", (await _units(db))["عدد"]))
    dlg = ItemDialog(ctx, await master.list_units(db), [], await items.get_item(db, admin, item_id),
                     warehouses=await master.list_warehouses(db))
    qtbot.addWidget(dlg)
    assert dlg.opening_qty not in dlg._inputs and dlg.collect_opening() is None


async def test_code_field_locked_after_first_document(qtbot, db, admin, make_ctx):
    import datetime as dt

    from caspian.db.models import DocType
    from caspian.services import documents as docs

    ctx = make_ctx(admin)
    u = await _units(db)
    item_id = await items.create_item(db, admin, ItemInput("6001", "کمد", u["عدد"]))
    wh = (await master.list_warehouses(db))[0].id
    await docs.create_document(db, admin, docs.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [docs.LineInput(item_id, u["عدد"], Decimal(1))]))
    dlg = ItemDialog(ctx, await master.list_units(db), [], await items.get_item(db, admin, item_id))
    qtbot.addWidget(dlg)
    assert dlg.code.isReadOnly() and "قابل تغییر نیست" in dlg.code.toolTip()


async def test_unit_dialog_sets_decimal_rule(qtbot, db, admin, make_ctx):
    from caspian.ui.master_page import UnitDialog

    ctx = make_ctx(admin)
    dlg = UnitDialog(ctx)
    qtbot.addWidget(dlg)
    dlg.name.setText("شاخه")
    dlg.allow_decimal.setChecked(False)
    dlg.submit_button.click()
    await settle(dlg)
    [unit] = [x for x in await master.list_units(db) if x.name == "شاخه"]
    assert unit.allow_decimal is False
