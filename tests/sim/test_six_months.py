"""Six simulated months of warehouse work (QA round 2 #4). Slow: run with CASPIAN_SLOW_TESTS=1.

Daily receipts / issues / transfers / adjustments / loans / returns / occasional cancels across two
warehouses and ~120 items, concurrent issues, monthly blind stocktakes, weekly backups, Nowruz 1406,
the fiscal year-end close of 1405 with open loans carried over, returns after the close, and a backup
restore at the end. Invariants are checked every simulated fortnight.
"""

import asyncio
import datetime as dt
import os
import random
import time
from decimal import Decimal

import pytest
import time_machine
from sqlalchemy import func, select

from caspian.db.models import DocStatus, DocType, Document, PersonKind, StockBalance, StockLedger
from caspian.services import backup, documents, fiscal, health, items, master, protected
from caspian.services import stocktake as st
from caspian.services.errors import ValidationError
from caspian.services.fiscal import CloseOptions
from caspian.services.items import ItemInput

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.environ.get("CASPIAN_SLOW_TESTS") != "1", reason="set CASPIAN_SLOW_TESTS=1"),
]

START = dt.date(2026, 10, 1)  # 1405/07/09
DAYS = 200  # ends in Farvardin/Ordibehesht 1406, after Nowruz (2027-03-21)
CLOSE_ON = dt.date(2027, 4, 5)  # 1406/01/16: close fiscal 1405
PASSWORD = "Backup#Pass2026"
PIN = "4826"
D = Decimal


class Sim:
    def __init__(self, db, admin, tmp_path):
        self.db, self.admin, self.tmp = db, admin, tmp_path
        self.rng = random.Random(1405)
        self.dumper = backup.SqliteDumper(db.url.database)
        self.timings: dict[str, float] = {}
        self.counts = {"documents": 0, "rejected": 0, "cancelled": 0, "stocktakes": 0, "backups": 0}

    async def setup(self):
        db, admin = self.db, self.admin
        self.unit = {u.name: u.id for u in await master.list_units(db)}["عدد"]
        self.wh = [(await master.list_warehouses(db))[0].id,
                   await master.save_warehouse(db, admin, "02", "انبار دوم")]
        self.items = [await items.create_item(db, admin, ItemInput(
            f"{1000 + i}", f"کالای آزمایشی شماره {i}", self.unit, reorder_point=D(5)))
            for i in range(110)]
        self.returnable = [await items.create_item(db, admin, ItemInput(
            f"{2000 + i}", f"ابزار امانی {i}", self.unit, is_returnable=True)) for i in range(10)]
        self.people = [await master.save_person(db, admin, f"شخص {i}", PersonKind.EMPLOYEE)
                       for i in range(8)]
        await self.post(DocType.OPENING, self.wh[0], [(i, 200) for i in self.items + self.returnable])
        await self.post(DocType.OPENING, self.wh[1], [(i, 50) for i in self.items[:60]])

    async def balance(self, item, wh) -> Decimal:
        async with self.db.session() as s:
            return await s.scalar(select(StockBalance.qty).where(
                StockBalance.item_id == item, StockBalance.warehouse_id == wh)) or D(0)

    async def post(self, doc_type, wh, lines, **kw):
        try:
            doc = await documents.create_and_post(self.db, self.admin, documents.DocumentInput(
                doc_type, dt.date.today(), wh, [documents.LineInput(i, self.unit, D(q)) for i, q in lines],
                **kw))
        except ValidationError:
            self.counts["rejected"] += 1  # e.g. not enough stock: refused, never oversold
            return None  # the refused document stays a draft; see end_of_day()
        self.counts["documents"] += 1
        return doc

    async def end_of_day(self):
        """Refused documents stay as drafts (work is never lost); the user deletes them."""
        for row in await documents.list_documents(self.db, self.admin, status=DocStatus.DRAFT):
            await documents.delete_draft(self.db, self.admin, row.id)

    async def day(self, n: int):
        rng = self.rng
        wh = rng.choice(self.wh)
        await self.post(DocType.RECEIPT, wh, [(i, rng.randint(1, 20)) for i in rng.sample(self.items, 3)],
                        person_id=rng.choice(self.people))
        for _ in range(rng.randint(1, 3)):
            item = rng.choice(self.items)
            have = int(await self.balance(item, wh))
            if have > 0:
                await self.post(DocType.ISSUE, wh, [(item, rng.randint(1, min(have, 8)))],
                                person_id=rng.choice(self.people))
        if n % 3 == 0:  # concurrent issues from several PCs, some deliberately too large
            item = rng.choice(self.items[:60])
            await asyncio.gather(*(self.post(DocType.ISSUE, self.wh[0], [(item, rng.randint(1, 40))],
                                             person_id=rng.choice(self.people)) for _ in range(4)))
        if n % 4 == 0:
            item = rng.choice(self.items[:60])
            have = int(await self.balance(item, self.wh[0]))
            if have > 1:
                await self.post(DocType.TRANSFER, self.wh[0], [(item, rng.randint(1, have // 2))],
                                dest_warehouse_id=self.wh[1])
        if n % 9 == 0:
            item = rng.choice(self.items)
            delta = rng.choice([-1, 1, 2])
            if delta > 0 or await self.balance(item, wh) >= 1:
                await self.post(DocType.ADJUSTMENT, wh, [(item, delta)])
        if n % 5 == 0:
            tool = rng.choice(self.returnable)
            if await self.balance(tool, self.wh[0]) >= 2:
                await self.post(DocType.LOAN_OUT, self.wh[0], [(tool, 2)], person_id=rng.choice(self.people))
        if n % 7 == 3:
            await self.return_some(limit=2, min_days=20)  # recent loans stay out
        if n % 15 == 0:  # occasionally a mistake is cancelled
            rows = await documents.list_documents(self.db, self.admin, DocType.RECEIPT, limit=3)
            posted = [r for r in rows if r.status.value == "POSTED"]
            if posted:
                try:
                    await documents.cancel_document(self.db, self.admin, posted[0].id, "اشتباه")
                    self.counts["cancelled"] += 1
                except ValidationError:
                    self.counts["rejected"] += 1

    async def return_some(self, limit=None, min_days=0):
        loans = [ln for ln in await documents.outstanding_loans(self.db, self.admin)
                 if ln.days_out >= min_days]
        for loan in loans[:limit] if limit else loans:
            await self.post(DocType.LOAN_RETURN, self.wh[0], [(loan.item_id, int(loan.outstanding))],
                            person_id=loan.person_id, related_document_id=loan.document_id)

    async def stocktake(self):
        started = time.perf_counter()
        sid = await st.create_stocktake(self.db, self.admin, self.wh[1])
        sheet = await st.count_sheet(self.db, self.admin, sid)
        counts = {}
        for ln in sheet.lines:
            real = int(await self.balance(ln.item_id, self.wh[1]))
            counts[ln.id] = (D(max(real + self.rng.choice([0, 0, 0, -1, 1]), 0)), "")
        await st.record_counts(self.db, self.admin, sid, counts)
        await st.submit_counts(self.db, self.admin, sid)
        await st.approve(self.db, self.admin, sid)
        self.counts["stocktakes"] += 1
        self.timings["stocktake"] = time.perf_counter() - started

    async def weekly_backup(self):
        info = await backup.create_backup(self.db, self.admin, self.dumper, PASSWORD, self.tmp / "bk")
        self.counts["backups"] += 1
        return info

    async def check_invariants(self):
        async with self.db.session() as s:
            ledger = dict(((i, w), q) for i, w, q in (await s.execute(
                select(StockLedger.item_id, StockLedger.warehouse_id, func.sum(StockLedger.qty_change))
                .group_by(StockLedger.item_id, StockLedger.warehouse_id))).all())
            balances = {(b.item_id, b.warehouse_id): b.qty
                        for b in (await s.scalars(select(StockBalance))).all()}
            dupes = (await s.execute(select(Document.doc_type, Document.fiscal_year, Document.number,
                                            func.count()).group_by(Document.doc_type, Document.fiscal_year,
                                                                   Document.number)
                                     .having(func.count() > 1))).all()
        for key in set(ledger) | set(balances):
            assert balances.get(key, D(0)) == ledger.get(key, D(0)), key
        assert min(balances.values(), default=D(0)) >= 0
        assert dupes == []
        assert await health.check(self.db, self.admin) == []


async def test_six_months(db, admin, tmp_path):
    sim = Sim(db, admin, tmp_path)
    t0 = time.perf_counter()
    with time_machine.travel(dt.datetime.combine(START, dt.time(9)), tick=False) as clock:
        await sim.setup()
        closed = False
        for n in range(DAYS):
            today = START + dt.timedelta(days=n)
            clock.move_to(dt.datetime.combine(today, dt.time(9)))
            if today == CLOSE_ON:  # after Nowruz 1406: close fiscal 1405, open loans carried over
                assert await documents.outstanding_loans(db, admin)  # there are loans to carry
                approval = await protected.approve(db, admin, protected.ProtectedAction.CLOSE_FISCAL_YEAR,
                                                   "admin", PIN)
                started = time.perf_counter()
                result = await fiscal.run_year_end(db, admin, 1405, CloseOptions(), approval, sim.dumper,
                                                   PASSWORD, tmp_path / "bk")
                sim.timings["year_end"] = time.perf_counter() - started
                assert result.carried_loans > 0
                closed = True
                await sim.check_invariants()
                await sim.return_some()  # carried loans come back in the new year
                await sim.check_invariants()
                continue
            if today.day == 1:
                await sim.stocktake()
            await sim.day(n)
            await sim.end_of_day()
            if today.weekday() == 4:
                await sim.weekly_backup()
            if n % 14 == 13:
                await sim.check_invariants()
        assert closed
        # A restore at the end brings back the last backup and everything still adds up.
        info = await sim.weekly_backup()
        await sim.post(DocType.RECEIPT, sim.wh[0], [(sim.items[0], 5)])
        approval = await protected.approve(db, admin, protected.ProtectedAction.RESTORE_BACKUP, "admin", PIN)
        started = time.perf_counter()
        await backup.restore_backup(db, admin, sim.dumper, info.path, PASSWORD, approval, tmp_path / "bk")
        sim.timings["restore"] = time.perf_counter() - started
        await sim.check_invariants()
    sim.timings["total"] = time.perf_counter() - t0
    print(f"\nsix-month simulation: {sim.counts}; timings (s): "
          + ", ".join(f"{k}={v:.1f}" for k, v in sim.timings.items()))
    assert sim.counts["documents"] > 800 and sim.counts["stocktakes"] >= 6
