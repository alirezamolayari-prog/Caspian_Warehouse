"""Bring a database to a usable state: create if asked, migrate, seed."""

import logging

from caspian.db.database import Database, DbConfig, ensure_database
from caspian.db.migrate import upgrade
from caspian.db.seed import seed_reference_data

log = logging.getLogger(__name__)


async def prepare(db: Database) -> None:
    from caspian.services.auth import ensure_default_admin

    await upgrade(db)
    async with db.session() as session:
        await seed_reference_data(session)
        await ensure_default_admin(session)


async def open_mariadb(config: DbConfig, password: str, create: bool = False) -> Database:
    if create:
        await ensure_database(config, password)
    db = Database(config.url(password))
    try:
        await db.ping()
        await prepare(db)
    except BaseException:
        await db.dispose()
        raise
    log.info("Database ready: %s@%s/%s", config.user, config.host, config.name)
    return db
