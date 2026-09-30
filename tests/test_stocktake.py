import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from caspian.db.models import DocStatus, DocType, StocktakeStatus
from caspian.services import auth, items, master, protected, users
from caspian.services import documents as docs
from caspian.services import stocktake as st
from caspian.services.errors import ApprovalError, PermissionDenied, StocktakeFrozen, ValidationError
from caspian.services.items import ItemInput
from caspian.services.protected import ProtectedAction
from conftest import ADMIN_PIN


@pytest.fixture
async def env(db, admin):
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    tools = await master.save_category(db, admin, "ابزار")
    a = await items.create_item(db, admin, ItemInput("1001", "دریل", u["عدد"], category_id=tools,
                                                     units=[(u["کارتن"], Decimal(12))],
                                                     barcodes=[("111", None), ("111-C", u["کارتن"])]))
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
    # Only possible with an admin override since the freeze is enforced (#7).
    issue = await docs.create_document(db, admin, docs.DocumentInput(
        DocType.ISSUE, dt.date.today(), env["wh"],
        [docs.LineInput(env["b"], env["u"]["عدد"], Decimal(1))]))
    approval = await protected.approve(db, admin, ProtectedAction.STOCKTAKE_OVERRIDE, "admin", ADMIN_PIN)
    await docs.post_document(db, admin, issue, approval=approval)
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


# ----- freeze while counting (#7) -----


def _issue(env, item="b", qty=1, wh="wh"):
    return docs.DocumentInput(DocType.ISSUE, dt.date.today(), env[wh],
                              [docs.LineInput(env[item], env["u"]["عدد"], Decimal(qty))])


async def test_posting_is_blocked_while_counting(db, admin, env):
    draft = await docs.create_document(db, admin, _issue(env))
    sid = await st.create_stocktake(db, admin, env["wh"])
    with pytest.raises(StocktakeFrozen, match="انبارگردانی"):
        await docs.post_document(db, admin, draft)
    await st.submit_counts(db, admin, sid, missing_as_zero=True)  # still frozen until approved
    with pytest.raises(StocktakeFrozen):
        await docs.post_document(db, admin, draft)
    assert (await docs.get_document(db, admin, draft)).status is DocStatus.DRAFT


async def test_cancel_is_blocked_while_counting(db, admin, env):
    posted = await docs.create_and_post(db, admin, _issue(env))
    await st.create_stocktake(db, admin, env["wh"])
    with pytest.raises(StocktakeFrozen):
        await docs.cancel_document(db, admin, posted, "اشتباه")


async def test_freeze_only_covers_counted_items_and_warehouse(db, admin, env):
    wh2 = await master.save_warehouse(db, admin, "02", "انبار دوم")
    env["wh2"] = wh2
    await st.create_stocktake(db, admin, env["wh"], env["tools"])  # counts 1001 and 1002 only
    await docs.create_and_post(db, admin, docs.DocumentInput(  # item outside the category: fine
        DocType.RECEIPT, dt.date.today(), env["wh"], [docs.LineInput(env["c"], env["u"]["عدد"], Decimal(1))]))
    await docs.create_and_post(db, admin, docs.DocumentInput(  # other warehouse: fine
        DocType.RECEIPT, dt.date.today(), wh2, [docs.LineInput(env["a"], env["u"]["عدد"], Decimal(1))]))
    transfer_in = docs.DocumentInput(DocType.TRANSFER, dt.date.today(), wh2,
                                     [docs.LineInput(env["a"], env["u"]["عدد"], Decimal(1))],
                                     dest_warehouse_id=env["wh"])
    with pytest.raises(StocktakeFrozen):  # into the counted warehouse: blocked
        await docs.create_and_post(db, admin, transfer_in)


async def test_admin_override_with_pin_and_ai_never(db, admin, env):
    draft = await docs.create_document(db, admin, _issue(env))
    await st.create_stocktake(db, admin, env["wh"])
    with pytest.raises(ApprovalError):
        await protected.approve(db, admin.as_ai(), ProtectedAction.STOCKTAKE_OVERRIDE, "admin", ADMIN_PIN)
    approval = await protected.approve(db, admin, ProtectedAction.STOCKTAKE_OVERRIDE, "admin", ADMIN_PIN)
    await docs.post_document(db, admin, draft, approval=approval)
    assert (await docs.get_document(db, admin, draft)).status is DocStatus.POSTED
    from sqlalchemy import select

    from caspian.db.models import AuditLog

    async with db.session() as s:
        row = await s.scalar(select(AuditLog).where(AuditLog.action == "document.posted"
                                                    ).order_by(AuditLog.id.desc()))
    assert row.approved_by_id is not None and row.details["stocktake_override"]


async def test_approving_the_stocktake_itself_is_not_blocked(db, admin, env):
    sid = await st.create_stocktake(db, admin, env["wh"])
    await _counts(db, admin, sid, **{"1001": "9", "1002": "5", "2001": "0"})
    await st.submit_counts(db, admin, sid)
    assert await st.approve(db, admin, sid) is not None
    await docs.create_and_post(db, admin, _issue(env))  # frozen no more


# ----- carton barcodes (#8) -----


async def test_scan_returns_unit_factor(db, admin, env):
    sid = await st.create_stocktake(db, admin, env["wh"])
    sheet = await st.count_sheet(db, admin, sid)
    piece = await st.scan(db, sid, "111")
    carton = await st.scan(db, sid, "111-C")
    assert (piece.line_id, piece.factor) == (sheet.lines[0].id, Decimal(1))
    assert (carton.line_id, carton.factor, carton.unit_name) == (sheet.lines[0].id, Decimal(12), "کارتن")
    by_code = await st.scan(db, sid, "۱۰۰۱")
    assert by_code.factor == Decimal(1)
    assert await st.scan(db, sid, "nope") is None
