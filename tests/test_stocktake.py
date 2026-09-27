import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from caspian.db.models import DocStatus, DocType, StocktakeStatus
from caspian.services import auth, items, master, users
from caspian.services import documents as docs
from caspian.services import stocktake as st
from caspian.services.errors import PermissionDenied, ValidationError
from caspian.services.items import ItemInput


@pytest.fixture
async def env(db, admin):
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    tools = await master.save_category(db, admin, "ابزار")
    a = await items.create_item(db, admin, ItemInput("1001", "دریل", u["عدد"], category_id=tools,
                                                     barcodes=[("111", None)]))
    b = await items.create_item(db, admin, ItemInput("1002", "فرز", u["عدد"], category_id=tools))
    c = await items.create_item(db, admin, ItemInput("2001", "میز", u["عدد"]))
    await docs.create_and_post(db, admin, docs.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh,
        [docs.LineInput(a, u["عدد"], Decimal(10)), docs.LineInput(b, u["عدد"], Decimal(5))]))
    await users.create_user(db, admin, "shomar", "شمارشگر", "Count#2026", "counter")
    counter = (await auth.login(db, "shomar", "Count#2026")).actor
    return {"wh": wh, "tools": tools, "a": a, "b": b, "c": c, "counter": counter, "u": u}


async def _counts(db, actor, stocktake_id, **by_code):
    sheet = await st.count_sheet(db, actor, stocktake_id)
    counts = {ln.id: (Decimal(by_code[ln.code]) if by_code.get(ln.code) is not None else None, "")
              for ln in sheet.lines if ln.code in by_code}
    await st.record_counts(db, actor, stocktake_id, counts)


async def test_blind_count_to_adjustment(db, admin, env):
    counter = env["counter"]
    sid = await st.create_stocktake(db, counter, env["wh"], title="پایان فصل")
    sheet = await st.count_sheet(db, counter, sid)
    assert [ln.code for ln in sheet.lines] == ["1001", "1002", "2001"]
    # The counter's view carries no system quantities at all.
    assert "system_qty" not in {f.name for f in dataclasses.fields(st.SheetLine)}
    with pytest.raises(PermissionDenied):
        await st.discrepancy_report(db, counter, sid)
    # ...and item lists hide stock from counters too.
    assert all(r.on_hand is None for r in await items.search_items(db, counter))

    await _counts(db, counter, sid, **{"1001": "8", "1002": "5"})
    with pytest.raises(ValidationError, match="شمارش نشده"):
        await st.submit_counts(db, counter, sid)
    await st.submit_counts(db, counter, sid, missing_as_zero=True)
    with pytest.raises(ValidationError, match="قفل"):
        await _counts(db, counter, sid, **{"1001": "9"})

    report = await st.discrepancy_report(db, admin, sid)
    assert [(d.code, d.system_qty, d.counted_qty, d.difference) for d in report.differences] == [
        ("1001", Decimal(10), Decimal(8), Decimal(-2))]
    assert report.movements_since_snapshot == 0

    doc_id = await st.approve(db, admin, sid)
    doc = await docs.get_document(db, admin, doc_id)
    assert doc.status is DocStatus.POSTED and doc.input.doc_type is DocType.ADJUSTMENT
    assert (await docs.stock_by_warehouse(db, env["a"]))[0][1] == Decimal(8)
    rows = await st.list_stocktakes(db, admin)
    assert rows[0].status is StocktakeStatus.APPROVED and rows[0].counted == 3


async def test_category_scope_and_single_open_per_warehouse(db, admin, env):
    sid = await st.create_stocktake(db, admin, env["wh"], env["tools"])
    assert len((await st.count_sheet(db, admin, sid)).lines) == 2
    with pytest.raises(ValidationError, match="باز"):
        await st.create_stocktake(db, admin, env["wh"])
    await st.cancel(db, admin, sid)
    await st.create_stocktake(db, admin, env["wh"])


async def test_find_line_by_barcode_or_code(db, admin, env):
    sid = await st.create_stocktake(db, admin, env["wh"])
    sheet = await st.count_sheet(db, admin, sid)
    assert await st.find_line(db, sid, "111") == sheet.lines[0].id
    assert await st.find_line(db, sid, "۲۰۰۱") == sheet.lines[2].id
    assert await st.find_line(db, sid, "nope") is None


async def test_movements_after_snapshot_are_flagged(db, admin, env):
    sid = await st.create_stocktake(db, admin, env["wh"])
    await docs.create_and_post(db, admin, docs.DocumentInput(
        DocType.ISSUE, dt.date.today(), env["wh"],
        [docs.LineInput(env["b"], env["u"]["عدد"], Decimal(1))]))
    await _counts(db, admin, sid, **{"1001": "10", "1002": "4", "2001": "0"})
    await st.submit_counts(db, admin, sid)
    report = await st.discrepancy_report(db, admin, sid)
    assert report.movements_since_snapshot == 1


async def test_no_differences_posts_nothing(db, admin, env):
    sid = await st.create_stocktake(db, admin, env["wh"])
    await _counts(db, admin, sid, **{"1001": "10", "1002": "5", "2001": "0"})
    await st.submit_counts(db, admin, sid)
    assert await st.approve(db, admin, sid) is None
    with pytest.raises(ValidationError):
        await st.approve(db, admin, sid)


async def test_counter_cannot_approve_or_cancel(db, admin, env):
    sid = await st.create_stocktake(db, env["counter"], env["wh"])
    with pytest.raises(PermissionDenied):
        await st.cancel(db, env["counter"], sid)
    await st.submit_counts(db, env["counter"], sid, missing_as_zero=True)
    with pytest.raises(PermissionDenied):
        await st.approve(db, env["counter"], sid)
