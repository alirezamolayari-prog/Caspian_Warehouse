"""#10: read-only data health check over data that validation would now refuse."""

import datetime as dt
from decimal import Decimal

import time_machine
from sqlalchemy import func, select, update

from caspian.db.models import Document, StockBalance, StockLedger, Unit
from caspian.services import documents, health, items, master
from caspian.services.items import ItemInput


@time_machine.travel(dt.datetime(2026, 10, 3, 10), tick=False)  # «future» stays future
async def test_lists_old_bad_data_without_changing_it(db, admin):
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    ok = await items.create_item(db, admin, ItemInput("1001", "دریل", u["عدد"]))
    cable = await items.create_item(db, admin, ItemInput("2001", "کابل", u["متر"]))
    future = await documents.create_and_post(db, admin, documents.DocumentInput(
        documents.DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(ok, u["عدد"], Decimal(3))]))
    await documents.create_and_post(db, admin, documents.DocumentInput(
        documents.DocType.RECEIPT, dt.date.today(), wh,
        [documents.LineInput(cable, u["متر"], Decimal("2.5"))]))
    assert await health.check(db, admin) == []

    # Data as entered by v1.0.0: a receipt dated 1406/05/01 and 2.5 «عدد» on hand.
    async with db.session() as s:
        await s.execute(update(Document).where(Document.id == future)
                        .values(doc_date=dt.date(2027, 7, 23), fiscal_year=1406))
        await s.execute(update(StockBalance).where(StockBalance.item_id == ok).values(qty=Decimal("2.5")))
    async with db.session() as s:
        counts = (await s.scalar(select(func.count()).select_from(StockLedger)),
                  await s.scalar(select(func.count()).select_from(Document)))
    findings = await health.check(db, admin)
    kinds = sorted(f.kind for f in findings)
    assert kinds == ["fractional_balance", "future_date"]
    future_row = next(f for f in findings if f.kind == "future_date")
    assert "ر-" in future_row.title and "۱۴۰۶/۰۵/۰۱" in future_row.detail
    assert "1001" in next(f for f in findings if f.kind == "fractional_balance").title
    async with db.session() as s:  # read-only
        assert counts == (await s.scalar(select(func.count()).select_from(StockLedger)),
                          await s.scalar(select(func.count()).select_from(Document)))
        assert (await s.get(Unit, u["متر"])).allow_decimal  # meters may be fractional: not reported
