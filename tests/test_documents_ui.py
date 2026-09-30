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


async def test_post_during_stocktake_asks_for_admin_pin(qtbot, env, monkeypatch):
    """A freeze error offers an admin override; with the PIN the document is posted (#7)."""
    from caspian.services import protected
    from caspian.services import stocktake as st
    from caspian.services.protected import ProtectedAction
    from caspian.ui import documents_page
    from conftest import ADMIN_PIN

    ctx = env["ctx"]
    wh = (await master.list_warehouses(ctx.db))[0].id
    draft = await docs.create_document(ctx.db, ctx.actor, DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [LineInput(env["drill"], env["u"]["عدد"], Decimal(1))]))
    await st.create_stocktake(ctx.db, ctx.actor, wh)
    asked = []

    async def fake_request(db, actor, action, description, parent=None):
        asked.append(description)
        return await protected.approve(db, actor, ProtectedAction.STOCKTAKE_OVERRIDE, "admin", ADMIN_PIN)

    monkeypatch.setattr(documents_page, "request_approval", fake_request)
    page = DocumentsPage(ctx)
    qtbot.addWidget(page)
    await page.documents.refresh()
    page.documents.table.select_id(draft)
    await page.documents.on_post()
    assert asked and "انبارگردانی" in asked[0]
    assert (await docs.get_document(ctx.db, ctx.actor, draft)).status is DocStatus.POSTED


async def test_issue_form_mentions_stock_waiting_in_a_draft(qtbot, env):
    """«موجودی: ۰» alone made users think the import failed (#9)."""
    ctx = env["ctx"]
    wh = (await master.list_warehouses(ctx.db))[0].id
    await docs.create_document(ctx.db, ctx.actor, DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [LineInput(env["drill"], env["u"]["عدد"], Decimal(5))]))
    dlg = await make_dialog(env, DocType.ISSUE)
    qtbot.addWidget(dlg)
    await scan(dlg, "111")
    assert "۵ عدد در پیش‌نویس ر-۱ منتظر ثبت نهایی است" in dlg.stock_hint.text()


async def test_add_new_person_from_the_picker(qtbot, env):
    """«+ افزودن شخص جدید» creates the person and selects it (#10)."""
    import asyncio

    from PySide6.QtWidgets import QApplication

    from caspian.ui.master_page import PersonDialog

    dlg = await make_dialog(env, DocType.ISSUE)
    qtbot.addWidget(dlg)
    dlg.person.lineEdit().setText("كامران")  # typed with Arabic letters
    task = asyncio.ensure_future(dlg.person.run_add("كامران"))

    def person_dialog():
        return next((w for w in QApplication.topLevelWidgets()
                     if isinstance(w, PersonDialog) and w.isVisible()), None)

    assert await wait_until(lambda: person_dialog() is not None)
    form = person_dialog()
    assert form.name.text() == "كامران"
    form.submit_button.click()
    new_id = await task
    assert new_id is not None and dlg.person.currentData() == new_id
    assert dlg.person.currentText().startswith("كامران")  # stored as typed; search is normalized
