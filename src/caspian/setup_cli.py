"""Non-GUI modes used by the Windows installer and its automated tests.

    CaspianWarehouse.exe --provision <password-file> [--db-name N] [--db-user U]
        After the installer has installed MariaDB: create the app's database and account
        (reusing caspian.db.provision.create_app_user), run migrations, and save the
        connection + passwords exactly like the first-run dialog does. The file holds the
        MariaDB administrator password chosen in the installer (it lives in the installer's
        private temp folder, deleted when setup ends).
    CaspianWarehouse.exe --check-db
        Connect with the saved settings; exit code 0 = OK, 1 = failed.
"""

import asyncio
import logging
from pathlib import Path

from caspian.core.secrets import get_secret, set_secret
from caspian.core.settings import Settings
from caspian.db.bootstrap import open_mariadb
from caspian.db.database import DEFAULT_DB_NAME, Database, DbConfig
from caspian.db.provision import create_app_user

log = logging.getLogger(__name__)

ADMIN_USER = "root"
SERVER_WAIT_SECONDS = 90


def _arg(argv: list[str], flag: str, default: str | None = None) -> str | None:
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default


async def _wait_for_server(config: DbConfig, admin_password: str) -> None:
    """A freshly installed MariaDB service can take a few seconds to accept connections."""
    probe = Database(DbConfig(config.host, config.port, "", ADMIN_USER).url(admin_password, database=""))
    try:
        for attempt in range(SERVER_WAIT_SECONDS):
            try:
                await probe.ping()
                return
            except Exception:
                if attempt == SERVER_WAIT_SECONDS - 1:
                    raise
                await asyncio.sleep(1)
    finally:
        await probe.dispose()


async def provision(password_file: Path, db_name: str = DEFAULT_DB_NAME,
                    db_user: str = "caspian") -> None:
    # utf-8-sig: Inno Setup writes the file with a byte-order mark.
    admin_password = password_file.read_text(encoding="utf-8-sig").strip()
    if not admin_password:
        raise ValueError("empty administrator password")
    config = DbConfig(name=db_name, user=db_user)
    await _wait_for_server(config, admin_password)
    app_password = await create_app_user(config, ADMIN_USER, admin_password, network=False)
    db = await open_mariadb(config, app_password)  # migrations + seed data
    await db.dispose()
    set_secret("db", config.secret_name, app_password)
    set_secret("mariadb", f"{ADMIN_USER}@{config.host}:{config.port}", admin_password)
    settings = Settings.load()
    settings.database = config.to_dict()
    settings.save()
    log.info("Provisioned database %s for user %s", db_name, db_user)


async def check_db() -> bool:
    config = DbConfig.from_dict(Settings.load().database)
    if config is None:
        log.error("check-db: no database configured")
        return False
    try:
        db = await open_mariadb(config, get_secret("db", config.secret_name) or "")
    except Exception:
        log.exception("check-db: connection failed")
        return False
    await db.dispose()
    return True


def run(argv: list[str]) -> int | None:
    """Handle an installer mode if present in argv; None means "start the normal app"."""
    if "--provision" in argv:
        path = _arg(argv, "--provision")
        try:
            asyncio.run(provision(Path(path or ""), _arg(argv, "--db-name", DEFAULT_DB_NAME),
                                  _arg(argv, "--db-user", "caspian")))
        except Exception:
            log.exception("Provisioning failed")
            return 1
        return 0
    if "--check-db" in argv:
        return 0 if asyncio.run(check_db()) else 1
    return None
