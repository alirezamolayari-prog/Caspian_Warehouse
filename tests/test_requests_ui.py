"""«درخواست کالا» on screen: the list and statuses, the new-request dialog (one request per dialog,
however often it is clicked), notifications and badge, chat opening at the newest message, the
connection tab, and printing an approved voucher (which posts it)."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocStatus, Document, GoodsRequest, RequestStatus
from caspian.services import auth, documents, master, users
from caspian.services.server_link import goods
from caspian.services.server_link.sync import SyncRunner, SyncState, TickResult
from caspian.ui import document_print, messages
from caspian.ui.app_context import AppContext
from caspian.ui.documents_page import DocumentDialog
from caspian.ui.main_window import MainWindow
from caspian.ui.requests_page import RequestChatDialog, RequestDialog, RequestsPage
from caspian.ui.server_status import ServerLink
from caspian.ui.settings_page import SettingsPage
from caspian.ui.widgets import Toast
from helpers import wait_until
from request_helpers import request_input, stock


def _ctx(themes, linked, actor, with_link=True) -> AppContext:
    ctx = AppContext(linked["db"], DbConfig(), Settings(), themes, actor)
    if with_link:
        ctx.server_link = ServerLink(SyncRunner(linked["engine"]))  # never started: ticks are manual
    return ctx


async def _count(db) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(GoodsRequest))


async def _dialog(ctx, linked) -> RequestDialog:
    db = linked["db"]
    dialog = RequestDialog(ctx, await master.search_persons(db), await master.list_warehouses(db),
                           await master.list_units(db, include_inactive=True))
    await dialog.load()
    return dialog


async def test_list_shows_every_status(qtbot, themes, linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    for qty in (1, 2, 3):
        await goods.create_request(db, admin, request_input(linked, qty=qty))
    await engine.tick(admin)
    server.approve(1)
    server.reject(2, "زیاد است")
    await engine.tick(admin)
    await goods.create_request(db, admin, request_input(linked, qty=4))  # still in the queue
    page = RequestsPage(_ctx(themes, linked, admin))
    qtbot.addWidget(page)
    page.show()
    await page.refresh()
    statuses = {page.table.item(r, 4).text() for r in range(page.table.rowCount())}
    assert statuses == {"در صف ارسال", "● تأیید شد", "● رد شد", "در انتظار تأیید مالک"}
    notes = {page.table.item(r, 6).text() for r in range(page.table.rowCount())}
    assert "دلیل رد: زیاد است" in notes and "آماده چاپ حواله" in notes


async def test_new_request_dialog_makes_one_request(qtbot, themes, linked, admin):
    db = linked["db"]
    dialog = await _dialog(_ctx(themes, linked, admin), linked)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.person.select_value(linked["person"])
    dialog.place.set_value(linked["location"].id)
    await dialog.add_item(linked["bags"], "1024", "کیسه زباله")
    await dialog.add_item(linked["bags"], "1024", "کیسه زباله")  # same item again: quantity + 1
    assert len(dialog.lines) == 1 and dialog.lines[0].qty.value() == 2
    dialog.lines[0].qty.set_value(Decimal(20))
    dialog.submit_button.click()
    dialog.submit_button.click()  # an impatient second click
    assert await wait_until(lambda: dialog.result() == 1 and not dialog.isVisible())
    assert await _count(db) == 1
    [row] = await goods.list_requests(db, admin)
    assert row.status == RequestStatus.QUEUED and row.lines[0].qty == 20
    assert not dialog.submit_button.isEnabled()


async def test_dialog_lists_every_problem_and_saves_nothing(qtbot, themes, linked, admin):
    dialog = await _dialog(_ctx(themes, linked, admin), linked)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.person.select_value(None)
    dialog.submit_button.click()
    assert await wait_until(lambda: dialog.status.isVisible())
    text = dialog.status.text()
    assert "درخواست‌کننده" in text and "محل مصرف" in text and "کالا" in text
    assert dialog.isVisible() and await _count(linked["db"]) == 0


async def test_decision_notifies_and_badges(qtbot, themes, linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    ctx = _ctx(themes, linked, admin)
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.show()
    await goods.create_request(db, admin, request_input(linked))
    ctx.server_link._on_result(await engine.tick(admin))
    assert window.server_status.text() == "سرور: متصل" and window.server_status.isVisible()
    server.approve(1)
    result = await engine.tick(admin)
    ctx.server_link._on_result(result)
    assert await wait_until(lambda: window._nav_buttons["requests"].text() == "درخواست کالا (۱)")
    assert any("تأیید شد" in toast.text() for toast in window.findChildren(Toast))
    ctx.server_link._on_result(TickResult(SyncState.DISCONNECTED, "سرور در دسترس نیست"))
    assert window.server_status.text() == "سرور: قطع (آفلاین)"
    # Opening the request clears the badge.
    window.navigate("requests")
    page = window._pages["requests"]
    await page.refresh()
    page.table.selectRow(0)
    dialog = RequestChatDialog(ctx, page.selected(), page)
    await dialog.reload()
    await page.refresh()
    assert await wait_until(lambda: window._nav_buttons["requests"].text() == "درخواست کالا")


async def test_chat_opens_at_the_latest_message(qtbot, themes, linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await goods.create_request(db, admin, request_input(linked))
    await engine.tick(admin)
    for n in range(30):
        server.owner_message(1, f"پیام شماره {n}")
    await engine.tick(admin)
    ctx = _ctx(themes, linked, admin)
    [row] = await goods.list_requests(db, admin)
    dialog = RequestChatDialog(ctx, row)
    qtbot.addWidget(dialog)
    dialog.show()
    await dialog.reload()
    bar = dialog.transcript.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == bar.maximum()
    assert "مالک" in dialog.transcript.toPlainText() and "مالک ۱" not in dialog.transcript.toPlainText()
    dialog.input.setText("ok")
    dialog.send_button.click()
    assert await wait_until(lambda: "در صف ارسال" in dialog.transcript.toPlainText())
    assert (await goods.list_messages(db, admin, request_id))[-1].text == "ok"


async def _approved_draft(linked, admin) -> int:
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    await goods.create_request(db, admin, request_input(linked))
    await engine.tick(admin)
    server.approve(1)
    await engine.tick(admin)
    async with db.session() as s:
        return await s.scalar(select(Document.id).where(Document.server_request_id == 1))


async def test_printing_an_approved_voucher_posts_it(qtbot, themes, linked, admin, monkeypatch):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    doc_id = await _approved_draft(linked, admin)
    ctx = _ctx(themes, linked, admin)
    asked = []

    async def confirm(parent, text, *args, **kwargs):
        asked.append(text)
        return len(asked) > 1  # «انصراف» the first time, «ثبت نهایی و چاپ» the second

    async def printed(parent, html_text, paper):
        return True

    monkeypatch.setattr(messages, "confirm", confirm)
    monkeypatch.setattr(document_print, "preview_and_print", printed)
    assert not await document_print.print_document(None, ctx, doc_id)
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.DRAFT
    assert "ثبت نهایی" in asked[0] and await stock(db, linked["bags"]) == 50

    assert await document_print.print_document(None, ctx, doc_id)
    detail = await documents.get_document(db, admin, doc_id)
    assert detail.status == DocStatus.POSTED and await stock(db, linked["bags"]) == 30
    sheet = await documents.print_sheet(db, admin, doc_id)
    assert sheet.print_count == 1  # the original; reprints are stamped as copies
    await engine.tick(admin)
    assert server.requests[1]["status"] == "issued"


async def test_voucher_of_pending_request_is_not_printed(qtbot, themes, linked, admin, monkeypatch):
    db = linked["db"]
    await goods.create_request(db, admin, request_input(linked))
    await linked["engine"].tick(admin)
    doc_id = await documents.create_document(db, admin, documents.DocumentInput(
        documents.DocType.ISSUE, documents.dt.date.today(), linked["warehouse"],
        [documents.LineInput(linked["bags"], linked["unit"], Decimal(20))], person_id=linked["person"],
        server_request_id=1))
    errors = []
    monkeypatch.setattr(document_print, "show_error", lambda parent, text: errors.append(text))
    assert not await document_print.print_document(None, _ctx(themes, linked, admin), doc_id)
    assert errors and "تأیید مالک" in errors[0]


async def test_linked_voucher_editor_is_locked(qtbot, themes, linked, admin):
    db = linked["db"]
    doc_id = await _approved_draft(linked, admin)
    detail = await documents.get_document(db, admin, doc_id)
    dialog = DocumentDialog(_ctx(themes, linked, admin), detail.input.doc_type, detail,
                            await master.list_warehouses(db), await master.search_persons(db),
                            await master.list_units(db, include_inactive=True), [])
    qtbot.addWidget(dialog)
    await dialog.load_lines()
    assert not dialog.person.isEnabled() and not dialog.place.isEnabled()
    assert not dialog.item_input.isEnabled() and not dialog.lines[0].qty.isEnabled()
    assert dialog.description.isEnabled() and dialog.date.isEnabled()


async def test_connection_tab_only_for_admin(qtbot, themes, linked, admin):
    page = SettingsPage(_ctx(themes, linked, admin))
    qtbot.addWidget(page)
    assert page.tabs.isTabVisible(page.server_index)
    await users.create_user(linked["db"], admin, "manager1", "مدیر پاساژ", "Str0ngPass1", "manager")
    manager = (await auth.login(linked["db"], "manager1", "Str0ngPass1")).actor
    page = SettingsPage(_ctx(themes, linked, manager))
    qtbot.addWidget(page)
    assert not page.tabs.isTabVisible(page.server_index)
    assert manager.can(goods.Perm.REQUESTS_MANAGE)  # the manager role runs requests itself


@pytest.mark.parametrize("role", ["viewer", "storekeeper"])
async def test_page_hidden_without_permission(qtbot, themes, linked, admin, role):
    await users.create_user(linked["db"], admin, "u1", "کاربر", "Str0ngPass1", role)
    actor = (await auth.login(linked["db"], "u1", "Str0ngPass1")).actor
    window = MainWindow(_ctx(themes, linked, actor))
    qtbot.addWidget(window)
    assert not window._nav_buttons["requests"].isVisibleTo(window)
    assert not window.server_status.isVisibleTo(window)
