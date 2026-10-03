"""QA round 2 #1: simultaneous documents get unique numbers and never oversell."""

import asyncio
import datetime as dt
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from caspian.db.base import Base
from caspian.db.bootstrap import prepare
from caspian.db.database import Database
from caspian.db.models import DocType, Document, PersonKind, StockBalance
from caspian.services import auth, documents, items, master
from caspian.services.errors import ValidationError
from caspian.services.items import ItemInput

MARIADB_URL = os.environ.get("CASPIAN_TEST_MARIADB_URL")


async def _scenario(db: Database, admin) -> None:
    unit = (await master.list_units(db))[0].id
    wh = (await master.list_warehouses(db))[0].id
    item = await items.create_item(db, admin, ItemInput("C-1", "میز همزمان", unit))
    person = await master.save_person(db, admin, "گیرنده", PersonKind.EMPLOYEE)
    await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(item, unit, Decimal(10))]))

    def issue(qty):
        return documents.create_and_post(db, admin, documents.DocumentInput(
            DocType.ISSUE, dt.date.today(), wh, [documents.LineInput(item, unit, Decimal(qty))],
            person_id=person))

    # 5 issues of 2 at the same moment: all fit, numbers must be unique, no raw DB error.
    ids = await asyncio.gather(*(issue(2) for _ in range(5)))
    async with db.session() as s:
        numbers = (await s.scalars(select(Document.number).where(Document.doc_type == DocType.ISSUE))).all()
        assert sorted(numbers) == [1, 2, 3, 4, 5] and len(set(ids)) == 5
        assert await s.scalar(select(StockBalance.qty).where(StockBalance.item_id == item)) == 0

    # 5 more at once with nothing left: every one fails as a Persian error, stock never negative.
    results = await asyncio.gather(*(issue(1) for _ in range(5)), return_exceptions=True)
    assert all(isinstance(r, ValidationError) for r in results), results
    async with db.session() as s:
        assert await s.scalar(select(func.min(StockBalance.qty))) >= 0


async def test_concurrent_posting_sqlite(db, admin):
    await _scenario(db, admin)


@pytest.mark.skipif(not MARIADB_URL, reason="CASPIAN_TEST_MARIADB_URL not set")
async def test_concurrent_posting_mariadb():
    db = Database(MARIADB_URL)
    try:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await prepare(db)
        admin = (await auth.login(db, "admin", "admin")).actor
        await _scenario(db, admin)
    finally:
        await db.dispose()
