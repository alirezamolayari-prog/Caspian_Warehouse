"""QA round 2, feature A: one item per real product."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from caspian.core.text import normalize
from caspian.db.models import (
    AuditLog,
    DocType,
    ImportKind,
    ImportSource,
    Item,
    StockBalance,
    StockLedger,
)
from caspian.services import documents, health, imports, items, master, protected, reports
from caspian.services import stocktake as st
from caspian.services.errors import ApprovalError, PermissionDenied, SimilarItems, ValidationError
from caspian.services.import_files import RawRow
from caspian.services.items import ItemInput
from caspian.services.protected import ProtectedAction

PIN = "4826"


@pytest.fixture
async def u(db):
    return {x.name: x.id for x in await master.list_units(db)}


async def _item(db, admin, u, code, name, **kw):
    return await items.create_item(db, admin, ItemInput(code, name, u["عدد"], **kw))


@pytest.mark.parametrize(("a", "b", "similar"), [
    ("جارو", "جاروو", True),
    ("جارو", "جارو ", True),
    ("كيف ابزار", "کیف ابزار", True),  # Arabic letters
    ("مایع ظرفشویی", "مايع ظرف‌شویی", True),  # ZWNJ, Arabic yeh
    ("جارو", "جاروی دسته‌بلند", False),
    ("دریل بوش", "دریل ماکیتا", False),
    ("پیچ ۴ در ۴۰", "پیچ ۴ در ۵۰", False),  # sizes differ
    ("کالای آزمایشی شماره 1", "کالای آزمایشی شماره 10", False),
])
def test_similarity_threshold(a, b, similar):
    score = items.similarity(normalize(" ".join(a.split())), normalize(" ".join(b.split())))
    assert (score >= items.SIMILAR_SCORE) is similar, score


async def test_exact_duplicate_is_refused_with_details(db, admin, u):
    await _item(db, admin, u, "1006", "جارو")
    for name in ("جارو", " جارو ", "جارو‌"):
        with pytest.raises(ValidationError, match="همین نام") as caught:
            await _item(db, admin, u, "1017", name)
        assert "1006" in caught.value.message and "موجودی" in caught.value.message
    other = await _item(db, admin, u, "1017", "سطل")
    detail = await items.get_item(db, admin, other)
    data = detail.input
    data.name = "جارو"  # renaming into a duplicate is refused too
    with pytest.raises(ValidationError, match="همین نام"):
        await items.update_item(db, admin, other, detail.version_id, data)


async def test_similar_needs_the_admin_pin_and_the_ai_never(db, admin, u):
    await _item(db, admin, u, "1006", "جارو")
    with pytest.raises(SimilarItems) as caught:
        await _item(db, admin, u, "1017", "جاروو")
    assert [c.code for c in caught.value.candidates] == ["1006"]
    assert await _item(db, admin, u, "1018", "جاروی دسته‌بلند")  # a different product: fine
    with pytest.raises(ApprovalError):
        await items.create_item(db, admin.as_ai(), ItemInput("1019", "جاروو", u["عدد"]),
                                similar_approval=protected.Approval("x", ProtectedAction.CREATE_SIMILAR_ITEM,
                                                                    admin.user_id, admin.user_id, 0))
    approval = await protected.approve(db, admin, ProtectedAction.CREATE_SIMILAR_ITEM, "admin", PIN,
                                       {"reason": "برند دیگر"})
    new = await items.create_item(db, admin, ItemInput("1019", "جاروو", u["عدد"]), similar_approval=approval)
    async with db.session() as s:
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "item.similar_created"))
        granted = await s.scalar(select(AuditLog).where(AuditLog.action == "approval.granted")
                                 .order_by(AuditLog.id.desc()))
    assert entry.entity_id == new and entry.approved_by_id == admin.user_id
    assert granted.details["reason"] == "برند دیگر"


async def test_imports_cannot_create_duplicates(db, admin, u):
    await _item(db, admin, u, "1006", "جارو")
    batch = await imports.create_batch(db, admin, ImportKind.ITEMS, ImportSource.EXCEL,
                                       [RawRow(code="2001", name="سطل"), RawRow(code="2002", name="جاروو")])
    for line in (await imports.get_batch(db, admin, batch)).lines:
        await imports.set_resolution(db, admin, line.id, imports.Resolution.CREATE)
    with pytest.raises(SimilarItems, match="ردیف 2"):
        await imports.apply_batch(db, admin, batch)
    assert await items.search_items(db, admin, "سطل") == []  # nothing written
    approval = await protected.approve(db, admin, ProtectedAction.CREATE_SIMILAR_ITEM, "admin", PIN)
    result = await imports.apply_batch(db, admin, batch, similar_approval=approval)
    assert result.created_items == 2


async def test_merge_moves_history_and_stock(db, admin, u):
    wh = (await master.list_warehouses(db))[0].id
    keep = await _item(db, admin, u, "1006", "جارو", units=[(u["جعبه"], Decimal(10))])
    dupe = await _item(db, admin, u, "1017", "سطل موقت", barcodes=[("626999", None)],
                       units=[(u["کارتن"], Decimal(20))])
    async with db.session() as s:  # legacy duplicate, created before this rule existed
        await s.execute(update(Item).where(Item.id == dupe).values(name="جارو", name_normalized="جارو"))
    for item_id, qty in ((keep, 5), (dupe, 3)):
        await documents.create_and_post(db, admin, documents.DocumentInput(
            DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(item_id, u["عدد"], Decimal(qty))]))
    [finding] = [f for f in await health.check(db, admin) if f.kind == "duplicate_items"]
    assert "۱۰۰۶" in finding.title and "۱۰۱۷" in finding.title
    [group] = await items.duplicate_groups(db, admin)
    assert sorted(g.code for g in group) == ["1006", "1017"]

    with pytest.raises(PermissionDenied):
        await items.merge_items(db, admin.as_ai(), keep, [dupe], None, "x")
    with pytest.raises(ApprovalError):
        await items.merge_items(db, admin, keep, [dupe], None, "تکراری")
    approval = await protected.approve(db, admin, ProtectedAction.MERGE_ITEMS, "admin", PIN)
    result = await items.merge_items(db, admin, keep, [dupe], approval, "تکراری")
    assert result.moved_lines == 1 and result.merged_codes == ["1017"]
    async with db.session() as s:
        assert await s.scalar(select(StockBalance.qty).where(StockBalance.item_id == keep)) == 8
        assert await s.scalar(select(func.count()).select_from(StockLedger)
                              .where(StockLedger.item_id == dupe)) == 0
        old = await s.get(Item, dupe)
        assert not old.is_active and "ادغام‌شده" in old.name
    merged = await items.get_item(db, admin, keep)
    assert ("626999", None) in merged.input.barcodes
    assert {(u["جعبه"], Decimal(10)), (u["کارتن"], Decimal(20))} <= set(merged.input.units)
    assert (await reports.cardex(db, admin, keep)).rows[-1][7] == 8  # cardex follows merged history
    assert [f for f in await health.check(db, admin) if f.kind == "duplicate_items"] == []


async def test_merge_refuses_unit_conflicts_and_open_stocktakes(db, admin, u):
    wh = (await master.list_warehouses(db))[0].id
    keep = await _item(db, admin, u, "1006", "جارو", units=[(u["جعبه"], Decimal(10))])
    dupe = await _item(db, admin, u, "1017", "سطل", units=[(u["جعبه"], Decimal(12))])
    approval = await protected.approve(db, admin, ProtectedAction.MERGE_ITEMS, "admin", PIN)
    with pytest.raises(ValidationError, match="ضریب"):
        await items.merge_items(db, admin, keep, [dupe], approval, "x")
    other = await _item(db, admin, u, "1018", "تی")
    await st.create_stocktake(db, admin, wh)
    approval = await protected.approve(db, admin, ProtectedAction.MERGE_ITEMS, "admin", PIN)
    with pytest.raises(ValidationError, match="انبارگردانی"):
        await items.merge_items(db, admin, keep, [other], approval, "x")
