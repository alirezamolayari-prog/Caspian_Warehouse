"""Test report issue 6: unusual document dates are never blocked, but the user is warned and asked
to confirm (outside the current fiscal year, or more than 30 days ago)."""

import datetime as dt
from decimal import Decimal

from caspian.core import jalali
from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocStatus, DocType
from caspian.services import documents, items, master
from caspian.services.items import ItemInput
from caspian.ui import documents_page
from caspian.ui.app_context import AppContext
from caspian.ui.documents_page import DocumentDialog, DocumentsList
from helpers import wait_until

TODAY = dt.date(2026, 10, 22)  # 1405/07/30: well inside fiscal year 1405


def test_warning_rules():
    assert documents.date_warning(TODAY, TODAY) is None
    assert documents.date_warning(TODAY - dt.timedelta(days=30), TODAY) is None
    text = documents.date_warning(TODAY - dt.timedelta(days=31), TODAY)
    assert text and "۳۱ روز" in text and jalali.format_date(TODAY - dt.timedelta(days=31)) in text
    start, _end = jalali.fiscal_year_bounds(jalali.fiscal_year_of(TODAY))
    text = documents.date_warning(start - dt.timedelta(days=1), TODAY)
    assert text and "سال مالی" in text and "۱۴۰۵" in text
    assert documents.date_warning(TODAY - dt.timedelta(days=800), TODAY)
    # Early in a fiscal year a recent date of the previous year is still flagged (other year).
    new_year = jalali.fiscal_year_bounds(1406)[0]
    assert "سال مالی" in documents.date_warning(new_year - dt.timedelta(days=2), new_year)


async def _editor(qtbot, themes, db, admin, date):
    unit = (await master.list_units(db))[0].id
    item = await items.create_item(db, admin, ItemInput("5001", "میخ", unit))
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    dialog = DocumentDialog(ctx, DocType.RECEIPT, None, await master.list_warehouses(db),
                            await master.search_persons(db),
                            await master.list_units(db, include_inactive=True), [])
    qtbot.addWidget(dialog)
    dialog.show()
    await dialog._ensure_units(item)
    dialog._add_line(item, "5001", "میخ", unit, Decimal(3))
    dialog.date.set_date(date)
    return dialog


async def test_old_date_asks_and_cancel_keeps_the_dialog(qtbot, themes, db, admin, monkeypatch):
    asked = []

    async def confirm(parent, text, *args, **kwargs):
        asked.append(text)
        return False

    monkeypatch.setattr(documents_page, "confirm", confirm)
    old = dt.date.today() - dt.timedelta(days=45)
    dialog = await _editor(qtbot, themes, db, admin, old)
    dialog.draft_button.click()
    assert await wait_until(lambda: bool(asked))
    assert jalali.format_date(old) in asked[0] and "ادامه" in asked[0]
    assert dialog.isVisible() and dialog.saved_id is None


async def test_old_date_continues_after_confirming(qtbot, themes, db, admin, monkeypatch):
    asked = []

    async def confirm(parent, text, *args, **kwargs):
        asked.append(text)
        return True

    monkeypatch.setattr(documents_page, "confirm", confirm)
    old = dt.date.today() - dt.timedelta(days=45)
    dialog = await _editor(qtbot, themes, db, admin, old)
    dialog.submit_button.click()  # «ذخیره و ثبت نهایی»: date question, then the usual one
    assert await wait_until(lambda: dialog.posted)
    assert jalali.format_date(old) in asked[0] and len(asked) == 2
    detail = await documents.get_document(db, admin, dialog.saved_id)
    assert detail.status == DocStatus.POSTED and detail.input.doc_date == old


async def test_recent_date_asks_nothing_extra(qtbot, themes, db, admin, monkeypatch):
    asked = []

    async def confirm(parent, text, *args, **kwargs):
        asked.append(text)
        return True

    monkeypatch.setattr(documents_page, "confirm", confirm)
    dialog = await _editor(qtbot, themes, db, admin, dt.date.today())
    dialog.draft_button.click()
    assert await wait_until(lambda: dialog.saved_id is not None)
    assert not asked


async def test_list_post_warns_about_an_old_draft(qtbot, themes, db, admin, monkeypatch):
    unit = (await master.list_units(db))[0].id
    item = await items.create_item(db, admin, ItemInput("5002", "پیچ", unit))
    wh = (await master.list_warehouses(db))[0].id
    old = dt.date.today() - dt.timedelta(days=60)
    draft = await documents.create_document(db, admin, documents.DocumentInput(
        DocType.RECEIPT, old, wh, [documents.LineInput(item, unit, Decimal(1))]))
    asked = []

    async def confirm(parent, text, *args, **kwargs):
        asked.append(text)
        return False

    monkeypatch.setattr(documents_page, "confirm", confirm)
    page = DocumentsList(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(page)
    page.show()
    await page.refresh()
    page.table.select_id(draft)
    await page.on_post()
    assert asked and jalali.format_date(old) in asked[0]
    assert (await documents.get_document(db, admin, draft)).status == DocStatus.DRAFT
