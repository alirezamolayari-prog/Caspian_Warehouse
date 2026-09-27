import asyncio
import shutil

import pytest
from argon2 import PasswordHasher

from caspian.core import security
from caspian.db.bootstrap import prepare
from caspian.db.database import Database

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """Argon2 at production cost makes the suite very slow; strength isn't under test."""
    monkeypatch.setattr(security, "_hasher", FAST_HASHER)


@pytest.fixture(scope="session")
def template_db(tmp_path_factory):
    """Migrate + seed once; each test gets a copy (migrations take seconds)."""
    path = tmp_path_factory.mktemp("template") / "template.db"

    async def build():
        original = security._hasher
        security._hasher = FAST_HASHER
        try:
            database = Database(f"sqlite+aiosqlite:///{path}")
            await prepare(database)
            await database.dispose()
        finally:
            security._hasher = original

    asyncio.run(build())
    return path


@pytest.fixture
async def db(tmp_path, template_db):
    path = tmp_path / "test.db"
    shutil.copyfile(template_db, path)
    database = Database(f"sqlite+aiosqlite:///{path}")
    yield database
    await database.dispose()


ADMIN_PW = "Str0ngPass"
ADMIN_PIN = "4826"


@pytest.fixture
async def admin(db):
    """The default admin after first-login setup (password changed, PIN set)."""
    from caspian.services import auth

    actor = (await auth.login(db, "admin", "admin")).actor
    await auth.change_password(db, actor, "admin", ADMIN_PW)
    await auth.set_pin(db, actor, ADMIN_PW, ADMIN_PIN)
    return actor
