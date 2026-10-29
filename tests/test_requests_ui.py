"""«درخواست کالا» on screen: the list and statuses, the new-request dialog (one request per dialog,
however often it is clicked), notifications and badge, chat opening at the newest message, the
connection tab, and printing an approved voucher (which posts it)."""

from decimal import Decimal

import pytest
from PySide6.QtWidgets import QLabel, QToolButton
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
    rows = range(page.table.rowCount())
    statuses = {page.table.item(r, 5).text() for r in rows}
    assert statuses == {"در صف ارسال", "● تأیید شد", "● رد شد", "در انتظار تأیید مالک"}
    sending = {page.table.item(r, 6).text() for r in rows}
    assert "دلیل رد: زیاد است" in sending and "آماده چاپ حواله" in sending
    assert {page.table.item(r, 8).text() for r in rows} == {"فوری"}  # «توضیحات» = the user's note only
    numbers = {page.table.item(r, 0).text() for r in rows}
    assert "در صف" in numbers and "—" not in numbers
    assert {page.table.item(r, 2).text() for r in rows} == {"انبار پاساژ", "مدیر سیستم"}  # «ثبت‌کننده»


async def test_full_error_in_tooltip_and_details_and_my_requests_filter(qtbot, themes, linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await goods.create_request(db, admin, request_input(linked))
    server.refuse_next = [(422, "")]
    await engine.tick(admin)
    ctx = _ctx(themes, linked, admin)
    page = RequestsPage(ctx)
    qtbot.addWidget(page)
    page.show()
    await page.refresh()
    [row] = page._rows.values()
    cell = page.table.item(0, 6)
    assert cell.text().startswith("ارسال نشد:") and cell.toolTip() == row.last_error
    dialog = RequestChatDialog(ctx, row, page)
    qtbot.addWidget(dialog)
    assert row.last_error in dialog.details.text()
    assert dialog.heading.text() == "درخواست در صف ارسال — علی رضایی"
    page.mine_only.setChecked(True)
    assert await wait_until(lambda: page.table.rowCount() == 1)
    await users.create_user(db, admin, "other", "دیگری", "Str0ngPass1", "manager")
    other = (await auth.login(db, "other", "Str0ngPass1")).actor
    assert not await goods.list_requests(db, other, mine=True)
    assert [r.id for r in await goods.list_requests(db, admin, mine=True)] == [request_id]


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
    assert "ساعت ۰" in dialog.transcript.toPlainText() or "ساعت ۱" in dialog.transcript.toPlainText() \
        or "ساعت ۲" in dialog.transcript.toPlainText()  # Jalali date + HH:MM
    await engine.tick(admin)
    await dialog.reload()
    assert await wait_until(lambda: "ارسال شد" in dialog.transcript.toPlainText())


async def _approved_draft(linked, admin) -> int:
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    await goods.create_request(db, admin, request_input(linked))
    await engine.tick(admin)
    server.approve(1)
    await engine.tick(admin)
    async with db.session() as s:
        return await s.scalar(select(Document.id).where(Document.server_request_id == 1))


async def test_approved_slip_is_final_only_after_it_is_really_printed(qtbot, themes, linked, admin,
                                                                       monkeypatch):
    """3.1: closing the preview or the print dialog never posts; printing does; a reprint is a copy."""
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    doc_id = await _approved_draft(linked, admin)
    ctx = _ctx(themes, linked, admin)
    previews, printed, choose_printer = [], [], []

    async def confirm(parent, text, *args, **kwargs):
        return True

    async def ask_print(printer, parent=None):
        return choose_printer.pop(0)

    async def exec_dialog(dialog):
        previews.append(dialog)
        return 0

    monkeypatch.setattr(messages, "confirm", confirm)
    monkeypatch.setattr(document_print, "ask_print", ask_print)
    monkeypatch.setattr(document_print, "print_to", lambda printer, html_text: printed.append(html_text))
    monkeypatch.setattr(document_print, "exec_dialog", exec_dialog)

    assert not await document_print.preview_document(None, ctx, doc_id)  # preview, then «بستن»
    [preview] = previews
    assert isinstance(preview, document_print.DraftPreviewDialog) and preview.pages
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.DRAFT
    assert await stock(db, linked["bags"]) == 50 and not printed

    choose_printer.append(False)  # the print dialog is cancelled
    assert not await document_print.print_document(None, ctx, doc_id)
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.DRAFT and not printed

    choose_printer.append(True)  # a printer was chosen: posted, printed with its number
    assert await document_print.print_document(None, ctx, doc_id)
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.POSTED
    assert await stock(db, linked["bags"]) == 30
    assert len(printed) == 1 and "پیش‌نویس" not in printed[0] and "المثنی" not in printed[0]
    assert (await documents.print_sheet(db, admin, doc_id)).print_count == 1
    await engine.tick(admin)
    assert server.requests[1]["status"] == "issued"

    async def preview_and_print(parent, html_text, paper):
        printed.append(html_text)
        return True

    monkeypatch.setattr(document_print, "preview_and_print", preview_and_print)
    assert await document_print.print_document(None, ctx, doc_id)  # a reprint
    assert "المثنی" in printed[-1]


async def test_pdf_of_an_approved_slip_cancelled_posts_nothing(qtbot, themes, linked, admin, monkeypatch):
    db = linked["db"]
    doc_id = await _approved_draft(linked, admin)

    async def confirm(parent, text, *args, **kwargs):
        return True

    async def no_file(*args, **kwargs):
        return None

    monkeypatch.setattr(messages, "confirm", confirm)
    monkeypatch.setattr(document_print, "ask_save_path", no_file)
    assert not await document_print.pdf_document(None, _ctx(themes, linked, admin), doc_id)
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.DRAFT


async def test_print_failure_after_posting_keeps_the_original(qtbot, themes, linked, admin, monkeypatch):
    db = linked["db"]
    doc_id = await _approved_draft(linked, admin)
    errors = []

    async def yes(*args, **kwargs):
        return True

    def broken(printer, html_text):
        raise RuntimeError("printer jammed")

    monkeypatch.setattr(messages, "confirm", yes)
    monkeypatch.setattr(document_print, "ask_print", yes)
    monkeypatch.setattr(document_print, "print_to", broken)
    monkeypatch.setattr(document_print, "show_error", lambda parent, text: errors.append(text))
    assert await document_print.print_document(None, _ctx(themes, linked, admin), doc_id)
    assert errors == [document_print.PRINT_FAILED]
    sheet = await documents.print_sheet(db, admin, doc_id)
    assert sheet.status == DocStatus.POSTED and sheet.print_count == 0  # the next print is the original


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
    dialog.show()
    assert not dialog.item_input.isVisible()  # 3.2: no search box that does nothing
    assert not [w for w in dialog.table.findChildren(QToolButton) if w.text() == "✕" and w.isVisible()]
    note = dialog.findChild(QLabel, "LockedNote")
    assert note is not None and "قابل تغییر نیست" in note.text()


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


async def test_item_suggestions_while_typing_and_over_stock_warning(qtbot, themes, linked, admin):
    """3.5: suggestions (code — name — unit — stock) appear before Enter; more than the stock is
    allowed but marked on that row."""
    dialog = await _dialog(_ctx(themes, linked, admin), linked)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.item_input.setText("کیسه")
    dialog._suggest()
    assert await wait_until(lambda: dialog._suggest_model.rowCount() > 0)
    text = dialog._suggest_model.stringList()[0]
    assert "کیسه زباله" in text and "موجودی: ۵۰" in text
    dialog._on_suggestion(dialog._suggest_model.index(0))
    assert await wait_until(lambda: len(dialog.lines) == 1)
    line = dialog.lines[0]
    assert not dialog.over_stock_lines()
    line.qty.set_value(Decimal(80))
    assert dialog.over_stock_lines() == [line] and "بیشتر از موجودی" in line.name_cell.toolTip()
    line.qty.set_value(Decimal(5))
    assert not dialog.over_stock_lines()


async def test_add_place_link_only_for_location_managers(qtbot, themes, linked, admin):
    dialog = await _dialog(_ctx(themes, linked, admin), linked)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.place.add_button.isVisible()
    await users.create_user(linked["db"], admin, "m2", "مدیر انبار", "Str0ngPass1", "manager")
    manager = (await auth.login(linked["db"], "m2", "Str0ngPass1")).actor
    other = await _dialog(_ctx(themes, linked, manager), linked)
    qtbot.addWidget(other)
    other.show()
    assert not other.place.add_button.isVisible()


async def test_connection_bar_is_coloured_with_a_setup_button(qtbot, themes, linked, admin):
    page = RequestsPage(_ctx(themes, linked, admin))
    qtbot.addWidget(page)
    page.show()
    asked = []
    page.setup_requested.connect(lambda: asked.append(1))
    page._show_server_state(TickResult(SyncState.ACCOUNT_PROBLEM, "رمز اشتباه است"))
    assert page.notice.isVisible() and page.setup_button.isVisible()
    page.setup_button.click()
    assert asked == [1]
    page._show_server_state(TickResult(SyncState.DISCONNECTED, "قطع"))
    assert page.notice.isVisible() and not page.setup_button.isVisible()
    page._show_server_state(TickResult(SyncState.CONNECTED))
    assert not page.notice.isVisible()


async def test_dashboard_request_cards_open_the_filtered_list(qtbot, themes, linked, admin):
    """4.5: «در انتظار تأیید مالک» / «تأییدشده — منتظر چاپ حواله» open the list with that filter,
    shown as a chip that clears it."""
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    for qty in (1, 2):
        await goods.create_request(db, admin, request_input(linked, qty=qty))
    await engine.tick(admin)
    server.approve(1)
    await engine.tick(admin)
    assert await goods.dashboard_counts(db) == (1, 1)
    window = MainWindow(_ctx(themes, linked, admin))
    qtbot.addWidget(window)
    dashboard = window._pages["dashboard"]
    await dashboard.refresh()
    assert dashboard.cards["requests_pending"].value.text() == "۱"
    dashboard.cards["requests_approved"].clicked.emit()
    requests = window._pages["requests"]
    assert window._stack.currentWidget() is requests
    assert requests.status_filter.currentData() == RequestStatus.APPROVED
    assert requests.filter_chip.isVisibleTo(requests) and "تأیید شد" in requests.filter_chip.text()
    requests.filter_chip.click()
    assert requests.status_filter.currentData() is None and not requests.filter_chip.isVisibleTo(requests)
