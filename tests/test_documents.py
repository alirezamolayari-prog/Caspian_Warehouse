import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from caspian.db.models import AppSetting, DocStatus, DocType, PersonKind, StockLedger
from caspian.services import auth, items, master, protected, users
from caspian.services import documents as docs
from caspian.services.documents import DocumentInput, LineInput
from caspian.services.errors import ConcurrencyError, PermissionDenied, ValidationError
from caspian.services.items import ItemInput
from caspian.services.protected import ProtectedAction

TODAY = dt.date(2026, 9, 27)  # 1405/07/05


@pytest.fixture
async def env(db, admin):
    u = {x.name: x.id for x in await master.list_units(db)}
    wh1 = (await master.list_warehouses(db))[0].id
    wh2 = await master.save_warehouse(db, admin, "02", "انبار دوم")
    drill = await items.create_item(db, admin, ItemInput(
        "1001", "دریل", u["عدد"], units=[(u["جعبه"], Decimal(24))]))
    pallet = await items.create_item(db, admin, ItemInput(
        "2001", "پالت چوبی", u["عدد"], is_returnable=True))
    person = await master.save_person(db, admin, "علی رضایی", PersonKind.EMPLOYEE)
    return {"u": u, "wh1": wh1, "wh2": wh2, "drill": drill, "pallet": pallet,
            "person": person, "admin": admin, "db": db}


def doc(doc_type, wh, *lines, **kw) -> DocumentInput:
    return DocumentInput(doc_type=doc_type, doc_date=kw.pop("date", TODAY), warehouse_id=wh,
                         lines=[LineInput(*ln) for ln in lines], **kw)


async def balance(db, item_id, wh_id=None) -> Decimal:
    rows = await docs.stock_by_warehouse(db, item_id)
    if wh_id is None:
        return sum((q for _, q in rows), Decimal(0))
    wh_names = {w.id: w.name for w in await master.list_warehouses(db, include_inactive=True)}
    return dict(rows).get(wh_names[wh_id], Decimal(0))


async def receive(e, qty=Decimal(2), unit="جعبه", item="drill", wh="wh1") -> int:
    return await docs.create_and_post(e["db"], e["admin"], doc(
        DocType.RECEIPT, e[wh], (e[item], e["u"][unit], qty)))


async def test_receipt_converts_units_and_posts(env):
    db, admin = env["db"], env["admin"]
    doc_id = await receive(env)  # 2 boxes = 48 pieces
    assert await balance(db, env["drill"]) == Decimal(48)
    detail = await docs.get_document(db, admin, doc_id)
    assert detail.status is DocStatus.POSTED
    assert detail.fiscal_year == 1405 and detail.number == 1
    assert detail.lines[0].base_qty == Decimal(48) and detail.lines[0].unit_name == "جعبه"
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(StockLedger)) == 1


async def test_numbering_per_type_and_year(env):
    db, admin = env["db"], env["admin"]
    await receive(env)
    await receive(env)
    issue = await docs.create_document(db, admin, doc(
        DocType.ISSUE, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1))))
    next_year = await docs.create_document(db, admin, doc(
        DocType.RECEIPT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1)),
        date=dt.date(2027, 3, 22)))
    numbers = {r.id: (r.doc_type, r.fiscal_year, r.number)
               for r in await docs.list_documents(db, admin)}
    assert numbers[issue] == (DocType.ISSUE, 1405, 1)
    assert numbers[next_year] == (DocType.RECEIPT, 1406, 1)


async def test_issue_cannot_exceed_stock(env):
    db, admin = env["db"], env["admin"]
    await receive(env, Decimal(10), "عدد")
    doc_id = await docs.create_document(db, admin, doc(
        DocType.ISSUE, env["wh1"], (env["drill"], env["u"]["جعبه"], Decimal(1))))  # 24 > 10
    with pytest.raises(ValidationError, match="کافی نیست"):
        await docs.post_document(db, admin, doc_id)
    assert (await docs.get_document(db, admin, doc_id)).status is DocStatus.DRAFT
    assert await balance(db, env["drill"]) == Decimal(10)
    async with db.session() as s:
        s.add(AppSetting(key="allow_negative_stock", value=True))
    await docs.post_document(db, admin, doc_id)
    assert await balance(db, env["drill"]) == Decimal(-14)


async def test_transfer(env):
    db, admin = env["db"], env["admin"]
    await receive(env, Decimal(30), "عدد")
    await docs.create_and_post(db, admin, doc(
        DocType.TRANSFER, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(12)),
        dest_warehouse_id=env["wh2"]))
    assert await balance(db, env["drill"], env["wh1"]) == Decimal(18)
    assert await balance(db, env["drill"], env["wh2"]) == Decimal(12)
    with pytest.raises(ValidationError, match="یکسان"):
        await docs.create_document(db, admin, doc(
            DocType.TRANSFER, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1)),
            dest_warehouse_id=env["wh1"]))


async def test_adjustment_signed_lines(env):
    db, admin = env["db"], env["admin"]
    await receive(env, Decimal(5), "عدد")
    await docs.create_and_post(db, admin, doc(
        DocType.ADJUSTMENT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(-2))))
    assert await balance(db, env["drill"]) == Decimal(3)
    with pytest.raises(ValidationError, match="بزرگ‌تر از صفر"):
        await docs.create_document(db, admin, doc(
            DocType.ISSUE, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(-1))))


async def test_line_validation(env):
    db, admin = env["db"], env["admin"]
    bad = [
        doc(DocType.RECEIPT, env["wh1"]),  # no lines
        doc(DocType.RECEIPT, env["wh1"], (env["drill"], env["u"]["متر"], Decimal(1))),
        doc(DocType.RECEIPT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(0))),
        doc(DocType.RECEIPT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1),
                                          Decimal(-5))),
        doc(DocType.RECEIPT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1)),
            dest_warehouse_id=env["wh2"]),
    ]
    for data in bad:
        with pytest.raises(ValidationError):
            await docs.create_document(db, admin, data)


async def test_edit_delete_only_drafts_and_concurrency(env):
    db, admin = env["db"], env["admin"]
    data = doc(DocType.RECEIPT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1)))
    doc_id = await docs.create_document(db, admin, data)
    detail = await docs.get_document(db, admin, doc_id)
    data.lines.append(LineInput(env["drill"], env["u"]["جعبه"], Decimal(1)))
    await docs.update_document(db, admin, doc_id, detail.version_id, data)
    with pytest.raises(ConcurrencyError):
        await docs.update_document(db, admin, doc_id, detail.version_id, data)
    detail = await docs.get_document(db, admin, doc_id)
    assert [ln.base_qty for ln in detail.lines] == [Decimal(1), Decimal(24)]
    await docs.post_document(db, admin, doc_id)
    with pytest.raises(ValidationError):
        await docs.update_document(db, admin, doc_id, detail.version_id + 1, data)
    with pytest.raises(ValidationError):
        await docs.delete_draft(db, admin, doc_id)

    draft = await docs.create_document(db, admin, data)
    await docs.delete_draft(db, admin, draft)
    assert draft not in {r.id for r in await docs.list_documents(db, admin)}


async def test_cancel_reverses_and_guards_negative(env):
    db, admin = env["db"], env["admin"]
    receipt = await receive(env, Decimal(10), "عدد")
    await docs.create_and_post(db, admin, doc(
        DocType.ISSUE, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(8))))
    with pytest.raises(ValidationError, match="کافی نیست"):
        await docs.cancel_document(db, admin, receipt)  # would leave -8
    issue = next(r.id for r in await docs.list_documents(db, admin, DocType.ISSUE))
    await docs.cancel_document(db, admin, issue, "اشتباه ثبت شد")
    assert await balance(db, env["drill"]) == Decimal(10)
    await docs.cancel_document(db, admin, receipt)
    assert await balance(db, env["drill"]) == Decimal(0)
    async with db.session() as s:  # history kept: 2 postings + 2 reversals
        assert await s.scalar(select(func.count()).select_from(StockLedger)) == 4
    with pytest.raises(ValidationError):
        await docs.cancel_document(db, admin, receipt)


async def test_post_revalidates_inactive_item(env):
    db, admin = env["db"], env["admin"]
    doc_id = await docs.create_document(db, admin, doc(
        DocType.RECEIPT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1))))
    approval = await protected.approve(db, admin, ProtectedAction.DEACTIVATE_ITEM, "admin",
                                       "4826")
    await items.set_item_active(db, admin, env["drill"], False, approval)
    with pytest.raises(ValidationError, match="غیرفعال"):
        await docs.post_document(db, admin, doc_id)


async def test_loans_lifecycle(env):
    db, admin = env["db"], env["admin"]
    await receive(env, Decimal(10), "عدد", item="pallet")
    with pytest.raises(ValidationError, match="امانی/برگشتی"):
        await docs.create_document(db, admin, doc(
            DocType.LOAN_OUT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1)),
            person_id=env["person"]))
    with pytest.raises(ValidationError, match="تحویل‌گیرنده"):
        await docs.create_document(db, admin, doc(
            DocType.LOAN_OUT, env["wh1"], (env["pallet"], env["u"]["عدد"], Decimal(1))))
    loan = await docs.create_and_post(db, admin, doc(
        DocType.LOAN_OUT, env["wh1"], (env["pallet"], env["u"]["عدد"], Decimal(6)),
        person_id=env["person"]))
    assert await balance(db, env["pallet"]) == Decimal(4)
    [row] = await docs.outstanding_loans(db, admin)
    assert (row.outstanding, row.person, row.item_name) == (Decimal(6), "علی رضایی", "پالت چوبی")

    ret = doc(DocType.LOAN_RETURN, env["wh1"], (env["pallet"], env["u"]["عدد"], Decimal(4)),
              person_id=env["person"], related_document_id=loan)
    first_return = await docs.create_and_post(db, admin, ret)
    [row] = await docs.outstanding_loans(db, admin)
    assert row.outstanding == Decimal(2)
    assert await balance(db, env["pallet"]) == Decimal(8)

    too_much = await docs.create_document(db, admin, ret)  # 4 more but only 2 outstanding
    with pytest.raises(ValidationError, match="مانده امانی"):
        await docs.post_document(db, admin, too_much)
    with pytest.raises(ValidationError, match="برگشت‌ها"):
        await docs.cancel_document(db, admin, loan)

    await docs.cancel_document(db, admin, first_return)
    assert (await docs.outstanding_loans(db, admin))[0].outstanding == Decimal(6)

    other = await master.save_person(db, admin, "سارا", PersonKind.EMPLOYEE)
    with pytest.raises(ValidationError, match="یکسان نیست"):
        await docs.create_document(db, admin, doc(
            DocType.LOAN_RETURN, env["wh1"], (env["pallet"], env["u"]["عدد"], Decimal(1)),
            person_id=other, related_document_id=loan))


async def test_permissions(env):
    db, admin = env["db"], env["admin"]
    await users.create_user(db, admin, "neda", "", "Neda#2026", "viewer")
    viewer = (await auth.login(db, "neda", "Neda#2026")).actor
    await docs.list_documents(db, viewer)
    with pytest.raises(PermissionDenied):
        await docs.create_document(db, viewer, doc(
            DocType.RECEIPT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1))))


async def test_list_filters_and_search(env):
    db, admin = env["db"], env["admin"]
    await receive(env)
    await receive(env, Decimal(1), "عدد", item="pallet")
    await docs.create_and_post(db, admin, doc(
        DocType.LOAN_OUT, env["wh1"], (env["pallet"], env["u"]["عدد"], Decimal(1)),
        person_id=env["person"], description="برای نمایشگاه"))
    assert len(await docs.list_documents(db, admin, DocType.RECEIPT)) == 2
    assert [r.doc_type for r in await docs.list_documents(db, admin, query="علي")] == \
        [DocType.LOAN_OUT]
    assert len(await docs.list_documents(db, admin, query="نمایشگاه")) == 1
    assert len(await docs.list_documents(db, admin, date_from=TODAY + dt.timedelta(1))) == 0


async def test_number_collision_retries(env, monkeypatch):
    """Simulate another PC grabbing the same number between our read and our insert."""
    db, admin = env["db"], env["admin"]
    await receive(env)
    real_next = docs._next_number
    calls = []

    async def stale_next(s, doc_type, year):
        calls.append(1)
        return 1 if len(calls) == 1 else await real_next(s, doc_type, year)  # first: stale value

    monkeypatch.setattr(docs, "_next_number", stale_next)
    doc_id = await docs.create_document(db, admin, doc(
        DocType.RECEIPT, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(1))))
    assert len(calls) == 2
    assert (await docs.get_document(db, admin, doc_id)).number == 2


# ----- printing (#4, #5) -----


async def test_print_sheet_has_everything_for_the_form(env):
    db, admin = env["db"], env["admin"]
    await receive(env, Decimal(1))  # 24 pieces in stock
    doc_id = await docs.create_and_post(db, admin, doc(
        DocType.ISSUE, env["wh1"], (env["drill"], env["u"]["عدد"], Decimal(3), Decimal(1000), "یدکی"),
        person_id=env["person"], description="برای کارگاه"))
    sheet = await docs.print_sheet(db, admin, doc_id)
    assert sheet.number_text == "ح-۱" and sheet.type_name == "حواله خروج"
    assert sheet.person == "علی رضایی" and sheet.person_label == "تحویل‌گیرنده"
    assert sheet.warehouse and sheet.description == "برای کارگاه"
    assert [(ln.code, ln.name, ln.unit, ln.qty, ln.notes) for ln in sheet.lines] == [
        ("1001", "دریل", "عدد", Decimal(3), "یدکی")]
    assert sheet.lines[0].amount == Decimal(3000) and sheet.total_amount == Decimal(3000)
    assert sheet.company == "بازار مبلمان کاسپین"
    assert sheet.print_count == 0 and sheet.trackable


async def test_record_print_counts_copies_and_audits(env):
    db, admin = env["db"], env["admin"]
    doc_id = await receive(env)
    assert await docs.record_print(db, admin, doc_id, "printer") == 1
    assert await docs.record_print(db, admin, doc_id, "pdf") == 2
    sheet = await docs.print_sheet(db, admin, doc_id)
    assert sheet.print_count == 2 and sheet.last_printed_by == "admin"
    [row] = [r for r in await docs.list_documents(db, admin) if r.id == doc_id]
    assert row.print_count == 2
    from caspian.db.models import AuditLog

    async with db.session() as s:
        actions = (await s.scalars(select(AuditLog.details).where(
            AuditLog.action == "document.printed"))).all()
    assert [a["copy"] for a in actions] == [1, 2] and actions[1]["kind"] == "pdf"


async def test_drafts_are_not_tracked(env):
    db, admin = env["db"], env["admin"]
    draft = await docs.create_document(db, admin, doc(DocType.RECEIPT, env["wh1"],
                                                      (env["drill"], env["u"]["عدد"], Decimal(1))))
    assert not (await docs.print_sheet(db, admin, draft)).trackable
    with pytest.raises(ValidationError):
        await docs.record_print(db, admin, draft, "printer")


def test_number_text_prefixes():
    assert docs.number_text(DocType.RECEIPT, 12) == "ر-۱۲"
    assert len({docs.DOC_PREFIX[t] for t in DocType}) == len(DocType)  # all distinct
