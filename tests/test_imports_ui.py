from decimal import Decimal

import pytest

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import BatchStatus, DocType, ImportSource, LineStatus, Resolution
from caspian.services import documents as docs
from caspian.services import imports, items, master
from caspian.services.import_files import TableData
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext
from caspian.ui.imports_page import FileImportDialog, ImportsPage, ReviewDialog, ScanDialog
from helpers import settle, wait_until


@pytest.fixture
async def env(themes, db, admin):
    u = {x.name: x.id for x in await master.list_units(db)}
    await items.create_item(db, admin, ItemInput("1001", "دریل بوش", u["عدد"],
                                                 barcodes=[("111", None)]))
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    return ctx, await master.list_warehouses(db), await master.search_persons(db)


async def test_file_dialog_mapping_and_batch(qtbot, env):
    ctx, warehouses, persons = env
    dlg = FileImportDialog(ctx, warehouses, persons)
    qtbot.addWidget(dlg)
    dlg.load_table(TableData(["نام کالا", "تعداد", "یادداشت"],
                             [["دریل بوش", "3", "a"], ["فرز", "2", "b"]], ImportSource.EXCEL,
                             "f.xlsx"))
    assert [c.currentData() for c in dlg.mapping_combos] == ["name", "qty", None]
    dlg.mapping_combos[2].setCurrentIndex(dlg.mapping_combos[2].findData("name"))
    dlg.submit_button.click()
    await settle(dlg)
    assert "دو ستون" in dlg.status.text()
    dlg.mapping_combos[2].setCurrentIndex(0)
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.batch_id is not None
    detail = await imports.get_batch(ctx.db, ctx.actor, dlg.batch_id)
    assert detail.doc_type == DocType.RECEIPT
    assert [ln.status for ln in detail.lines] == [LineStatus.EXISTING_MATCH, LineStatus.NEW]


async def test_scan_dialog_accumulates(qtbot, env):
    ctx, warehouses, persons = env
    dlg = ScanDialog(ctx, warehouses, persons)
    qtbot.addWidget(dlg)
    for code in ("111", "۱۱۱", "999"):
        dlg.scan.setText(code)
        await dlg.on_scan()
    assert dlg.counts["111"][0] == Decimal(2) and dlg.counts["111"][1] == "دریل بوش"
    assert dlg.counts["999"][1] == "— ناشناخته —"
    dlg.submit_button.click()
    await settle(dlg)
    lines = (await imports.get_batch(ctx.db, ctx.actor, dlg.batch_id)).lines
    assert [(ln.barcode, ln.qty, ln.status) for ln in lines] == [
        ("111", Decimal(2), LineStatus.EXISTING_MATCH), ("999", Decimal(1), LineStatus.NEW)]


async def test_review_resolve_and_apply(qtbot, env):
    ctx, warehouses, _ = env
    from caspian.services.import_files import RawRow

    unit = (await master.list_units(ctx.db))[0].id
    await items.create_item(ctx.db, ctx.actor, ItemInput("1002", "دریل ماکیتا", unit))
    # "دریل" alone is ambiguous between the two drills -> conflict the user must resolve.
    batch = await imports.create_batch(
        ctx.db, ctx.actor, imports.ImportKind.STOCK, ImportSource.EXCEL,
        [RawRow(name="دریل بوش", qty=Decimal(2)), RawRow(name="دریل", qty=Decimal(1))],
        "فاکتور", DocType.RECEIPT, warehouses[0].id)
    dlg = ReviewDialog(ctx, batch)
    qtbot.addWidget(dlg)
    await dlg.reload()
    assert "نیازمند تصمیم: ۱" in dlg.summary.text()
    combo = dlg.table.cellWidget(1, 8)
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert any(t.startswith("استفاده از: دریل بوش") for t in labels)
    combo.setCurrentIndex(next(i for i, t in enumerate(labels) if t.startswith("استفاده از")))
    await wait_until(lambda: "نیازمند تصمیم: ۰" in dlg.summary.text())
    assert "نیازمند تصمیم: ۰" in dlg.summary.text(), dlg.status.text()
    dlg.submit_button.click()
    await settle(dlg)
    assert "پیش‌نویس" in dlg.result_message
    detail = await imports.get_batch(ctx.db, ctx.actor, batch)
    assert detail.row.status == BatchStatus.APPLIED
    assert all(ln.resolution == Resolution.MATCH for ln in detail.lines)
    doc = await docs.get_document(ctx.db, ctx.actor, detail.result_document_id)
    assert sum(ln.base_qty for ln in doc.lines) == Decimal(3)


async def test_imports_page_lists_batches(qtbot, env):
    ctx, _warehouses, _ = env
    from caspian.services.import_files import RawRow

    await imports.create_batch(ctx.db, ctx.actor, imports.ImportKind.ITEMS, ImportSource.CSV,
                               [RawRow(name="کمد")], "کالاهای جدید")
    page = ImportsPage(ctx)
    qtbot.addWidget(page)
    await page.refresh()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == "کالاهای جدید"
