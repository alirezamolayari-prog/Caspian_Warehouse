"""Polling `/changes`: decisions and messages arrive by id (overlaps never duplicate), owners stay
anonymous, rejected requests can be corrected and sent again."""

import asyncio
from decimal import Decimal

import httpx
from sqlalchemy import func, select

from caspian.db.models import AppSetting, GoodsRequestMessage, RequestStatus
from caspian.services.server_link import goods
from caspian.services.server_link.sync import SYNC_KEY, SyncEngine, SyncRunner, SyncState
from request_helpers import request_input


async def _sent(linked, admin, **changes) -> int:
    request_id = await goods.create_request(linked["db"], admin, request_input(linked, **changes))
    await linked["engine"].tick(admin)
    return request_id


async def test_approval_arrives_with_a_notification(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await _sent(linked, admin)
    async with db.session() as s:
        assert (await s.get(AppSetting, SYNC_KEY)).value["next_since"]
    server.approve(1)
    result = await engine.tick(admin)
    assert [(e.request_id, e.kind) for e in result.events] == [(request_id, "approved")]
    [row] = await goods.list_requests(db, admin)
    assert row.status == RequestStatus.APPROVED and row.unseen and row.status_name == "تأیید شد"
    assert await goods.unseen_count(db) == 1
    await goods.mark_seen(db, admin, request_id)
    assert await goods.unseen_count(db) == 0
    assert not (await engine.tick(admin)).events  # the 2 s overlap repeats rows: no second notice


async def test_rejection_then_corrected_and_resubmitted(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await _sent(linked, admin)
    server.reject(1, "مقدار زیاد است")
    result = await engine.tick(admin)
    assert result.events[0].kind == "rejected"
    detail = await goods.get_request(db, admin, request_id)
    assert detail.row.rejection_reason == "مقدار زیاد است" and detail.row.editable
    data = detail.input
    data.lines[0].qty = Decimal(8)
    await goods.resubmit_request(db, admin, request_id, data)
    assert (await goods.get_request(db, admin, request_id)).row.status == RequestStatus.QUEUED
    await engine.tick(admin)
    assert server.requests[1]["status"] == "pending" and server.requests[1]["items"][0]["quantity"] == 8
    assert (await goods.get_request(db, admin, request_id)).row.status == RequestStatus.PENDING
    assert len(server.requests) == 1


async def test_resubmit_retry_after_lost_answer(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await _sent(linked, admin)
    server.reject(1, "کم کنید")
    await engine.tick(admin)
    data = (await goods.get_request(db, admin, request_id)).input
    await goods.resubmit_request(db, admin, request_id, data)
    server.drop_next = 1
    await engine.tick(admin)  # saved on the server, answer lost; the retry gets 409 → checked
    await engine.tick(admin)
    row = (await goods.get_request(db, admin, request_id)).row
    assert row.status == RequestStatus.PENDING and not row.last_error and not row.waiting


async def test_messages_by_id_owner_anonymous_and_no_duplicates(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await _sent(linked, admin)
    server.owner_message(1, "برای کدام طبقه است؟")
    result = await engine.tick(admin)
    assert result.events[0].kind == "message"
    await engine.tick(admin)  # overlap: the same message comes again
    await goods.queue_message(db, admin, request_id, "طبقه همکف")
    server.drop_next = 1
    await engine.tick(admin)  # our message reached the server but the answer was lost
    await engine.tick(admin)
    rows = await goods.list_messages(db, admin, request_id)
    assert [(m.sender, m.text, m.mine) for m in rows] == [
        ("مالک", "برای کدام طبقه است؟", False), (admin.display_name, "طبقه همکف", True)]
    assert not any(m.queued for m in rows)
    assert sum(1 for m in server.messages if m["text"] == "طبقه همکف") == 1
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(GoodsRequestMessage)) == 2


async def test_message_queued_while_offline(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await _sent(linked, admin)
    server.online = False
    await goods.queue_message(db, admin, request_id, "سلام")
    await engine.tick(admin)
    assert (await goods.list_messages(db, admin, request_id))[0].queued
    server.online = True
    await engine.tick(admin)
    assert not (await goods.list_messages(db, admin, request_id))[0].queued
    assert [m["text"] for m in server.messages] == ["سلام"]


async def test_request_registered_elsewhere_is_shown_read_only(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    server._create({"client_request_id": None, "requester_name": "مهمان", "consumption_location": "دفتر",
                    "items": [{"item_code": "1024", "name": "کیسه زباله", "quantity": 2, "unit": "عدد"}]})
    await engine.tick(admin)
    [row] = await goods.list_requests(db, admin)
    assert row.origin == "remote" and row.requester == "مهمان" and row.lines[0].item_id == linked["bags"]
    server.approve(1)
    await engine.tick(admin)
    row = (await goods.list_requests(db, admin))[0]
    assert row.status == RequestStatus.APPROVED and row.document_id is None  # not issued from here
    assert not row.editable


async def test_runner_keeps_running_in_the_background(linked, admin):
    results = []
    gate = asyncio.Event()
    server = linked["server"]

    async def slow(request: httpx.Request) -> httpx.Response:
        await gate.wait()  # the server takes its time; nothing else may wait for it
        return server.handle(request)

    engine = SyncEngine(linked["db"], httpx.MockTransport(slow))
    runner = SyncRunner(engine, results.append, interval=0.05)
    runner.set_actor(admin)
    runner.start()
    try:
        await goods.create_request(linked["db"], admin, request_input(linked))  # not blocked
        await asyncio.sleep(0.1)
        assert not results  # the tick is still waiting for the server
        gate.set()
        for _ in range(200):
            if server.requests:  # picked up by the tick running then or the next one
                break
            await asyncio.sleep(0.02)
        assert results[0].state == SyncState.CONNECTED and len(server.requests) == 1
    finally:
        await runner.stop()
