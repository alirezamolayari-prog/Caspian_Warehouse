"""Bug list round 1 #2: the row numbers beside the line tables use Persian digits («۱», not «1»),
matching the «ردیف ۱» of the error messages, and stay right after a line is removed."""

from decimal import Decimal

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocType
from caspian.services import items, master
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext
from caspian.ui.documents_page import DocumentDialog
from caspian.ui.requests_page import RequestDialog


def _numbers(table) -> list[str]:
    return [table.verticalHeaderItem(r).text() if table.verticalHeaderItem(r) else str(r + 1)
            for r in range(table.rowCount())]


async def _two_items(db, admin):
    unit = (await master.list_units(db))[0].id
    a = await items.create_item(db, admin, ItemInput("2001", "لامپ", unit))
    b = await items.create_item(db, admin, ItemInput("2002", "سیم", unit))
    return unit, a, b


async def test_document_editor_rows_are_persian(qtbot, themes, db, admin):
    unit, a, b = await _two_items(db, admin)
    dialog = DocumentDialog(AppContext(db, DbConfig(), Settings(), themes, admin), DocType.RECEIPT, None,
                            await master.list_warehouses(db), [],
                            await master.list_units(db, include_inactive=True), [])
    qtbot.addWidget(dialog)
    for item in (a, b):
        await dialog._ensure_units(item)
        dialog._add_line(item, "x", "x", unit, Decimal(1))
    assert _numbers(dialog.table) == ["۱", "۲"]
    dialog._remove_line(dialog.lines[0])
    assert _numbers(dialog.table) == ["۱"]


async def test_request_dialog_rows_are_persian(qtbot, themes, db, admin):
    _unit, a, b = await _two_items(db, admin)
    dialog = RequestDialog(AppContext(db, DbConfig(), Settings(), themes, admin), [],
                           await master.list_warehouses(db),
                           await master.list_units(db, include_inactive=True))
    qtbot.addWidget(dialog)
    await dialog.add_item(a, "2001", "لامپ")
    await dialog.add_item(b, "2002", "سیم")
    assert _numbers(dialog.table) == ["۱", "۲"]
    dialog._remove_line(dialog.lines[0])
    assert _numbers(dialog.table) == ["۱"]
