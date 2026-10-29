"""QA Part A, issues 1 and 4: the assistant prepares drafts, the person confirms on a card in the
chat; links to documents open them."""

import asyncio
import datetime as dt
from decimal import Decimal

import httpx
import pytest
from PySide6.QtCore import QPoint, Qt, QUrl
from PySide6.QtTest import QTest
from sqlalchemy import select

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import AuditLog, DocStatus, DocType, Document, PersonKind, ProviderKind
from caspian.services import auth, documents, items, locations, master, reports, users
from caspian.services.ai import config
from caspian.services.ai.gateway import Gateway
from caspian.services.items import ItemInput
from caspian.ui import documents_page
from caspian.ui.app_context import AppContext
from caspian.ui.assistant_page import AssistantPage
from helpers import wait_until
from test_assistant import call, scripted


@pytest.fixture
async def shop(locations_required, admin, monkeypatch):
    db = locations_required
    monkeypatch.setattr(config, "get_secret", lambda k, n: None)
    await config.save_provider(db, admin, "Local", ProviderKind.OLLAMA, "http://localhost:11434/v1", "q")
    u = (await master.list_units(db))[0].id
    wh = (await master.list_warehouses(db))[0].id
    candle = await items.create_item(db, admin, ItemInput("1016", "شمع", u))
    await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(candle, u, Decimal(5))]))
    await master.save_person(db, admin, "خانم صفا", PersonKind.EMPLOYEE)
    return {"db": db, "candle": candle}


async def _stock(db, item_id) -> Decimal:
    return sum((q for _w, q in await documents.stock_by_warehouse(db, item_id)), Decimal(0))


def _page(qtbot, themes, db, actor, handler, open_document=None):
    ctx = AppContext(db, DbConfig(), Settings(), themes, actor)
    ctx.ai = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    ctx.assistant_ai = ctx.ai  # the conversation logic, with a mocked provider instead of the server
    page = AssistantPage(ctx, open_document=open_document)
    qtbot.addWidget(page)
    page.resize(900, 700)
    page.show()
    return page


def _issue_script(qty=1):
    return scripted(
        {"content": None, "tool_calls": [call(
            "create_document", doc_type="ISSUE", person="خانم صفا", lines=[{"code": "1016", "qty": qty}],
            location="پله اضطراری اسدنژاد", floor="دوم", usage_note="روشنایی پله")]},
        {"content": "پیش‌نویس حواله آماده است؛ برای ثبت، دکمه تأیید را بزنید."})


async def test_yes_posts_as_the_person(qtbot, themes, shop, admin):
    db = shop["db"]
    handler, _seen = _issue_script()
    page = _page(qtbot, themes, db, admin, handler)
    await page.send("یه شمع بده به خانم صفا برای پله اضطراری اسدنژاد طبقه دوم")
    [card] = page.pending_cards()
    assert card.isVisible() and page.window().focusWidget() is card.yes  # on screen, ready for Enter
    text = card.text.text()
    assert "۱ عدد شمع (کد ۱۰۱۶)" in text and "تحویل‌گیرنده: خانم صفا" in text
    assert "محل مصرف: پله اضطراری اسدنژاد · طبقه دوم" in text and "شرح مصرف: روشنایی پله" in text
    assert await _stock(db, shop["candle"]) == 5  # nothing changed before «بله»

    card.yes.click()
    assert await wait_until(lambda: not page.pending_cards())
    assert await _stock(db, shop["candle"]) == 4
    assert "ثبت نهایی شد" in page.transcript.toPlainText()
    async with db.session() as s:
        posted = await s.scalar(select(AuditLog).where(AuditLog.action == "document.posted")
                                .order_by(AuditLog.id.desc()))
    assert posted.details["via"] == "assistant" and posted.user_id == admin.user_id
    table = await reports.user_activity(db, admin)
    actions = [row[2] for row in table.rows]
    assert any("به پیشنهاد دستیار" in a for a in actions)
    assert any("توسط دستیار از طرف" in a for a in actions)  # the draft the assistant created


async def test_no_deletes_the_draft(qtbot, themes, shop, admin):
    db = shop["db"]
    handler, _seen = _issue_script()
    page = _page(qtbot, themes, db, admin, handler)
    await page.send("یه شمع بده به خانم صفا")
    [card] = page.pending_cards()
    doc_id = card.summary.document_id
    card.no.click()
    assert await wait_until(lambda: not page.pending_cards())
    async with db.session() as s:
        assert await s.get(Document, doc_id) is None
        assert await s.scalar(select(AuditLog.id).where(
            AuditLog.action == "document.assistant_draft_discarded"))
    assert await _stock(db, shop["candle"]) == 5


async def test_a_changed_draft_or_missing_stock_is_never_posted(qtbot, themes, shop, admin):
    db = shop["db"]
    handler, _seen = _issue_script(qty=1)
    page = _page(qtbot, themes, db, admin, handler)
    await page.send("یه شمع بده به خانم صفا")
    [card] = page.pending_cards()
    doc_id = card.summary.document_id
    detail = await documents.get_document(db, admin, doc_id)  # someone edits it to 9 meanwhile
    detail.input.lines[0].qty = Decimal(9)
    await documents.update_document(db, admin, doc_id, detail.version_id, detail.input)
    card.yes.click()
    assert await wait_until(lambda: card.status.isVisible())
    assert (await documents.get_document(db, admin, doc_id)).status is DocStatus.DRAFT
    assert await _stock(db, shop["candle"]) == 5 and page.pending_cards() == [card]

    # A fresh card for the edited draft: posting 9 of 5 fails, the draft stays.
    fresh = await page.add_card(doc_id)
    fresh.yes.click()
    assert await wait_until(lambda: "کافی نیست" in fresh.status.text())
    assert (await documents.get_document(db, admin, doc_id)).status is DocStatus.DRAFT


async def test_keyboard_and_permissions(qtbot, themes, shop, admin):
    db = shop["db"]
    handler, _seen = _issue_script()
    page = _page(qtbot, themes, db, admin, handler)
    await page.send("یه شمع بده به خانم صفا")
    [card] = page.pending_cards()
    QTest.keyClick(card, Qt.Key.Key_Escape)  # Esc = «نه»
    assert await wait_until(lambda: not page.pending_cards())

    await users.create_user(db, admin, "anbar", "انباردار", "Anbar#2026x", "storekeeper")
    storekeeper = (await auth.login(db, "anbar", "Anbar#2026x")).actor
    viewer_page = _page(qtbot, themes, db, storekeeper, lambda r: httpx.Response(500))
    doc_id = await documents.create_document(db, storekeeper, documents.DocumentInput(
        DocType.ISSUE, dt.date.today(), (await master.list_warehouses(db))[0].id,
        [documents.LineInput(shop["candle"], (await master.list_units(db))[0].id, Decimal(1))]))
    assert not (await viewer_page.add_card(doc_id)).yes.isHidden()  # storekeepers may post
    await users.create_user(db, admin, "nazer", "ناظر", "Nazer#2026x", "viewer")
    viewer = (await auth.login(db, "nazer", "Nazer#2026x")).actor
    page_v = _page(qtbot, themes, db, viewer, lambda r: httpx.Response(500))
    card_v = await page_v.add_card(doc_id)
    assert card_v.yes.isHidden() and "مجوز" in card_v.status.text()


# ----- issue 4: links and the card's «باز کردن سند» open the document -----


async def test_clicking_the_document_link_opens_it(qtbot, themes, shop, admin):
    db = shop["db"]
    opened = []

    async def open_document(doc_id):
        opened.append(doc_id)

    handler, _seen = _issue_script()
    page = _page(qtbot, themes, db, admin, handler, open_document)
    await page.send("یه شمع بده به خانم صفا")
    doc_id = page.pending_cards()[0].summary.document_id
    cursor = page.transcript.document().find("باز کردن")
    assert cursor.charFormat().anchorHref() == f"doc:{doc_id}"
    pos = page.transcript.cursorRect(cursor).center() - QPoint(8, 0)
    QTest.mouseClick(page.transcript.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert await wait_until(lambda: opened == [doc_id])
    page.pending_cards()[0].open_button.click()
    assert await wait_until(lambda: opened == [doc_id, doc_id])


async def test_link_through_the_main_window_shows_the_editor(qtbot, themes, shop, admin, monkeypatch,
                                                            tmp_path):
    from caspian.ui.main_window import MainWindow

    monkeypatch.setattr("caspian.core.settings.settings_path", lambda: tmp_path / "settings.json")
    db = shop["db"]
    index = await locations.load_index(db)
    floor = next(f.id for f in index.floors if f.code == "F2")
    person = (await master.search_persons(db, "صفا"))[0].id
    doc_id = await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.ISSUE, dt.date.today(), (await master.list_warehouses(db))[0].id,
        [documents.LineInput(shop["candle"], (await master.list_units(db))[0].id, Decimal(1))],
        person_id=person, location_id=index.find("PL-ASD").id, floor_id=floor))
    shown = []
    real_exec = documents_page.exec_dialog

    async def spy(dialog):
        task = asyncio.ensure_future(real_exec(dialog))
        await asyncio.sleep(0.2)
        shown.append((type(dialog).__name__, dialog.isVisible()))
        dialog.reject()
        return await task

    monkeypatch.setattr(documents_page, "exec_dialog", spy)
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.navigate("assistant")
    win._pages["assistant"].on_link(QUrl(f"doc:{doc_id}"))
    assert await wait_until(lambda: shown == [("DocumentDialog", True)])
    assert win.current_page == "documents"


async def test_a_failing_open_is_reported_not_silent(qtbot, themes, shop, admin):
    async def broken(_doc_id):
        raise RuntimeError("boom")

    page = _page(qtbot, themes, shop["db"], admin, lambda r: httpx.Response(500), broken)
    page.on_link(QUrl("doc:999"))
    assert await wait_until(lambda: "سند باز نشد" in page.transcript.toPlainText())
