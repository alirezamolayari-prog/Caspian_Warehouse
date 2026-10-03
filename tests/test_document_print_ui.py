import datetime as dt
from decimal import Decimal

import pytest

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocType, PersonKind
from caspian.services import documents as docs
from caspian.services import items, master
from caspian.services.documents import DocumentInput, LineInput
from caspian.services.items import ItemInput
from caspian.ui import document_print
from caspian.ui.app_context import AppContext
from caspian.ui.document_print import document_html
from caspian.ui.documents_page import DocumentsPage
from caspian.ui.printing import save_pdf
from helpers import wait_until


@pytest.fixture
async def env(themes, db, admin):
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    drill = await items.create_item(db, admin, ItemInput("1001", "دریل بوش", u["عدد"]))
    person = await master.save_person(db, admin, "علی رضایی", PersonKind.SUPPLIER)
    receipt = await docs.create_and_post(db, admin, DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [LineInput(drill, u["عدد"], Decimal(5), Decimal(2000), "نو")],
        person_id=person))
    draft = await docs.create_document(db, admin, DocumentInput(
        DocType.ISSUE, dt.date.today(), wh, [LineInput(drill, u["عدد"], Decimal(1))]))
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    return {"ctx": ctx, "receipt": receipt, "draft": draft}


async def test_form_contents_and_duplicate_stamp(env):
    ctx = env["ctx"]
    sheet = await docs.print_sheet(ctx.db, ctx.actor, env["receipt"])
    first = document_html(sheet, 1, "A5")
    for text in ("رسید ورود", "ر-۱", "علی رضایی", "تأمین‌کننده", "دریل بوش", "۱۰۰۱", "عدد", "نو",
                 "۱۰٬۰۰۰", "تحویل‌دهنده", "تحویل‌گیرنده", "انباردار", "بازار مبلمان کاسپین"):
        assert text in first, text
    assert "المثنی" not in first
    assert "کپی / المثنی (نسخه ۲)" in document_html(sheet, 2, "A4")
    draft = await docs.print_sheet(ctx.db, ctx.actor, env["draft"])
    assert "پیش‌نویس — فاقد اعتبار" in document_html(draft, 1)


async def test_pdf_renders_a5(env, tmp_path):
    ctx = env["ctx"]
    sheet = await docs.print_sheet(ctx.db, ctx.actor, env["receipt"])
    path = tmp_path / "r.pdf"
    save_pdf(document_html(sheet, 1), str(path), document_print.PAPERS["A5"])
    assert path.read_bytes().startswith(b"%PDF") and path.stat().st_size > 1000


async def test_print_from_list_counts_copies(qtbot, env, monkeypatch):
    shown = []

    async def fake_preview(parent, html_text, paper):
        shown.append((html_text, paper))
        return True

    monkeypatch.setattr(document_print, "preview_and_print", fake_preview)
    page = DocumentsPage(env["ctx"])
    qtbot.addWidget(page)
    lst = page.documents
    await lst.refresh()
    lst.table.select_id(env["receipt"])
    assert lst.print_button.isEnabled()
    actions = lst.print_button.menu().actions()
    assert [a.text() for a in actions] == ["پیش‌نمایش و چاپ (A5)", "پیش‌نمایش و چاپ (A4)", "ذخیره PDF (A5)"]
    actions[0].trigger()
    assert await wait_until(lambda: "چاپ‌شده (۱)" in lst.table.item(lst.table.currentRow(), 6).text())
    actions[1].trigger()
    assert await wait_until(lambda: len(shown) == 2)
    assert "المثنی" not in shown[0][0] and "المثنی" in shown[1][0] and shown[1][1] == "A4"
    assert await wait_until(lambda: "چاپ‌شده (۲)" in lst.table.item(lst.table.currentRow(), 6).text())


async def test_cancelled_preview_is_not_counted(qtbot, env, monkeypatch):
    async def cancel(parent, html_text, paper):
        return False

    monkeypatch.setattr(document_print, "preview_and_print", cancel)
    ctx = env["ctx"]
    assert not await document_print.print_document(None, ctx, env["receipt"])
    assert (await docs.print_sheet(ctx.db, ctx.actor, env["receipt"])).print_count == 0


async def test_pdf_export_counts_and_draft_does_not(qtbot, env, monkeypatch, tmp_path):
    async def fake_ask(parent, title, settings, default_name, name_filter):
        return str(tmp_path / default_name)

    monkeypatch.setattr(document_print, "ask_save_path", fake_ask)
    ctx = env["ctx"]
    assert await document_print.pdf_document(None, ctx, env["receipt"])
    assert (tmp_path / "رسید ورود ر-۱.pdf").exists()
    assert (await docs.print_sheet(ctx.db, ctx.actor, env["receipt"])).print_count == 1
    assert await document_print.pdf_document(None, ctx, env["draft"])  # allowed, but not counted
    assert (await docs.print_sheet(ctx.db, ctx.actor, env["draft"])).print_count == 0


async def test_form_fields_are_bidi_safe_and_complete(env):
    """#16: item count was empty, «شماره: … تاریخ: …» lost its colons, footer showed «admin»."""
    ctx = env["ctx"]
    sheet = await docs.print_sheet(ctx.db, ctx.actor, env["receipt"])
    html_text = document_html(sheet, 1, "A5")
    assert "⁧تعداد اقلام:⁩" in html_text and "<b>۱</b>" in html_text
    assert "⁧شماره:⁩" in html_text and "⁧تاریخ:⁩" in html_text
    assert ctx.actor.display_name in html_text and sheet.created_by == ctx.actor.display_name
    assert "font-size: 10pt" in html_text
