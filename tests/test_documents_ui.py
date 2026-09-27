import datetime as dt
from decimal import Decimal

import pytest

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocStatus, DocType, PersonKind
from caspian.services import documents as docs
from caspian.services import items, master
from caspian.services.documents import DocumentInput, LineInput
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext
from caspian.ui.documents_page import CancelDialog, DocumentDialog, DocumentsPage
from helpers import settle, wait_until


@pytest.fixture
async def env(themes, db, admin):
    u = {x.name: x.id for x in await master.list_units(db)}
    drill = await items.create_item(db, admin, ItemInput(
        "1001", "دریل بوش", u["عدد"], units=[(u["جعبه"], Decimal(24))],
        barcodes=[("111", None), ("222", u["جعبه"])]))
    await items.create_item(db, admin, ItemInput("1002", "دریل ماکیتا", u["عدد"]))
    pallet = await items.create_item(db, admin, ItemInput("2001", "پالت", u["عدد"],
                                                          is_returnable=True))
    person = await master.save_person(db, admin, "علی رضایی", PersonKind.EMPLOYEE)
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    return {"ctx": ctx, "u": u, "drill": drill, "pallet": pallet, "person": person}


async def make_dialog(env, doc_type, detail=None):
    db = env["ctx"].db
    loans = await docs.outstanding_loans(db, env["ctx"].actor)
    dlg = DocumentDialog(env["ctx"], doc_type, detail, await master.list_warehouses(db),
                         await master.search_persons(db), await master.list_units(db), loans)
    await dlg.load_lines()
    return dlg


async def scan(dlg, text):
    dlg.item_input.setText(text)
    await dlg.on_item_entered()


async def test_scanning_builds_lines(qtbot, env):
    dlg = await make_dialog(env, DocType.RECEIPT)
    qtbot.addWidget(dlg)
    await scan(dlg, "111")  # piece barcode
    await scan(dlg, "111")  # same barcode again -> qty 2
    await scan(dlg, "222")  # box barcode -> separate line in boxes
    assert [(ln.name, ln.unit.currentText(), ln.qty.value()) for ln in dlg.lines] == [
        ("دریل بوش", "عدد", Decimal(2)), ("دریل بوش", "جعبه", Decimal(1))]
    await scan(dlg, "1002")  # exact code
    assert dlg.lines[-1].name == "دریل ماکیتا"
    await scan(dlg, "zzz")
    assert "پیدا نشد" in dlg.status.text()
    assert dlg.item_input.text() == ""


async def test_save_and_post_receipt(qtbot, env):
    db = env["ctx"].db
    dlg = await make_dialog(env, DocType.RECEIPT)
    qtbot.addWidget(dlg)
    dlg.open()
    await scan(dlg, "222")
    dlg.lines[0].qty.setText("2")
    dlg.lines[0].price.setText("1500000")
    assert "۳٬۰۰۰٬۰۰۰" in dlg.totals.text()
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.posted and dlg.result() == dlg.DialogCode.Accepted
    assert (await docs.stock_by_warehouse(db, env["drill"]))[0][1] == Decimal(48)


async def test_insufficient_stock_keeps_draft(qtbot, env):
    db = env["ctx"].db
    dlg = await make_dialog(env, DocType.ISSUE)
    qtbot.addWidget(dlg)
    dlg.open()
    await scan(dlg, "111")
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.isVisible() and "کافی نیست" in dlg.status.text()
    [row] = await docs.list_documents(db, env["ctx"].actor)
    assert row.status is DocStatus.DRAFT  # work saved as a draft, not lost


async def test_posted_document_is_read_only(qtbot, env):
    db, actor = env["ctx"].db, env["ctx"].actor
    wh = (await master.list_warehouses(db))[0].id
    doc_id = await docs.create_and_post(db, actor, DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [LineInput(env["drill"], env["u"]["عدد"],
                                                         Decimal(3))]))
    dlg = await make_dialog(env, DocType.RECEIPT, await docs.get_document(db, actor, doc_id))
    qtbot.addWidget(dlg)
    assert dlg.submit_button.isHidden() and dlg.draft_button.isHidden()
    assert not dlg.item_input.isEnabled()
    assert not dlg.lines[0].qty.isEnabled()


async def test_loan_return_prefills_outstanding(qtbot, env):
    db, actor = env["ctx"].db, env["ctx"].actor
    wh = (await master.list_warehouses(db))[0].id
    await docs.create_and_post(db, actor, DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [LineInput(env["pallet"], env["u"]["عدد"],
                                                         Decimal(10))]))
    loan = await docs.create_and_post(db, actor, DocumentInput(
        DocType.LOAN_OUT, dt.date.today(), wh,
        [LineInput(env["pallet"], env["u"]["عدد"], Decimal(7))], person_id=env["person"]))
    dlg = await make_dialog(env, DocType.LOAN_RETURN)
    qtbot.addWidget(dlg)
    dlg.person.setCurrentIndex(dlg.person.findData(env["person"]))
    dlg.loan.setCurrentIndex(dlg.loan.findData(loan))
    await wait_until(lambda: len(dlg.lines) == 1)
    assert [(ln.name, ln.qty.value()) for ln in dlg.lines] == [("پالت", Decimal(7))]


async def test_documents_page_and_cancel(qtbot, env):
    db, actor = env["ctx"].db, env["ctx"].actor
    wh = (await master.list_warehouses(db))[0].id
    doc_id = await docs.create_and_post(db, actor, DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [LineInput(env["drill"], env["u"]["عدد"],
                                                         Decimal(3))]))
    page = DocumentsPage(env["ctx"])
    qtbot.addWidget(page)
    await page.documents.refresh()
    assert page.documents.table.rowCount() == 1
    page.documents.table.selectRow(0)
    assert page.documents.cancel_button.isEnabled()
    assert not page.documents.post_button.isEnabled()

    row = page.documents.selected()
    dlg = CancelDialog(env["ctx"], row)
    qtbot.addWidget(dlg)
    dlg.submit_button.click()
    await settle(dlg)
    assert "علت" in dlg.status.text()
    dlg.reason.setText("ثبت تکراری")
    dlg.submit_button.click()
    await settle(dlg)
    assert (await docs.get_document(db, actor, doc_id)).status is DocStatus.CANCELLED
