"""Read-only database access for the AI / MCP server.

Defense in depth:
1. A dedicated MariaDB user with SELECT on business tables only (never `users`, which holds
   password hashes, nor `app_settings`).
2. Every connection opened for AI/MCP use is switched to a READ ONLY transaction mode,
   so even the fallback app user can't write through it.
"""

from sqlalchemy import event, text

from caspian.core.secrets import get_secret, set_secret
from caspian.db.database import Database, DbConfig
from caspian.services.errors import ValidationError

EXCLUDED_TABLES = {"users", "app_settings", "alembic_version"}
SECRET_KIND = "db-ro"


def readonly_username(config: DbConfig) -> str:
    return f"{config.user}_ro"[:80]


def readonly_password(config: DbConfig) -> str | None:
    return get_secret(SECRET_KIND, config.secret_name)


def _enforce_read_only(db: Database) -> None:
    def on_connect(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        if db.is_sqlite:
            cursor.execute("PRAGMA query_only = ON")
        else:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        cursor.close()

    event.listen(db.engine.sync_engine, "connect", on_connect)


def open_read_only(config: DbConfig, app_password: str) -> tuple[Database, bool]:
    """(database, uses_dedicated_user). Falls back to the app user in read-only mode."""
    ro_password = readonly_password(config)
    if ro_password:
        ro_config = DbConfig(config.host, config.port, config.name, readonly_username(config))
        db = Database(ro_config.url(ro_password))
    else:
        db = Database(config.url(app_password))
    _enforce_read_only(db)
    return db, bool(ro_password)


def wrap_read_only(db_url: str) -> Database:
    """For tests / SQLite: any URL, forced read-only."""
    db = Database(db_url)
    _enforce_read_only(db)
    return db


async def create_readonly_user(config: DbConfig, admin_user: str, admin_password: str,
                               password: str | None = None) -> str:
    """Create/refresh the SELECT-only user (needs a MariaDB account allowed to grant)."""
    import secrets as _secrets

    if not config.user.replace("_", "").isalnum() or not config.name.replace("_", "").isalnum():
        raise ValidationError("نام کاربر یا پایگاه داده نامعتبر است.")
    user = readonly_username(config)
    password = password or _secrets.token_urlsafe(24)
    admin = Database(DbConfig(config.host, config.port, config.name, admin_user)
                     .url(admin_password, database=""))
    try:
        async with admin.engine.connect() as conn:
            await conn.execute(text(f"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY :pw"),
                               {"pw": password})
            await conn.execute(text(f"ALTER USER '{user}'@'%' IDENTIFIED BY :pw"), {"pw": password})
            await conn.execute(text(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{user}'@'%'"))
            tables = (await conn.execute(text(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :db"),
                {"db": config.name})).scalars().all()
            for table in tables:
                if table not in EXCLUDED_TABLES and table.replace("_", "").isalnum():
                    await conn.execute(text(
                        f"GRANT SELECT ON `{config.name}`.`{table}` TO '{user}'@'%'"))
            await conn.commit()
    finally:
        await admin.dispose()
    set_secret(SECRET_KIND, config.secret_name, password)
    return user
