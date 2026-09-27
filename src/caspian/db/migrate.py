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


async def upgrade(db: Database, revision: str = "head") -> None:
    cfg = alembic_config()

    def _run(sync_conn) -> None:
        cfg.attributes["connection"] = sync_conn
        command.upgrade(cfg, revision)

    async with db.engine.begin() as conn:
        await conn.run_sync(_run)


async def current_revision(db: Database) -> str | None:
    async with db.engine.connect() as conn:
        return await conn.run_sync(
            lambda c: MigrationContext.configure(c).get_current_revision()
        )


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()
