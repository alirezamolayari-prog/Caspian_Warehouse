"""QA round 1, Phase 2 UI: confirmations (#12), duplicate-name warning (#13), validate first (#15),
dashboard health warning (#10)."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import update

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import BatchStatus, DocType, Document, ImportKind, ImportSource, StocktakeStatus
from caspian.services import documents, imports, items, master
from caspian.services import stocktake as st
from caspian.services.import_files import RawRow
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext
from helpers import settle, wait_until


@pytest.fixture
async def ctx(themes, db, admin):
    return AppContext(db, DbConfig(), Settings(), themes, admin)


def _answer(monkeypatch, module, value: bool):
    asked = []

    async def fake(parent, text, yes_text="بله", danger=False, title="تأیید"):
        asked.append(text)
        return value

    monkeypatch.setattr(module, "confirm", fake)
    return asked


@pytest.mark.parametrize("answer", [False, True])
async def test_discarding_an_import_draft_asks(qtbot, ctx, monkeypatch, answer):
    from caspian.ui import imports_page

    batch = await imports.create_batch(ctx.db, ctx.actor, ImportKind.ITEMS, ImportSource.CSV,
                                       [RawRow(name="کمد")], "x")
    asked = _answer(monkeypatch, imports_page, answer)
    page = imports_page.ImportsPage(ctx)
    qtbot.addWidget(page)
    await page.refresh()
    page.table.select_id(batch)
    await page.on_discard()
    detail = await imports.get_batch(ctx.db, ctx.actor, batch)
    assert asked and detail.row.status is (BatchStatus.DISCARDED if answer else BatchStatus.OPEN)


@pytest.mark.parametrize("answer", [False, True])
async def test_cancelling_a_stocktake_asks(qtbot, ctx, monkeypatch, answer):
    from caspian.ui import stocktake_page

    unit = (await master.list_units(ctx.db))[0].id
    await items.create_item(ctx.db, ctx.actor, ItemInput("1", "x", unit))
    sid = await st.create_stocktake(ctx.db, ctx.actor, (await master.list_warehouses(ctx.db))[0].id)
    asked = _answer(monkeypatch, stocktake_page, answer)
    page = stocktake_page.StocktakePage(ctx)
    qtbot.addWidget(page)
    await page.refresh()
    page.table.select_id(sid)
    await page.on_cancel()
    [row] = await st.list_stocktakes(ctx.db, ctx.actor)
    assert asked and row.status is (StocktakeStatus.CANCELLED if answer else StocktakeStatus.OPEN)


async def test_duplicate_item_name_warns_before_saving(qtbot, ctx, monkeypatch):
    from caspian.ui import items_page

    units = await master.list_units(ctx.db)
    await items.create_item(ctx.db, ctx.actor, ItemInput("1006", "جارو", units[0].id))
    asked = _answer(monkeypatch, items_page, False)
    dlg = items_page.ItemDialog(ctx, units, [], suggested_code="1017")
    qtbot.addWidget(dlg)
    dlg.name.setText("جارو")
    dlg.submit_button.click()
    await settle(dlg)
    assert asked and "1006" in asked[0] and dlg.saved_id is None
    assert len(await items.search_items(ctx.db, ctx.actor, "جارو")) == 1
    _answer(monkeypatch, items_page, True)  # «ذخیره» anyway
    dlg.submit_button.click()
    await settle(dlg)
    assert len(await items.search_items(ctx.db, ctx.actor, "جارو")) == 2


async def test_editor_validates_before_asking_to_post(qtbot, ctx, monkeypatch):
    from caspian.ui import documents_page
    from caspian.ui.documents_page import DocumentDialog

    asked = _answer(monkeypatch, documents_page, True)
    db = ctx.db
    dlg = DocumentDialog(ctx, DocType.RECEIPT, None, await master.list_warehouses(db),
                         await master.search_persons(db), await master.list_units(db), [])
    qtbot.addWidget(dlg)
    dlg.submit_button.click()  # no lines
    await settle(dlg)
    assert asked == [] and "حداقل یک ردیف" in dlg.status.text()


async def test_dashboard_warns_about_unhealthy_data(qtbot, ctx, monkeypatch):
    from caspian.ui import pages

    unit = (await master.list_units(ctx.db))[0].id
    item = await items.create_item(ctx.db, ctx.actor, ItemInput("1", "x", unit))
    wh = (await master.list_warehouses(ctx.db))[0].id
    doc = await documents.create_and_post(ctx.db, ctx.actor, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(item, unit, Decimal(1))]))
    page = pages.DashboardPage(ctx)
    qtbot.addWidget(page)
    await page.refresh()
    assert page.health_warning.isHidden()
    async with ctx.db.session() as s:
        await s.execute(update(Document).where(Document.id == doc)
                        .values(doc_date=dt.date.today() + dt.timedelta(days=300)))
    shown = []

    async def fake_show(c, parent=None):
        shown.append(True)

    monkeypatch.setattr(pages, "show_health", fake_show)
    await page.refresh()
    assert not page.health_warning.isHidden() and "۱ مورد" in page.health_warning.text()
    page.health_warning.click()
    assert await wait_until(lambda: shown)
