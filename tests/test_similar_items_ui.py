"""QA round 2, feature A (UI): suggestions while typing, choose existing, PIN override, merge."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocType, Document, Item, StockLedger
from caspian.services import items, master, protected
from caspian.services.items import ItemInput
from caspian.services.protected import ProtectedAction
from caspian.ui import items_page
from caspian.ui.app_context import AppContext
from helpers import settle, wait_until


@pytest.fixture
async def ctx(themes, db, admin):
    return AppContext(db, DbConfig(), Settings(), themes, admin)


async def _units(ctx):
    return await master.list_units(ctx.db)


async def _movements(db) -> tuple[int, int]:
    async with db.session() as s:
        return (await s.scalar(select(func.count()).select_from(Document)),
                await s.scalar(select(func.count()).select_from(StockLedger)))


async def _item_count(db) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(Item))


async def test_typing_shows_similar_items_and_click_loads_it(qtbot, ctx):
    units = await _units(ctx)
    existing = await items.create_item(ctx.db, ctx.actor, ItemInput(
        "1006", "جارو", units[0].id, reorder_point=Decimal(4), description="قدیمی",
        barcodes=[("6260001", None)]))
    before = await _movements(ctx.db), await _item_count(ctx.db)
    dlg = items_page.ItemDialog(ctx, units, [], suggested_code="1017",
                                warehouses=await master.list_warehouses(ctx.db))
    qtbot.addWidget(dlg)
    dlg.show()
    dlg.name.setText("جاروو")
    await dlg.update_suggestions()
    assert not dlg.similar_hint.isHidden() and "۱۰۰۶" in dlg.similar_hint.text()
    dlg.similar_hint.linkActivated.emit(str(existing))
    await wait_until(lambda: dlg.code.text() == "1006")
    # Still open, filled with the existing item, clearly marked; nothing saved, no stock moved.
    assert dlg.isVisible() and dlg.result() == 0
    assert dlg.name.text() == "جارو" and dlg.reorder_point.value() == Decimal(4)
    assert dlg.description.toPlainText() == "قدیمی"
    assert [r[0].text() for r in dlg.barcodes.rows] == ["6260001"]
    assert "ویرایش کالا" in dlg.windowTitle() and not dlg.existing_notice.isHidden()
    assert dlg.similar_hint.isHidden()
    assert (await _movements(ctx.db), await _item_count(ctx.db)) == before

    dlg.reorder_point.set_value(Decimal(9))
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.result() == 1 and dlg.saved_id == existing
    assert (await _movements(ctx.db), await _item_count(ctx.db)) == before  # updated, not inserted
    detail = await items.get_item(ctx.db, ctx.actor, existing)
    assert detail.input.reorder_point == Decimal(9) and detail.input.code == "1006"


async def test_opening_stock_after_picking_existing_goes_through_an_opening_document(qtbot, ctx):
    units = await _units(ctx)
    existing = await items.create_item(ctx.db, ctx.actor, ItemInput("1006", "جارو", units[0].id))
    dlg = items_page.ItemDialog(ctx, units, [], suggested_code="1017",
                                warehouses=await master.list_warehouses(ctx.db))
    qtbot.addWidget(dlg)
    dlg.name.setText("جاروو")
    await dlg.load_existing(existing)
    dlg.opening_qty.set_value(Decimal(5))
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.saved_id == existing and await _item_count(ctx.db) == 1
    async with ctx.db.session() as s:
        [doc] = (await s.scalars(select(Document))).all()
    assert doc.doc_type is DocType.OPENING
    [row] = await items.search_items(ctx.db, ctx.actor, "جارو")
    assert row.on_hand == Decimal(5)


async def test_similar_item_choice_or_admin_pin(qtbot, ctx, monkeypatch):
    units = await _units(ctx)
    existing = await items.create_item(ctx.db, ctx.actor, ItemInput("1006", "جارو", units[0].id))
    choices = iter(["use", "create"])

    async def fake_exec(dialog):
        if isinstance(dialog, items_page.SimilarItemsDialog):
            assert dialog.table.rowCount() == 1
            if next(choices) == "use":
                dialog.table.select_id(existing)
                dialog.on_use()
                return dialog.result()
            dialog.reason.setText("برند دیگر")
            return 1
        return 0

    async def fake_approval(db, actor, action, description, parent=None, details=None):
        assert action is ProtectedAction.CREATE_SIMILAR_ITEM and details == {"reason": "برند دیگر"}
        return await protected.approve(db, actor, action, "admin", "4826", details)

    monkeypatch.setattr(items_page, "exec_dialog", fake_exec)
    monkeypatch.setattr(items_page, "request_approval", fake_approval)
    dlg = items_page.ItemDialog(ctx, units, [], suggested_code="1017")
    qtbot.addWidget(dlg)
    dlg.name.setText("جاروو")
    dlg.submit_button.click()
    await settle(dlg)
    # «انتخاب این کالا» loads the existing item into the still-open form
    assert dlg.chosen_existing == existing and dlg.code.text() == "1006" and dlg.result() == 0
    assert len(await items.search_items(ctx.db, ctx.actor, "جارو")) == 1
    dlg2 = items_page.ItemDialog(ctx, units, [], suggested_code="1017")
    qtbot.addWidget(dlg2)
    dlg2.name.setText("جاروو")
    dlg2.submit_button.click()
    await settle(dlg2)
    assert dlg2.saved_id and len(await items.search_items(ctx.db, ctx.actor, "جارو")) == 2


async def test_duplicates_filter_and_merge(qtbot, ctx, monkeypatch):
    units = await _units(ctx)
    keep = await items.create_item(ctx.db, ctx.actor, ItemInput("1006", "جارو", units[0].id))
    dupe = await items.create_item(ctx.db, ctx.actor, ItemInput("1017", "سطل موقت", units[0].id))
    await items.create_item(ctx.db, ctx.actor, ItemInput("2001", "میز", units[0].id))
    async with ctx.db.session() as s:  # legacy duplicate
        await s.execute(update(Item).where(Item.id == dupe).values(name="جارو", name_normalized="جارو"))
    page = items_page.ItemsPage(ctx)
    qtbot.addWidget(page)
    page.duplicates_only.setChecked(True)
    await page.refresh()
    assert page.table.rowCount() == 2

    async def fake_approval(db, actor, action, description, parent=None, details=None):
        return await protected.approve(db, actor, action, "admin", "4826", details)

    async def fake_exec(dialog):
        if isinstance(dialog, items_page.MergeDialog):
            dialog.target.select_value(keep)
            dialog.submit_button.click()
            await settle(dialog)
            return 1
        return 1

    monkeypatch.setattr(items_page, "request_approval", fake_approval)
    monkeypatch.setattr(items_page, "exec_dialog", fake_exec)
    monkeypatch.setattr(items_page, "show_info", lambda parent, text: None)
    page.table.select_id(dupe)
    await page.on_merge()
    assert await wait_until(lambda: page.table.rowCount() == 0)  # no duplicates left
    assert not (await items.get_item(ctx.db, ctx.actor, dupe)).is_active
