import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from caspian.db.models import AuditLog, DocType, Document, DocumentLine, StockBalance, Warehouse
from caspian.services import auth, items, master, protected, users
from caspian.services.errors import (
    ApprovalError,
    ConcurrencyError,
    PermissionDenied,
    ValidationError,
)
from caspian.services.items import ItemInput
from caspian.services.protected import ProtectedAction

PIN = "4826"


async def _units(db) -> dict[str, int]:
    return {u.name: u.id for u in await master.list_units(db)}


async def _drill(db, admin, code="1001", name="دریل بوش", **kw) -> int:
    u = await _units(db)
    data = ItemInput(code=code, name=name, base_unit_id=u["عدد"], **kw)
    return await items.create_item(db, admin, data)


async def test_create_and_get_item_with_units_and_barcodes(db, admin):
    u = await _units(db)
    cat = await master.save_category(db, admin, "ابزار برقی")
    item_id = await items.create_item(db, admin, ItemInput(
        code="۲۰۰۱", name="  پیچ   گوشتی  شارژی ", base_unit_id=u["عدد"], category_id=cat,
        reorder_point=Decimal(10), units=[(u["جعبه"], Decimal(24))],
        barcodes=[("۶۲۶۰۰۰۱", None), ("6260002", u["جعبه"])],
    ))
    detail = await items.get_item(db, admin, item_id)
    assert detail.input.code == "2001"  # Persian digits normalized
    assert detail.input.name == "پیچ گوشتی شارژی"
    assert detail.input.units == [(u["جعبه"], Decimal(24))]
    assert sorted(detail.input.barcodes) == [("6260001", None), ("6260002", u["جعبه"])]
    assert detail.is_active and not detail.has_movements
    assert await items.lookup_barcode(db, "6260002") == (item_id, u["جعبه"])
    assert await items.lookup_barcode(db, "nope") is None


async def test_search(db, admin):
    await _drill(db, admin, "1001", "دریل بوش GSB", barcodes=[("111", None)])
    await _drill(db, admin, "1002", "دريل ماكیتا")  # Arabic yeh/kaf
    await _drill(db, admin, "2001", "میز کار")

    async def names(q, **kw):
        return [r.name for r in await items.search_items(db, admin, q, **kw)]

    assert set(await names("دریل")) == {"دریل بوش GSB", "دريل ماكیتا"}
    assert await names("ماکیتا دریل") == ["دريل ماكیتا"]  # word order, normalized letters
    assert await names("gsb") == ["دریل بوش GSB"]
    assert await names("۲۰۰") == ["میز کار"]  # code prefix, Persian digits
    assert await names("111") == ["دریل بوش GSB"]  # exact barcode
    assert len(await names("")) == 3
    assert await names("100%") == []  # LIKE wildcards are escaped


async def test_validation_errors(db, admin):
    u = await _units(db)
    await _drill(db, admin, barcodes=[("555", None)])
    cases = [
        ItemInput(code="", name="x", base_unit_id=u["عدد"]),
        ItemInput(code="9", name=" ", base_unit_id=u["عدد"]),
        ItemInput(code="1001", name="dup code", base_unit_id=u["عدد"]),
        ItemInput(code="9", name="x", base_unit_id=u["عدد"], barcodes=[("555", None)]),
        ItemInput(code="9", name="x", base_unit_id=u["عدد"],
                  units=[(u["جعبه"], Decimal(0))]),
        ItemInput(code="9", name="x", base_unit_id=u["عدد"], units=[(u["عدد"], Decimal(2))]),
        ItemInput(code="9", name="x", base_unit_id=u["عدد"], barcodes=[("7", u["کارتن"])]),
        ItemInput(code="9", name="x", base_unit_id=u["عدد"], reorder_point=Decimal(-1)),
    ]
    for data in cases:
        with pytest.raises(ValidationError):
            await items.create_item(db, admin, data)


async def test_update_with_concurrency_check_and_audit(db, admin):
    u = await _units(db)
    item_id = await _drill(db, admin)
    detail = await items.get_item(db, admin, item_id)
    data = detail.input
    data.units = [(u["جعبه"], Decimal(12))]
    await items.update_item(db, admin, item_id, detail.version_id, data)

    # A second editor still holding the old version is rejected, even though the first
    # edit only touched units.
    with pytest.raises(ConcurrencyError):
        await items.update_item(db, admin, item_id, detail.version_id, data)

    fresh = await items.get_item(db, admin, item_id)
    assert fresh.input.units == [(u["جعبه"], Decimal(12))]
    async with db.session() as s:
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "item.updated"))
        assert "units" in entry.details


async def _add_movement(db, item_id, unit_id):
    async with db.session() as s:
        wh = await s.scalar(select(Warehouse))
        doc = Document(doc_type=DocType.RECEIPT, fiscal_year=1405, number=1,
                       doc_date=dt.date(2026, 9, 27), warehouse_id=wh.id)
        doc.lines.append(DocumentLine(line_no=1, item_id=item_id, unit_id=unit_id,
                                      qty=Decimal(5), base_qty=Decimal(5)))
        s.add(doc)
        s.add(StockBalance(item_id=item_id, warehouse_id=wh.id, qty=Decimal(5)))


async def test_base_unit_locked_after_movement(db, admin):
    u = await _units(db)
    item_id = await _drill(db, admin)
    await _add_movement(db, item_id, u["عدد"])
    detail = await items.get_item(db, admin, item_id)
    assert detail.has_movements
    data = detail.input
    data.base_unit_id = u["متر"]
    with pytest.raises(ValidationError, match="واحد اصلی"):
        await items.update_item(db, admin, item_id, detail.version_id, data)


async def test_on_hand_and_reorder(db, admin):
    u = await _units(db)
    item_id = await _drill(db, admin, reorder_point=Decimal(5))
    await _drill(db, admin, "1002", "میز", reorder_point=Decimal(1))
    await _add_movement(db, item_id, u["عدد"])
    rows = {r.code: r for r in await items.search_items(db, admin)}
    assert rows["1001"].on_hand == Decimal(5) and rows["1001"].below_reorder
    assert rows["1002"].on_hand == 0 and rows["1002"].below_reorder
    assert await items.count_summary(db) == (2, 2)
    low = await items.search_items(db, admin, only_below_reorder=True)
    assert len(low) == 2


async def test_deactivate_requires_pin_and_hides_item(db, admin):
    item_id = await _drill(db, admin)
    with pytest.raises(ApprovalError):
        await items.set_item_active(db, admin, item_id, False)
    approval = await protected.approve(db, admin, ProtectedAction.DEACTIVATE_ITEM, "admin", PIN)
    await items.set_item_active(db, admin, item_id, False, approval)
    assert await items.search_items(db, admin) == []
    assert len(await items.search_items(db, admin, include_inactive=True)) == 1
    await items.set_item_active(db, admin, item_id, True)  # reactivation needs no PIN
    assert len(await items.search_items(db, admin)) == 1


async def test_delete_rules(db, admin):
    u = await _units(db)
    used = await _drill(db, admin, "1001")
    unused = await _drill(db, admin, "1002", "میز", barcodes=[("999", None)])
    await _add_movement(db, used, u["عدد"])
    approval = await protected.approve(db, admin, ProtectedAction.DELETE_ITEM, "admin", PIN)
    with pytest.raises(ValidationError, match="غیرفعال"):
        await items.delete_item(db, admin, used, approval)
    with pytest.raises(ApprovalError):
        await items.delete_item(db, admin, unused, None)
    approval = await protected.approve(db, admin, ProtectedAction.DELETE_ITEM, "admin", PIN)
    await items.delete_item(db, admin, unused, approval)
    assert await items.lookup_barcode(db, "999") is None
    assert [r.code for r in await items.search_items(db, admin, include_inactive=True)] == ["1001"]


async def test_viewer_cannot_edit(db, admin):
    await users.create_user(db, admin, "neda", "", "Neda#2026", "viewer")
    viewer = (await auth.login(db, "neda", "Neda#2026")).actor
    await items.search_items(db, viewer)
    with pytest.raises(PermissionDenied):
        await _drill(db, viewer)


async def test_next_code(db, admin):
    assert await items.next_code(db) == "1001"
    await _drill(db, admin, "1050")
    await _drill(db, admin, "A-7", "x")
    assert await items.next_code(db) == "1051"


# ----- opening stock on the new-item form (#12) -----


async def test_opening_stock_goes_through_an_opening_document(db, admin):
    """Stock only changes through documents: the item form creates a posted OPENING document."""
    from caspian.db.models import DocStatus
    from caspian.services import documents as docs
    from caspian.services import reports

    u = await _units(db)
    wh = (await master.list_warehouses(db))[0].id
    result = await items.create_item_with_opening(
        db, admin, ItemInput(code="3001", name="میز اداری", base_unit_id=u["عدد"]),
        items.OpeningStock(wh, Decimal(7), Decimal(1_200_000)))
    assert result.posted and result.document_id is not None
    doc = await docs.get_document(db, admin, result.document_id)
    assert doc.input.doc_type is DocType.OPENING and doc.status is DocStatus.POSTED
    assert [(ln.item_id, ln.base_qty, ln.unit_price) for ln in doc.lines] == [
        (result.item_id, Decimal(7), Decimal(1_200_000))]
    assert (await docs.stock_by_warehouse(db, result.item_id))[0][1] == Decimal(7)
    cardex = await reports.cardex(db, admin, result.item_id)
    assert cardex.rows[-1][7] == Decimal(7) and "موجودی اول دوره" in cardex.rows[-1][1]


async def test_opening_stock_without_post_permission_stays_draft(db, admin):
    from caspian.db.models import DocStatus
    from caspian.services import documents as docs

    u = await _units(db)
    wh = (await master.list_warehouses(db))[0].id
    from caspian.core.permissions import Perm
    from caspian.services.actor import Actor

    clerk = Actor(admin.user_id, "admin", "ثبت‌کننده", "custom", frozenset(p.value for p in (
        Perm.ITEMS_VIEW, Perm.ITEMS_EDIT, Perm.DOCUMENTS_VIEW, Perm.DOCUMENTS_EDIT)))
    result = await items.create_item_with_opening(
        db, clerk, ItemInput(code="3002", name="صندلی", base_unit_id=u["عدد"]),
        items.OpeningStock(wh, Decimal(3)))
    assert not result.posted
    assert (await docs.get_document(db, admin, result.document_id)).status is DocStatus.DRAFT
    assert await docs.stock_by_warehouse(db, result.item_id) == []


async def test_no_opening_quantity_means_no_document(db, admin):
    u = await _units(db)
    result = await items.create_item_with_opening(
        db, admin, ItemInput(code="3003", name="کمد", base_unit_id=u["عدد"]), None)
    assert result.document_id is None and not result.posted
    wh = (await master.list_warehouses(db))[0].id
    with pytest.raises(ValidationError, match="بزرگ‌تر از صفر"):
        await items.create_item_with_opening(
            db, admin, ItemInput(code="3004", name="قفسه", base_unit_id=u["عدد"]),
            items.OpeningStock(wh, Decimal(0)))
    assert await items.search_items(db, admin, "قفسه") == []  # all or nothing
