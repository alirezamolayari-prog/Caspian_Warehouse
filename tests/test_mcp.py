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


# ----- #17: HTTP transport, audit, draft-only tools -----

HTTP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _http_db(tmp_path):
    import asyncio

    from caspian.db.bootstrap import prepare
    from caspian.db.database import Database

    url = f"sqlite+aiosqlite:///{tmp_path / 'http.db'}"
    setup = Database(url)
    asyncio.run(prepare(setup))
    asyncio.run(setup.dispose())
    return Database(url)


def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def test_http_requires_the_bearer_token(tmp_path, _isolated_logs):
    from starlette.testclient import TestClient

    from caspian.mcp_server import http_app

    app = http_app(build_server(_http_db(tmp_path), allow_sql=False), "s3cret-token")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        for auth in (None, "Bearer wrong", "s3cret-token", "Basic s3cret-token"):
            headers = dict(HTTP_HEADERS, **({"authorization": auth} if auth else {}))
            response = client.post("/mcp", json=_rpc("tools/list"), headers=headers)
            assert response.status_code == 401, auth
            assert response.headers["www-authenticate"] == "Bearer"
        ok = dict(HTTP_HEADERS, authorization="Bearer s3cret-token")
        tools = client.post("/mcp", json=_rpc("tools/list"), headers=ok).json()["result"]["tools"]
        assert "search_items" in {t["name"] for t in tools}
        call = client.post("/mcp", json=_rpc("tools/call", {"name": "open_loans", "arguments": {}}),
                           headers=ok)
        assert call.status_code == 200
    audit = (_isolated_logs / "mcp-audit.log").read_text(encoding="utf-8")
    assert audit.count("rejected") == 4
    assert '"tool": "open_loans"' in audit and '"client": "http testclient"' in audit  # peer address


def test_http_binds_to_localhost_unless_lan(tmp_path):
    from caspian.mcp_server import BearerTokenMiddleware, bind_host, http_app

    assert bind_host(False) == "127.0.0.1" and bind_host(True) == "0.0.0.0"
    with pytest.raises(ValueError):
        BearerTokenMiddleware(object(), "")
    from starlette.testclient import TestClient

    app = http_app(build_server(_http_db(tmp_path), allow_sql=False), "t")
    with TestClient(app, base_url="http://192.168.1.20:8765") as client:  # LAN name, localhost-only server
        response = client.post("/mcp", json=_rpc("tools/list"),
                               headers=dict(HTTP_HEADERS, authorization="Bearer t"))
        assert response.status_code != 200  # DNS-rebinding protection refuses other hosts


async def test_every_stdio_call_is_audited(ro_db, _isolated_logs):
    server = build_server(ro_db, allow_sql=False)
    await server.call_tool("search_items", {"query": "دریل"})
    line = (_isolated_logs / "mcp-audit.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    assert '"tool": "search_items"' in line and '"client": "stdio"' in line and '"دریل"' in line


async def test_draft_tools_only_when_enabled_and_never_post(db, admin, ro_db):
    from caspian.db.models import DocStatus
    from caspian.services import documents as docs
    from caspian.services import mcp_settings

    assert not await mcp_settings.allow_drafts(db)
    names = {t.name for t in await build_server(ro_db, allow_sql=False).list_tools()}
    assert "create_draft_document" not in names
    with pytest.raises(Exception):  # noqa: B017 - the AI can't switch it on
        await mcp_settings.set_allow_drafts(db, admin.as_ai(), True)
    await mcp_settings.set_allow_drafts(db, admin, True)
    assert await mcp_settings.allow_drafts(db)

    unit = (await master.list_units(db))[0].id
    await items.create_item(db, admin, ItemInput("2001", "میز", unit))
    server = build_server(ro_db, allow_sql=False, write_db=db)
    assert "create_draft_document" in {t.name for t in await server.list_tools()}
    result = _payload(await server.call_tool("create_draft_document", {
        "doc_type": "RECEIPT", "warehouse_code": "01", "lines": [{"code": "2001", "qty": 3}],
        "description": "فاکتور ۱۲"}))
    assert result["status"] == "DRAFT" and result["number"].startswith("ر-")
    detail = await docs.get_document(db, admin, result["document_id"])
    assert detail.status is DocStatus.DRAFT and "MCP" in detail.input.description
    assert "error" in _payload(await server.call_tool("create_draft_document", {
        "doc_type": "ADJUSTMENT", "warehouse_code": "01", "lines": []}))
    names = {t.name for t in await server.list_tools()}
    assert not any(n.startswith(("post", "cancel", "delete")) for n in names)
