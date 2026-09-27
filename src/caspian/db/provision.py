"""First-install provisioning: a dedicated MariaDB account for the app (never run as root)."""

import secrets

from sqlalchemy import text

from caspian.db.database import CHARSET, COLLATION, Database, DbConfig
from caspian.services.errors import ValidationError


def _check_identifier(value: str, label: str) -> None:
    if not value or not value.replace("_", "").isalnum() or len(value) > 64:
        raise ValidationError(f"{label} فقط می‌تواند حروف لاتین، عدد و _ باشد.")


async def create_app_user(config: DbConfig, admin_user: str, admin_password: str,
                          network: bool, password: str = "") -> str:
    """Create the database and `config.user` with full rights on it only. Returns its password
    (random unless one is given; LAN clients need to know it).

    `network=True` lets the account connect from other PCs on the LAN ('%'); otherwise only
    from this PC ('localhost').
    """
    _check_identifier(config.user, "نام کاربری")
    _check_identifier(config.name, "نام پایگاه داده")
    host = "%" if network else "localhost"
    password = password or secrets.token_urlsafe(24)
    admin = Database(DbConfig(config.host, config.port, "", admin_user).url(admin_password, database=""))
    try:
        async with admin.engine.connect() as conn:
            await conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{config.name}` "
                                    f"CHARACTER SET {CHARSET} COLLATE {COLLATION}"))
            await conn.execute(text(f"CREATE USER IF NOT EXISTS '{config.user}'@'{host}' "
                                    "IDENTIFIED BY :pw"), {"pw": password})
            await conn.execute(text(f"ALTER USER '{config.user}'@'{host}' IDENTIFIED BY :pw"),
                               {"pw": password})
            await conn.execute(text(f"GRANT ALL PRIVILEGES ON `{config.name}`.* "
                                    f"TO '{config.user}'@'{host}'"))
            await conn.commit()
    finally:
        await admin.dispose()
    return password
