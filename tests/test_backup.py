import os
import time
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from caspian.db.models import AuditLog, Item
from caspian.services import backup, items, master, protected
from caspian.services.backup import BackupError, SqliteDumper
from caspian.services.errors import ApprovalError, ValidationError
from caspian.services.items import ItemInput

PASSWORD = "Backup#Pass2026"


@pytest.fixture
async def env(db, admin, tmp_path):
    unit = (await master.list_units(db))[0].id
    await items.create_item(db, admin, ItemInput("1001", "دریل بوش", unit, reorder_point=Decimal(3)))
    dumper = SqliteDumper(make_url(str(db.url)).database)
    return {"dumper": dumper, "dir": tmp_path / "backups", "unit": unit}


async def test_backup_is_encrypted_and_verifiable(db, admin, env):
    info = await backup.create_backup(db, admin, env["dumper"], PASSWORD, env["dir"], "شبانه")
    data = info.path.read_bytes()
    assert data.startswith(backup.MAGIC)
    assert "دریل".encode() not in data and b"CREATE TABLE" not in data  # nothing in the clear
    assert info.database and info.schema_revision and info.label == "شبانه"
    assert (await backup.verify_backup(info.path, PASSWORD)).name == info.name
    with pytest.raises(BackupError, match="رمز"):
        await backup.verify_backup(info.path, "Wrong#Password1")


async def test_tampered_file_is_rejected(db, admin, env):
    info = await backup.create_backup(db, admin, env["dumper"], PASSWORD, env["dir"])
    raw = bytearray(info.path.read_bytes())
    raw[-40] ^= 0xFF
    info.path.write_bytes(bytes(raw))
    with pytest.raises(BackupError):
        await backup.verify_backup(info.path, PASSWORD)


async def test_weak_password_refused(db, admin, env):
    with pytest.raises(ValidationError):
        await backup.create_backup(db, admin, env["dumper"], "short", env["dir"])


async def test_restore_requires_pin_and_brings_data_back(db, admin, env):
    info = await backup.create_backup(db, admin, env["dumper"], PASSWORD, env["dir"])
    await items.create_item(db, admin, ItemInput("2002", "آیتم بعد از بکاپ", env["unit"]))

    with pytest.raises(ApprovalError):
        await backup.restore_backup(db, admin, env["dumper"], info.path, PASSWORD, None, env["dir"])

    approval = await protected.approve(
            db, admin, protected.ProtectedAction.RESTORE_BACKUP, "admin", "4826")
    await backup.restore_backup(db, admin, env["dumper"], info.path, PASSWORD, approval, env["dir"])
    async with db.session() as s:
        codes = set((await s.scalars(select(Item.code))).all())
        restored = await s.scalar(select(AuditLog).where(AuditLog.action == "backup.restored"))
    assert codes == {"1001"}  # the later item is gone
    assert restored.approved_by_id == admin.user_id
    safety = [b for b in backup.list_backups(env["dir"]) if b.label == "pre-restore"]
    assert len(safety) == 1  # current data was saved before being replaced


async def test_wrong_password_never_touches_the_database(db, admin, env):
    info = await backup.create_backup(db, admin, env["dumper"], PASSWORD, env["dir"])
    await items.create_item(db, admin, ItemInput("2002", "جدید", env["unit"]))
    approval = await protected.approve(db, admin, protected.ProtectedAction.RESTORE_BACKUP,
                                       "admin", "4826")
    with pytest.raises(BackupError):
        await backup.restore_backup(db, admin, env["dumper"], info.path, "Wrong#Password1", approval,
                                    env["dir"])
    async with db.session() as s:
        assert set((await s.scalars(select(Item.code))).all()) == {"1001", "2002"}


async def test_list_and_prune(db, admin, env):
    made = []
    for i in range(3):
        made.append(await backup.create_backup(db, admin, env["dumper"], PASSWORD, env["dir"], f"n{i}"))
        time.sleep(1.1)  # distinct timestamps in file names
    (env["dir"] / "someone-else.bak").write_bytes(b"not ours")
    assert [b.label for b in backup.list_backups(env["dir"])] == ["n2", "n1", "n0"]
    assert backup.prune(env["dir"], keep=1) == 2
    assert [b.label for b in backup.list_backups(env["dir"])] == ["n2"]
    assert (env["dir"] / "someone-else.bak").exists()  # never delete files we didn't create


MARIADB_URL = os.environ.get("CASPIAN_TEST_MARIADB_URL")


@pytest.mark.skipif(not MARIADB_URL, reason="CASPIAN_TEST_MARIADB_URL not set")
async def test_mariadb_backup_and_restore(tmp_path):
    from caspian.db.bootstrap import prepare
    from caspian.db.database import Database, DbConfig
    from caspian.services import auth

    url = make_url(MARIADB_URL)
    config = DbConfig(url.host, url.port or 3306, url.database, url.username)
    db = Database(MARIADB_URL)
    try:
        await prepare(db)
        admin = (await auth.login(db, "admin", "admin")).actor
        unit = (await master.list_units(db))[0].id
        async with db.session() as s:
            for item in (await s.scalars(select(Item).where(Item.code.in_(["BK1", "BK2"])))).all():
                await s.delete(item)
        await items.create_item(db, admin, ItemInput("BK1", "قبل از بکاپ", unit))
        dumper = backup.MariaDbDumper(config, url.password)
        assert dumper.dump_exe and dumper.client_exe
        info = await backup.create_backup(db, admin, dumper, PASSWORD, tmp_path)
        await items.create_item(db, admin, ItemInput("BK2", "بعد از بکاپ", unit))
        await auth.change_password(db, admin, "admin", "Str0ngPass")
        await auth.set_pin(db, admin, "Str0ngPass", "4826")
        approval = await protected.approve(
            db, admin, protected.ProtectedAction.RESTORE_BACKUP, "admin", "4826")

        async def after():
            await prepare(db)

        await backup.restore_backup(db, admin, dumper, info.path, PASSWORD, approval, tmp_path, after)
        async with db.session() as s:
            codes = set((await s.scalars(select(Item.code))).all())
        assert "BK1" in codes and "BK2" not in codes
    finally:
        await db.dispose()
