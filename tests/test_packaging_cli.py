import asyncio
import os
import tomllib
from pathlib import Path

import pytest
from sqlalchemy import text

from caspian import __version__, setup_cli
from caspian.core.settings import Settings
from caspian.db.database import Database, DbConfig

ROOT = Path(__file__).resolve().parents[1]


def test_version_comes_from_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert __version__ == project["version"]


def test_normal_start_is_not_intercepted():
    assert setup_cli.run(["caspian"]) is None
    assert setup_cli.run(["caspian", "--smoke-test"]) is None


def test_check_db_fails_cleanly(monkeypatch):
    Settings(database={"host": "127.0.0.1", "port": 1, "name": "x", "user": "y"}).save()
    monkeypatch.setattr(setup_cli, "get_secret", lambda k, n: "")
    assert setup_cli.run(["caspian", "--check-db"]) == 1
    Settings().save()  # nothing configured at all
    assert setup_cli.run(["caspian", "--check-db"]) == 1


def test_provision_bad_input_returns_error(tmp_path):
    empty = tmp_path / "pw.txt"
    empty.write_text("", encoding="utf-8")
    assert setup_cli.run(["caspian", "--provision", str(empty)]) == 1
    assert setup_cli.run(["caspian", "--provision", str(tmp_path / "missing.txt")]) == 1


MARIADB_URL = os.environ.get("CASPIAN_TEST_MARIADB_URL")
MARIADB_ROOT = os.environ.get("CASPIAN_TEST_MARIADB_ROOT")


@pytest.mark.skipif(not (MARIADB_URL and MARIADB_ROOT), reason="MariaDB admin credentials not set")
def test_provision_then_check_db(tmp_path, monkeypatch):
    """What the installer does after installing MariaDB, on a throwaway database/account."""
    store = {}
    monkeypatch.setattr(setup_cli, "set_secret", lambda k, n, v: store.__setitem__((k, n), v))
    monkeypatch.setattr(setup_cli, "get_secret", lambda k, n: store.get((k, n)))
    root_user, root_password = MARIADB_ROOT.split(":", 1)
    assert root_user == setup_cli.ADMIN_USER
    pw_file = tmp_path / "pw.txt"
    pw_file.write_text(root_password + "\n", encoding="utf-8-sig")  # as Inno Setup writes it
    try:
        code = setup_cli.run(["caspian", "--provision", str(pw_file),
                              "--db-name", "caspian_inst_test", "--db-user", "caspian_inst"])
        assert code == 0
        assert Settings.load().database == DbConfig(name="caspian_inst_test", user="caspian_inst").to_dict()
        assert store[("db", "caspian_inst@localhost:3306")]
        assert store[("mariadb", "root@localhost:3306")] == root_password
        assert setup_cli.run(["caspian", "--check-db"]) == 0
    finally:
        async def cleanup():
            root = Database(DbConfig(name="", user=root_user).url(root_password, database=""))
            async with root.engine.connect() as conn:
                await conn.execute(text("DROP USER IF EXISTS 'caspian_inst'@'localhost'"))
                await conn.execute(text("DROP DATABASE IF EXISTS caspian_inst_test"))
            await root.dispose()

        asyncio.run(cleanup())
