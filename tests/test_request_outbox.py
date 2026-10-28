"""The local outbox: requests wait while the server is unreachable, are sent once when it returns,
and a retry after a lost answer never makes a second request."""

import asyncio
import uuid

from sqlalchemy import func, select

from caspian.db.models import GoodsRequest, RequestStatus, ServerOutbox
from caspian.services.server_link import goods
from caspian.services.server_link.errors import ServerRejected
from caspian.services.server_link.sync import SyncEngine, SyncState
from request_helpers import request_input


async def _status(db, request_id) -> RequestStatus:
    async with db.session() as s:
        return (await s.get(GoodsRequest, request_id)).status


async def _outbox(db) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(ServerOutbox))


async def test_offline_requests_wait_in_the_queue_then_go_once(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    server.online = False
    first = await goods.create_request(db, admin, request_input(linked))
    second = await goods.create_request(db, admin, request_input(linked, qty=5))
    result = await engine.tick(admin)
    assert result.state == SyncState.DISCONNECTED and "صف" in result.message
    assert await _status(db, first) == RequestStatus.QUEUED and not server.requests
    rows = await goods.list_requests(db, admin)
    assert {r.status_name for r in rows} == {"در صف ارسال"}

    server.online = True
    assert (await engine.tick(admin)).state == SyncState.CONNECTED
    assert await _status(db, first) == RequestStatus.PENDING == await _status(db, second)
    assert len(server.requests) == 2 and await _outbox(db) == 0
    sent = server.requests[1]
    assert sent["requester_name"] == "علی رضایی" and sent["requester_phone"] == "0912 345 6789"
    assert sent["items"][0]["item_code"] == "1024" and sent["items"][0]["quantity"] == 20
    assert sent["consumption_location"] == linked["location"].label()
    await engine.tick(admin)
    assert len(server.requests) == 2  # nothing sent twice


async def test_lost_answer_retry_does_not_duplicate(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await goods.create_request(db, admin, request_input(linked))
    server.drop_next = 1  # the server saves it, but the answer never arrives
    assert (await engine.tick(admin)).state == SyncState.DISCONNECTED
    assert len(server.requests) == 1 and await _status(db, request_id) == RequestStatus.QUEUED
    await engine.tick(admin)
    assert len(server.requests) == 1
    assert await _status(db, request_id) == RequestStatus.PENDING
    async with db.session() as s:
        assert (await s.get(GoodsRequest, request_id)).server_id == 1


async def test_same_client_request_id_is_one_request(linked, admin):
    db = linked["db"]
    cid = str(uuid.uuid4())
    a = await goods.create_request(db, admin, request_input(linked), cid)
    b = await goods.create_request(db, admin, request_input(linked), cid)  # a double click / retry
    assert a == b
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(GoodsRequest)) == 1


async def test_two_saves_at_the_same_moment_are_one_request(linked, admin):
    db = linked["db"]
    cid = str(uuid.uuid4())
    a, b = await asyncio.gather(goods.create_request(db, admin, request_input(linked), cid),
                                goods.create_request(db, admin, request_input(linked), cid))
    assert a == b
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(GoodsRequest)) == 1
        assert await s.scalar(select(func.count()).select_from(ServerOutbox)) == 1


async def test_queue_survives_a_restart(linked, admin, fake_server):
    server = fake_server
    db = linked["db"]
    server.online = False
    request_id = await goods.create_request(db, admin, request_input(linked))
    await linked["engine"].tick(admin)
    server.online = True
    restarted = SyncEngine(db, server.transport())  # the app was closed and opened again
    await restarted.tick(admin)
    assert await _status(db, request_id) == RequestStatus.PENDING and len(server.requests) == 1


async def test_refused_request_is_marked_and_does_not_block_others(linked, admin, monkeypatch):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    bad = await goods.create_request(db, admin, request_input(linked, note="رد کن"))
    good = await goods.create_request(db, admin, request_input(linked, qty=3))
    original = server._create

    def create(body, user):
        if body.get("note") == "رد کن":
            return server._error(422, "items: field required")
        return original(body, user)

    monkeypatch.setattr(server, "_create", create)
    assert (await engine.tick(admin)).state == SyncState.CONNECTED
    assert await _status(db, bad) == RequestStatus.QUEUED
    assert await _status(db, good) == RequestStatus.PENDING
    [row] = [r for r in await goods.list_requests(db, admin) if r.id == bad]
    assert row.last_error == "ارسال نشد: " + ServerRejected(422).message
    calls = len(server.calls)
    await engine.tick(admin)
    assert ("POST", "/requests") not in server.calls[calls:]  # a refused row isn't resent blindly


async def test_not_configured_and_account_rejected(db, admin, request_shop, fake_server):
    shop, server = request_shop, fake_server
    engine = SyncEngine(db, server.transport())
    await goods.create_request(db, admin, request_input(shop))
    assert (await engine.tick(admin)).state == SyncState.NOT_CONFIGURED
    from caspian.services.server_link import connection

    await connection.save_connection(db, admin, "https://caspian.test", "warehouse", "warehouse123")
    server.deactivate("warehouse")
    result = await engine.tick(admin)
    assert result.state == SyncState.ACCOUNT_PROBLEM and "غیرفعال" in result.message
    logins = server.calls.count(("POST", "/auth/login"))
    await engine.tick(admin)  # no hammering the server with a refused account
    assert server.calls.count(("POST", "/auth/login")) == logins
    server.users["warehouse"]["active"] = True
    engine.reset_rejection()
    assert (await engine.tick(admin)).state == SyncState.CONNECTED and len(server.requests) == 1
