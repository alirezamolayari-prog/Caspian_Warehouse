"""PR #6 + PR #7 together: posting a voucher made from a goods request goes through every check of
`documents.post_document_in` — the request rules (approved only, approved lines, «issued» queued),
the unit-factor check (report #3) and the live account check (report #1/#2)."""

from decimal import Decimal

import pytest
from sqlalchemy import select

import approval_helpers as slips
from caspian.db.models import DocStatus, Document, OutboxOp, RequestStatus, ServerOutbox
from caspian.services import auth, documents, items, master, users
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.server_link import goods
from caspian.services.server_link import linked as linked_rules
from request_helpers import stock

PW = "Str0ngPass1"


@pytest.fixture
async def cartons(linked, admin):
    """Bags also come in cartons of 10; the request asks for 2 cartons."""
    db = linked["db"]
    units = {u.name: u.id for u in await master.list_units(db)}
    detail = await items.get_item(db, admin, linked["bags"])
    data = detail.input
    data.units = [(units["کارتن"], Decimal(10))]
    await items.update_item(db, admin, linked["bags"], detail.version_id, data)
    request = goods.RequestInput(linked["person"], linked["location"].id, None, "",
                                 [goods.RequestLineInput(linked["bags"], units["کارتن"], Decimal(2))])
    request_id = await goods.create_request(db, admin, request)
    await linked["engine"].tick(admin)
    return {**linked, "carton": units["کارتن"], "request_id": request_id}


async def _approve(shop, admin) -> int:
    shop["server"].approve(1)
    await shop["engine"].tick(admin)
    async with shop["db"].session() as s:
        return await s.scalar(select(Document.id).where(Document.server_request_id == 1))


async def test_approved_voucher_posts_and_queues_issued(cartons, admin):
    db = cartons["db"]
    doc_id = await _approve(cartons, admin)
    await slips.post_document(db, admin, doc_id, via="goods_request")
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.POSTED
    assert await stock(db, cartons["bags"]) == 30  # 2 cartons × 10
    async with db.session() as s:
        ops = (await s.scalars(select(ServerOutbox.op))).all()
    assert OutboxOp.ISSUED in ops
    row = (await goods.get_request(db, admin, cartons["request_id"])).row
    assert row.status == RequestStatus.ISSUED


async def test_changed_factor_blocks_the_linked_voucher(cartons, admin):
    db = cartons["db"]
    doc_id = await _approve(cartons, admin)
    detail = await items.get_item(db, admin, cartons["bags"])
    data = detail.input
    data.units = [(cartons["carton"], Decimal(12))]
    await items.update_item(db, admin, cartons["bags"], detail.version_id, data)
    with pytest.raises(ValidationError) as exc:
        await slips.post_document(db, admin, doc_id)
    assert "ضریب" in exc.value.message and "۱۰" in exc.value.message and "۱۲" in exc.value.message
    assert await stock(db, cartons["bags"]) == 50
    assert (await goods.get_request(db, admin, cartons["request_id"])).row.status == RequestStatus.APPROVED


async def test_deactivated_user_cannot_post_the_linked_voucher(cartons, admin):
    db = cartons["db"]
    doc_id = await _approve(cartons, admin)
    await users.create_user(db, admin, "manager1", "مدیر", PW, "manager")
    manager = (await auth.login(db, "manager1", PW)).actor
    await users.set_active(db, admin, manager.user_id, False)
    with pytest.raises(ServiceError) as exc:
        await slips.post_document(db, manager, doc_id)
    assert "غیرفعال" in exc.value.message
    assert await stock(db, cartons["bags"]) == 50
    assert (await goods.get_request(db, admin, cartons["request_id"])).row.status == RequestStatus.APPROVED


async def test_unapproved_request_blocks_posting(cartons, admin):
    db = cartons["db"]
    detail = await goods.get_request(db, admin, cartons["request_id"])
    data = documents.DocumentInput(
        documents.DocType.ISSUE, documents.dt.date.today(), cartons["warehouse"],
        [documents.LineInput(cartons["bags"], cartons["carton"], Decimal(2))],
        person_id=cartons["person"], location_id=cartons["location"].id,
        server_request_id=detail.row.server_id)
    doc_id = await documents.create_document(db, admin, data)
    with pytest.raises(ValidationError) as exc:
        await slips.post_document(db, admin, doc_id)
    assert exc.value.message == linked_rules.NOT_APPROVED
    assert await stock(db, cartons["bags"]) == 50


async def test_deactivated_user_cannot_register_or_send_requests(cartons, admin):
    db = cartons["db"]
    await users.create_user(db, admin, "manager3", "مدیر", PW, "manager")
    manager = (await auth.login(db, "manager3", PW)).actor
    await users.set_active(db, admin, manager.user_id, False)
    data = (await goods.get_request(db, admin, cartons["request_id"])).input
    for action in (lambda: goods.create_request(db, manager, data),
                   lambda: goods.queue_message(db, manager, cartons["request_id"], "سلام")):
        with pytest.raises(ServiceError) as exc:
            await action()
        assert "غیرفعال" in exc.value.message
    assert len(await goods.list_requests(db, admin)) == 1


async def test_deactivated_user_in_background_sync_leaves_no_false_error(cartons, admin):
    db = cartons["db"]
    await users.create_user(db, admin, "manager2", "مدیر", PW, "manager")
    manager = (await auth.login(db, "manager2", PW)).actor
    await users.set_active(db, admin, manager.user_id, False)
    cartons["server"].approve(1)
    await cartons["engine"].tick(manager)  # the stale session can't make the voucher...
    row = (await goods.get_request(db, admin, cartons["request_id"])).row
    assert row.document_id is None and row.last_error == ""  # ...and the request isn't blamed
    await cartons["engine"].tick(admin)
    assert (await goods.get_request(db, admin, cartons["request_id"])).row.document_id is not None
