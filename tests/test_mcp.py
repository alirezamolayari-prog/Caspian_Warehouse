import json
import os
import shutil

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from caspian.db.readonly import wrap_read_only
from caspian.mcp_server import build_server, validate_select
from caspian.services import items, master
from caspian.services.items import ItemInput


def _payload(result) -> object:
    """First text content of a CallToolResult, decoded as JSON."""
    return json.loads(result.content[0].text)


@pytest.fixture
async def ro_db(db, admin, tmp_path):
    unit = (await master.list_units(db))[0].id
    await items.create_item(db, admin, ItemInput("1001", "دریل بوش", unit))
    await db.dispose()
    copy = tmp_path / "ro.db"
    shutil.copyfile(str(db.url.database), copy)
    ro = wrap_read_only(f"sqlite+aiosqlite:///{copy}")
    yield ro
    await ro.dispose()


async def test_tools_listed_and_read_only(ro_db):
    server = build_server(ro_db, allow_sql=False)
    names = {t.name for t in await server.list_tools()}
    assert names == {"search_items", "item_stock", "reorder_analysis", "recent_documents",
                     "open_loans", "report"}
    result = _payload(await server.call_tool("search_items", {"query": "دريل"}))
    assert result[0]["name"] == "دریل بوش"
    report = _payload(await server.call_tool("report", {"name": "stock_balance"}))
    assert report["title"] == "گزارش موجودی کالا"
    assert "error" in _payload(await server.call_tool("report", {"name": "activity"}))


async def test_connection_refuses_writes(ro_db):
    async with ro_db.engine.connect() as conn:
        with pytest.raises(OperationalError):
            await conn.execute(text("UPDATE items SET name = 'x'"))


async def test_sql_tool(ro_db):
    server = build_server(ro_db, allow_sql=True)
    ok = _payload(await server.call_tool("query_sql", {"sql": "select code, name from items;"}))
    assert ok["columns"] == ["code", "name"] and ok["rows"] == [["1001", "دریل بوش"]]
    for bad in ("delete from items", "select 1; drop table items", "select sleep(5)"):
        assert "error" in _payload(await server.call_tool("query_sql", {"sql": bad}))


def test_validate_select():
    assert validate_select(" SELECT 1 ; ") == "SELECT 1"
    assert validate_select("with x as (select 1) select * from x")
    for bad in ("update items set x=1", "select * from items into outfile 'x'", "select 1;select 2"):
        with pytest.raises(ValueError):
            validate_select(bad)


MARIADB_ROOT = os.environ.get("CASPIAN_TEST_MARIADB_ROOT")  # "user:password" of an admin account
MARIADB_URL = os.environ.get("CASPIAN_TEST_MARIADB_URL")


@pytest.mark.skipif(not (MARIADB_ROOT and MARIADB_URL), reason="MariaDB admin credentials not set")
async def test_readonly_user_on_mariadb(monkeypatch):
    from sqlalchemy.engine import make_url

    from caspian.db import readonly
    from caspian.db.bootstrap import prepare
    from caspian.db.database import Database, DbConfig

    store = {}
    monkeypatch.setattr(readonly, "set_secret", lambda k, n, v: store.__setitem__((k, n), v))
    monkeypatch.setattr(readonly, "get_secret", lambda k, n: store.get((k, n)))
    url = make_url(MARIADB_URL)
    config = DbConfig(url.host, url.port or 3306, url.database, url.username)
    app_db = Database(MARIADB_URL)
    await prepare(app_db)
    await app_db.dispose()
    admin_user, admin_password = MARIADB_ROOT.split(":", 1)
    user = await readonly.create_readonly_user(config, admin_user, admin_password)
    ro, dedicated = readonly.open_read_only(config, url.password)
    assert dedicated and user.endswith("_ro")
    try:
        async with ro.engine.connect() as conn:
            assert (await conn.execute(text("SELECT COUNT(*) FROM items"))).scalar() >= 0
            with pytest.raises(OperationalError):  # no privilege on password hashes
                await conn.execute(text("SELECT password_hash FROM users"))
            with pytest.raises(OperationalError):
                await conn.execute(text("UPDATE items SET name = 'x'"))
    finally:
        await ro.dispose()
