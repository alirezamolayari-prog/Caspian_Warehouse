"""Go-live without Caspian Server: everything that talks to it must fail gracefully — not configured,
unreachable or hanging — with a Persian message, no freeze, and local warehouse work unaffected."""

import asyncio
import datetime as dt
import time
from decimal import Decimal

import httpx
import pytest

from caspian.db.models import DocType, RequestStatus
from caspian.services import documents
from caspian.services.server_link import connection, goods
from caspian.services.server_link.errors import ServerUnreachable
from caspian.services.server_link.sync import SyncEngine, SyncRunner, SyncState
from request_helpers import request_input, stock

FAST = 0.5  # seconds: the test client's timeout instead of the real 10


@pytest.fixture
async def hanging_server():
    """A real TCP server that accepts connections and never answers (like a frozen server)."""
    held = []

    async def accept(reader, writer):
        held.append(writer)
        await asyncio.sleep(3600)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    for writer in held:
        writer.close()
    server.close()


async def test_not_configured_makes_no_network_call(db, admin, request_shop):
    calls = []
    engine = SyncEngine(db, httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)))
    assert await connection.get_connection(db) is None  # empty by default: no built-in address
    request_id = await goods.create_request(db, admin, request_input(request_shop))
    result = await engine.tick(admin)
    assert result.state == SyncState.NOT_CONFIGURED and calls == []
    assert (await goods.get_request(db, admin, request_id)).row.status == RequestStatus.QUEUED


async def test_unreachable_server_gives_a_persian_message(db, admin, request_shop):
    await connection.save_connection(db, admin, "https://caspian.test", "warehouse", "pw-123456")

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    result = await SyncEngine(db, httpx.MockTransport(refuse)).tick(admin)
    assert result.state == SyncState.DISCONNECTED and "در دسترس نیست" in result.message


async def test_hanging_server_times_out_quickly(db, admin, request_shop, hanging_server):
    await connection.save_connection(db, admin, hanging_server, "warehouse", "pw-123456")
    request_id = await goods.create_request(db, admin, request_input(request_shop))
    started = time.monotonic()
    result = await SyncEngine(db, timeout=FAST).tick(admin)
    assert time.monotonic() - started < 5
    assert result.state == SyncState.DISCONNECTED and "در دسترس نیست" in result.message
    assert (await goods.get_request(db, admin, request_id)).row.status == RequestStatus.QUEUED


async def test_local_work_continues_while_the_server_hangs(db, admin, request_shop, hanging_server):
    await connection.save_connection(db, admin, hanging_server, "warehouse", "pw-123456")
    await goods.create_request(db, admin, request_input(request_shop))
    runner = SyncRunner(SyncEngine(db, timeout=30), interval=0.05)  # a tick stuck on the server
    runner.set_actor(admin)
    runner.start()
    try:
        await asyncio.sleep(0.2)
        started = time.monotonic()
        await documents.create_and_post(db, admin, documents.DocumentInput(
            DocType.RECEIPT, dt.date.today(), request_shop["warehouse"],
            [documents.LineInput(request_shop["bags"], request_shop["unit"], Decimal(7))]))
        assert time.monotonic() - started < 3  # receipts don't wait for the server
        assert await stock(db, request_shop["bags"]) == 57
    finally:
        await runner.stop()


async def test_connection_test_on_a_hanging_server(hanging_server):
    started = time.monotonic()
    with pytest.raises(ServerUnreachable) as exc:
        await connection.test_connection(hanging_server, "warehouse", "pw", timeout=FAST)
    assert time.monotonic() - started < 5 and "در دسترس نیست" in exc.value.message


async def test_https_address_is_accepted(db, admin):
    saved = await connection.save_connection(db, admin, "https://caspian.example.ir/", "warehouse", "pw-123")
    assert saved.url == "https://caspian.example.ir" and not saved.insecure
