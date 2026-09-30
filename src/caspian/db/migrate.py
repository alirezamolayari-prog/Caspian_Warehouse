"""Run Alembic migrations programmatically (the app upgrades its schema on startup)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from caspian.db.database import Database

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


async def _run(db: Database, action, revision: str) -> None:
    cfg = alembic_config()

    def _go(sync_conn) -> None:
        cfg.attributes["connection"] = sync_conn
        action(cfg, revision)

    if not db.is_sqlite:
        async with db.engine.begin() as conn:
            await conn.run_sync(_go)
        return
    # SQLite applies ALTERs by rebuilding the table (copy, DROP, rename). With foreign keys on,
    # that DROP would fire ON DELETE CASCADE on child tables and silently delete their rows.
    # The pragma can't change inside a transaction, so switch it around the migration.
    async with db.engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.commit()
        try:
            async with conn.begin():
                await conn.run_sync(_go)
                problems = (await conn.exec_driver_sql("PRAGMA foreign_key_check")).fetchall()
                if problems:
                    raise RuntimeError(f"Migration left broken foreign keys: {problems[:5]}")
        finally:
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.commit()


async def upgrade(db: Database, revision: str = "head") -> None:
    await _run(db, command.upgrade, revision)


async def downgrade(db: Database, revision: str) -> None:
    """Only for tests and support: schema back to `revision` (data is kept where possible)."""
    await _run(db, command.downgrade, revision)


async def current_revision(db: Database) -> str | None:
    async with db.engine.connect() as conn:
        return await conn.run_sync(
            lambda c: MigrationContext.configure(c).get_current_revision()
        )


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()
