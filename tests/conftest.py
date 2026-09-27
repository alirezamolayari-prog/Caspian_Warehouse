import pytest

from caspian.db.bootstrap import prepare
from caspian.db.database import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await prepare(database)
    yield database
    await database.dispose()
