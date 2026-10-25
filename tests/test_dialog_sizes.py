"""Bug list round 1 #1: a dialog opens at least as large as its contents need (the document editor
opened 620 px high for 701 px of contents: the line counter sat on the table)."""

from decimal import Decimal

from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocType
from caspian.services import items, master
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.documents_page import DocumentDialog
from caspian.ui.tasks import spawn
from helpers import wait_until


async def test_document_editor_opens_without_overlap(qtbot, themes, db, admin):
    unit = (await master.list_units(db))[0].id
    lamp = await items.create_item(db, admin, ItemInput("2001", "لامپ", unit))
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    dialog = DocumentDialog(ctx, DocType.ISSUE, None, await master.list_warehouses(db), [],
                            await master.list_units(db, include_inactive=True), [])
    qtbot.addWidget(dialog)
    await dialog.load_lines()
    await dialog._ensure_units(lamp)
    dialog._add_line(lamp, "2001", "لامپ", unit, Decimal(3))
    spawn(exec_dialog(dialog))
    assert await wait_until(dialog.isVisible)
    assert dialog.height() >= dialog.minimumSizeHint().height()
    assert dialog.totals.geometry().top() >= dialog.table.geometry().bottom()  # counter below the table
    assert dialog.submit_button.geometry().top() > dialog.totals.geometry().bottom()
    dialog.dirty = False
    dialog.reject()


async def test_any_dialog_opens_at_least_its_content_size(qtbot):
    dialog = QDialog()
    dialog.setMinimumSize(300, 100)  # smaller than what it holds
    layout = QVBoxLayout(dialog)
    for n in range(15):
        layout.addWidget(QLabel(f"سطر {n}"))
    qtbot.addWidget(dialog)
    spawn(exec_dialog(dialog))
    assert await wait_until(dialog.isVisible)
    assert dialog.height() >= dialog.minimumSizeHint().height()
    dialog.reject()
