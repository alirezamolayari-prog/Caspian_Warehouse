"""Stock code paths the test report listed as untested (its last table)."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.exc import StaleDataError

from caspian.db.models import DocStatus, DocType, PersonKind
from caspian.services import documents, items, master, protected, stocktake
from caspian.services.errors import ConcurrencyError, ServiceError, ValidationError
from caspian.services.items import ItemInput
from caspian.services.protected import ProtectedAction
from conftest import ADMIN_PIN


@pytest.fixture
async def shop(db, admin):
    units = {u.name: u.id for u in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    wh2 = await master.save_warehouse(db, admin, "W2", "انبار دوم")
    drill = await items.create_item(db, admin, ItemInput("7001", "دریل", units["عدد"], is_returnable=True))
    await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(drill, units["عدد"], Decimal(5))]))
    person = await master.save_person(db, admin, "رضا", PersonKind.EMPLOYEE)
    return {"unit": units["عدد"], "wh": wh, "wh2": wh2, "drill": drill, "person": person}


def _line(shop, qty):
    return [documents.LineInput(shop["drill"], shop["unit"], Decimal(qty))]


async def test_check_lists_a_loan_return_above_what_is_outstanding(db, admin, shop):
    loan = await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.LOAN_OUT, dt.date.today(), shop["wh"], _line(shop, 2), person_id=shop["person"]))
    data = documents.DocumentInput(DocType.LOAN_RETURN, dt.date.today(), shop["wh"], _line(shop, 3),
                                   person_id=shop["person"], related_document_id=loan)
    problems = await documents.check(db, admin, data)
    assert [(p.field, "مانده امانی" in p.message) for p in problems] == [("line:1", True)]
    data.lines = _line(shop, 2)
    assert await documents.check(db, admin, data) == []


async def test_check_of_a_transfer_counts_both_warehouses(db, admin, shop):
    ok = documents.DocumentInput(DocType.TRANSFER, dt.date.today(), shop["wh"], _line(shop, 5),
                                 dest_warehouse_id=shop["wh2"])
    assert await documents.check(db, admin, ok) == []
    too_much = documents.DocumentInput(DocType.TRANSFER, dt.date.today(), shop["wh"], _line(shop, 6),
                                       dest_warehouse_id=shop["wh2"])
    [problem] = await documents.check(db, admin, too_much)
    assert "کافی نیست" in problem.message and problem.field == "line:1"


async def test_post_with_pin_on_a_deadlock_is_not_retried(db, admin, shop, monkeypatch):
    draft = await documents.create_document(db, admin, documents.DocumentInput(
        DocType.ISSUE, dt.date.today(), shop["wh"], _line(shop, 1), person_id=shop["person"]))
    approval = await protected.approve(db, admin, ProtectedAction.STOCKTAKE_OVERRIDE, "admin", ADMIN_PIN)
    calls = []

    async def deadlock(*_args, **_kwargs):
        calls.append(1)
        raise OperationalError("UPDATE …", {}, Exception(1213, "Deadlock found"))

    monkeypatch.setattr(documents, "post_document_in", deadlock)
    with pytest.raises(ValidationError) as exc:
        await documents.post_document(db, admin, draft, approval=approval)
    assert exc.value.message == documents.CONFLICT_MESSAGE and calls == [1]  # the PIN is single-use
    assert (await documents.get_document(db, admin, draft)).status == DocStatus.DRAFT


async def test_cancel_with_pin_changed_meanwhile_reports_a_conflict(db, admin, shop, monkeypatch):
    doc = await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.ISSUE, dt.date.today(), shop["wh"], _line(shop, 1), person_id=shop["person"]))
    approval = await protected.approve(db, admin, ProtectedAction.STOCKTAKE_OVERRIDE, "admin", ADMIN_PIN)

    async def stale(*_args, **_kwargs):
        raise StaleDataError("changed by another PC")

    monkeypatch.setattr(documents, "_cancel_document", stale)
    with pytest.raises(ConcurrencyError):
        await documents.cancel_document(db, admin, doc, "اشتباه", approval)


async def test_stocktake_refuses_negative_and_fractional_counts(db, admin, shop):
    st = await stocktake.create_stocktake(db, admin, shop["wh"])
    sheet = await stocktake.count_sheet(db, admin, st)
    line = sheet.lines[0].id
    for qty in (Decimal(-1), Decimal("1.5")):
        with pytest.raises(ServiceError) as exc:
            await stocktake.record_counts(db, admin, st, {line: (qty, "")})
        assert "ردیف ۱" in exc.value.message
    await stocktake.record_counts(db, admin, st, {line: (Decimal(4), "یکی کم")})
    assert (await stocktake.count_sheet(db, admin, st)).lines[0].counted_qty == 4
