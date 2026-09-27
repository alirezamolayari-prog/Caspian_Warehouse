import datetime as dt
import os
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from caspian.core.permissions import Perm
from caspian.core.text import normalize
from caspian.db.base import Base
from caspian.db.bootstrap import prepare
from caspian.db.database import Database, DbConfig, describe_error
from caspian.db.migrate import current_revision, head_revision
from caspian.db.models import (
    DocStatus,
    DocType,
    Document,
    DocumentLine,
    Item,
    ItemBarcode,
    ItemUnit,
    Role,
    Unit,
    Warehouse,
)
from caspian.db.seed import DEFAULT_UNITS, seed_reference_data
from caspian.services.errors import ValidationError

# Set to a MariaDB URL (mariadb+aiomysql://user:pass@host/testdb) to also run the
# integration test against a real server. That database will be wiped.
MARIADB_URL = os.environ.get("CASPIAN_TEST_MARIADB_URL")


async def test_migrated_to_head(db):
    assert head_revision() is not None
    assert await current_revision(db) == head_revision()


async def test_migrations_match_models(db):
    """Fails if a model changed without a new Alembic revision."""
    async with db.engine.connect() as conn:
        diff = await conn.run_sync(
            lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata)
        )
    assert diff == []


async def test_seed_is_idempotent(db):
    async with db.session() as s:
        await seed_reference_data(s)
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Role)) == 5
        assert await s.scalar(select(func.count()).select_from(Unit)) == len(DEFAULT_UNITS)
        assert await s.scalar(select(func.count()).select_from(Warehouse)) == 1
        admin = await s.scalar(select(Role).where(Role.code == "admin"))
        assert {p.permission for p in admin.permissions} == {p.value for p in Perm}


async def _make_item(s, code="D-1", name="دریل بوش") -> Item:
    piece = await s.scalar(select(Unit).where(Unit.name == "عدد"))
    box = await s.scalar(select(Unit).where(Unit.name == "جعبه"))
    item = Item(code=code, name=name, name_normalized=normalize(name), base_unit_id=piece.id)
    item.units.append(ItemUnit(unit_id=box.id, factor=Decimal(24)))
    item.barcodes.append(ItemBarcode(barcode=f"626{code}"))
    s.add(item)
    await s.flush()
    return item


async def test_item_with_units_and_document(db):
    async with db.session() as s:
        item = await _make_item(s)
        wh = await s.scalar(select(Warehouse))
        doc = Document(
            doc_type=DocType.RECEIPT, fiscal_year=1405, number=1,
            doc_date=dt.date(2026, 9, 27), warehouse_id=wh.id,
        )
        doc.lines.append(DocumentLine(
            line_no=1, item_id=item.id, unit_id=item.units[0].unit_id,
            qty=Decimal(2), factor=Decimal(24), base_qty=Decimal(48),
        ))
        s.add(doc)

    async with db.session() as s:
        doc = await s.scalar(select(Document))
        assert doc.status is DocStatus.DRAFT
        assert doc.lines[0].base_qty == Decimal(48)
        item = await s.get(Item, doc.lines[0].item_id)
        assert item.units[0].factor == Decimal(24)
        assert item.base_unit.name == "عدد"


async def test_barcode_unique(db):
    async with db.session() as s:
        await _make_item(s, code="A")
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            item = await _make_item(s, code="B")
            item.barcodes.append(ItemBarcode(barcode="626A"))
            await s.flush()


async def test_optimistic_locking(db):
    async with db.session() as s:
        item_id = (await _make_item(s)).id

    async with db.sessionmaker() as a, db.sessionmaker() as b:
        item_a = await a.get(Item, item_id)
        item_b = await b.get(Item, item_id)
        item_a.name = "first"
        await a.commit()
        item_b.name = "second"
        with pytest.raises(StaleDataError):
            await b.commit()


async def test_foreign_keys_enforced(db):
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(Item(code="X", name="x", name_normalized="x", base_unit_id=9999))


def test_db_config_roundtrip():
    cfg = DbConfig(host="192.168.1.10", port=3307, name="caspian", user="app")
    assert DbConfig.from_dict(cfg.to_dict()) == cfg
    assert DbConfig.from_dict({}) is None
    assert not cfg.is_local
    url = cfg.url("p@ss")
    assert url.drivername == "mariadb+aiomysql"
    assert url.password == "p@ss" and url.database == "caspian" and url.port == 3307
    assert cfg.url("x", database="").database is None


def test_describe_error():
    class Fake(Exception):
        pass

    assert "رمز عبور" in describe_error(Fake(1045, "Access denied"))
    assert "MariaDB" in describe_error(ConnectionRefusedError())


@pytest.mark.skipif(not MARIADB_URL, reason="CASPIAN_TEST_MARIADB_URL not set")
async def test_mariadb_full_schema():
    db = Database(MARIADB_URL)
    try:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await prepare(db)
        assert await current_revision(db) == head_revision()
        async with db.engine.connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert {"items", "documents", "stock_ledger", "audit_log"} <= set(tables)
        async with db.session() as s:
            item = await _make_item(s, name="كابينت")
            wh_id = (await s.scalar(select(Warehouse))).id
            box_id = item.units[0].unit_id

        # Full business flow on the real server (row locks, numbering, Persian collation).
        from caspian.services import auth, items
        from caspian.services import documents as docs

        admin = (await auth.login(db, "admin", "admin")).actor
        assert [r.name for r in await items.search_items(db, admin, "کابینت")] == ["كابينت"]
        await docs.create_and_post(db, admin, docs.DocumentInput(
            DocType.RECEIPT, dt.date(2026, 9, 27), wh_id,
            [docs.LineInput(item.id, box_id, Decimal(2))]))
        issue = await docs.create_document(db, admin, docs.DocumentInput(
            DocType.ISSUE, dt.date(2026, 9, 27), wh_id,
            [docs.LineInput(item.id, item.base_unit_id, Decimal(50))]))
        with pytest.raises(ValidationError):
            await docs.post_document(db, admin, issue)  # 50 > 48
        assert (await docs.stock_by_warehouse(db, item.id))[0][1] == Decimal(48)

        from caspian.services import reports

        report = await reports.stock_balance(db, admin)  # window function on MariaDB
        assert report.rows[0][4] == Decimal(48)
        assert (await reports.cardex(db, admin, item.id)).rows[-1][7] == Decimal(48)
    finally:
        await db.dispose()
