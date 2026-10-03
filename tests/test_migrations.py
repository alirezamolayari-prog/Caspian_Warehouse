"""Schema migrations added after v1.0.0 must upgrade, downgrade and upgrade again without losing data."""

import datetime as dt
import os
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from caspian.db.base import Base
from caspian.db.bootstrap import prepare
from caspian.db.database import Database
from caspian.db.migrate import current_revision, downgrade, head_revision, upgrade
from caspian.db.models import DocType, ImportKind, ImportSource
from caspian.services import auth, imports, items, master
from caspian.services import documents as docs
from caspian.services.import_files import RawRow
from caspian.services.items import ItemInput

V1_0_0_HEAD = "986638137277"  # last revision shipped in v1.0.0
MARIADB_URL = os.environ.get("CASPIAN_TEST_MARIADB_URL")


async def _business_data(db: Database) -> dict:
    admin = (await auth.login(db, "admin", "admin")).actor
    unit = (await master.list_units(db))[0].id
    wh = (await master.list_warehouses(db))[0].id
    item = await items.create_item(db, admin, ItemInput("M-1", "کالای مهاجرت", unit))
    doc = await docs.create_and_post(db, admin, docs.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [docs.LineInput(item, unit, Decimal(5))]))
    batch = await imports.create_batch(db, admin, ImportKind.STOCK, ImportSource.SCAN,
                                       [RawRow(code="M-1", qty=Decimal(1))], "b", DocType.RECEIPT, wh)
    draft = (await imports.apply_batch(db, admin, batch)).document_id
    return {"admin": admin, "item": item, "doc": doc, "batch": batch, "draft": draft}


async def _counts(db: Database) -> dict[str, int]:
    async with db.engine.connect() as conn:
        return {t: (await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar()
                for t in ("items", "documents", "document_lines", "stock_ledger", "import_batches",
                          "import_lines", "audit_log")}


async def _roundtrip(db: Database) -> None:
    await _business_data(db)
    before = await _counts(db)
    await downgrade(db, V1_0_0_HEAD)
    assert await current_revision(db) == V1_0_0_HEAD
    assert await _counts(db) == before  # no table rebuild may lose rows (e.g. cascades)
    await upgrade(db)
    assert await current_revision(db) == head_revision()
    assert await _counts(db) == before
    async with db.engine.connect() as conn:
        diff = await conn.run_sync(lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))
    assert diff == []
    # Existing installations: the seeded count units become integer-only, others unchanged.
    units = {u.name: u.allow_decimal for u in await master.list_units(db, include_inactive=True)}
    assert units["عدد"] is False and units["کارتن"] is False and units["متر"] is True


async def test_new_migrations_roundtrip_sqlite(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await prepare(db)
        await _roundtrip(db)
    finally:
        await db.dispose()


async def test_deleting_a_document_nulls_the_batch_link_in_the_database(db):
    """ON DELETE SET NULL at the database level, not only in the service (#3)."""
    data = await _business_data(db)
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM document_lines WHERE document_id = :d"), {"d": data["draft"]})
        await conn.execute(text("DELETE FROM documents WHERE id = :d"), {"d": data["draft"]})
        link = (await conn.execute(text("SELECT result_document_id FROM import_batches WHERE id = :b"),
                                   {"b": data["batch"]})).scalar()
    assert link is None


@pytest.mark.skipif(not MARIADB_URL, reason="CASPIAN_TEST_MARIADB_URL not set")
async def test_new_migrations_roundtrip_mariadb():
    db = Database(MARIADB_URL)
    try:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await prepare(db)
        await _roundtrip(db)
        data = await _business_data_again(db)
        await docs.delete_draft(db, data["admin"], data["draft"])  # FK 1451 before the fix
    finally:
        await db.dispose()


async def _business_data_again(db: Database) -> dict:
    admin = (await auth.login(db, "admin", "admin")).actor
    wh = (await master.list_warehouses(db))[0].id
    batch = await imports.create_batch(db, admin, ImportKind.STOCK, ImportSource.SCAN,
                                       [RawRow(code="M-1", qty=Decimal(2))], "b2", DocType.RECEIPT, wh)
    return {"admin": admin, "draft": (await imports.apply_batch(db, admin, batch)).document_id}


async def test_newer_database_is_refused_untouched(tmp_path):
    """An older app met a DB migrated by a newer one: raw English Alembic error before (#9)."""
    from caspian.db.database import describe_error
    from caspian.db.migrate import SchemaTooNew

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'n.db'}")
    try:
        await prepare(db)
        async with db.engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
        before = await _counts(db)
        with pytest.raises(SchemaTooNew) as caught:
            await prepare(db)
        assert "نسخه جدیدتری" in describe_error(caught.value)
        assert await current_revision(db) == "ffffffffffff" and await _counts(db) == before
    finally:
        await db.dispose()
