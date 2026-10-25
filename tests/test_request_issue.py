"""Approval → draft issue voucher → printing posts it → «issued» reaches the server. The voucher
keeps the approved lines exactly; before approval it can be neither printed nor posted."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from caspian.db.models import (
    DocStatus,
    DocType,
    Document,
    GoodsRequest,
    OutboxOp,
    RequestStatus,
    ServerOutbox,
)
from caspian.services import auth, documents, users
from caspian.services.errors import ValidationError
from caspian.services.server_link import goods
from caspian.services.server_link import linked as linked_rules
from caspian.ui.document_print import document_html
from request_helpers import request_input, stock


async def _approved(linked, admin) -> tuple[int, int]:
    """(request id, draft voucher id) of a request the owner approved."""
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await goods.create_request(db, admin, request_input(linked))
    await engine.tick(admin)
    server.approve(1)
    await engine.tick(admin)
    async with db.session() as s:
        doc_id = await s.scalar(select(Document.id).where(Document.server_request_id == 1))
    return request_id, doc_id


async def test_approval_makes_one_draft_voucher(linked, admin):
    db = linked["db"]
    request_id, doc_id = await _approved(linked, admin)
    await linked["engine"].tick(admin)
    await linked["engine"].tick(admin)
    async with db.session() as s:
        docs = (await s.scalars(select(Document).where(Document.server_request_id == 1))).all()
    assert len(docs) == 1
    detail = await documents.get_document(db, admin, doc_id)
    assert detail.status == DocStatus.DRAFT and detail.input.doc_type == DocType.ISSUE
    assert detail.input.person_id == linked["person"] and detail.input.location_id == linked["location"].id
    assert [(ln.item_id, ln.qty) for ln in detail.lines] == [(linked["bags"], Decimal(20))]
    assert detail.input.usage_note == "فوری"
    row = (await goods.get_request(db, admin, request_id)).row
    assert row.document_id == doc_id
    assert await stock(db, linked["bags"]) == 50  # a draft doesn't touch stock


async def test_no_voucher_before_approval_and_linked_draft_is_guarded(linked, admin):
    db, engine = linked["db"], linked["engine"]
    await goods.create_request(db, admin, request_input(linked))
    await engine.tick(admin)
    async with db.session() as s:
        assert await s.scalar(select(Document.id).where(Document.server_request_id.is_not(None))) is None
    # Even a voucher made by hand for a pending request can't be printed or posted.
    data = documents.DocumentInput(DocType.ISSUE, dt.date.today(), linked["warehouse"],
                                   [documents.LineInput(linked["bags"], linked["unit"], Decimal(20))],
                                   person_id=linked["person"], location_id=linked["location"].id,
                                   server_request_id=1)
    doc_id = await documents.create_document(db, admin, data)
    with pytest.raises(ValidationError) as exc:
        await documents.print_sheet(db, admin, doc_id)
    assert exc.value.message == linked_rules.NOT_APPROVED
    with pytest.raises(ValidationError):
        await documents.post_document(db, admin, doc_id)
    assert await stock(db, linked["bags"]) == 50


async def test_printing_posts_and_reports_issued(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id, doc_id = await _approved(linked, admin)
    sheet = await documents.print_sheet(db, admin, doc_id)
    assert sheet.linked_request and not sheet.trackable
    await documents.post_document(db, admin, doc_id, via="goods_request")
    assert await stock(db, linked["bags"]) == 30
    row = (await goods.get_request(db, admin, request_id)).row
    number = documents.number_text(DocType.ISSUE, (await documents.get_document(db, admin, doc_id)).number)
    assert row.status == RequestStatus.ISSUED and row.issue_number == number and row.waiting
    await engine.tick(admin)
    assert server.requests[1]["status"] == "issued" and server.requests[1]["issue_number"] == number
    row = (await goods.get_request(db, admin, request_id)).row
    assert row.status_name == "حواله صادر شد" and not row.waiting
    # Copies: the first is the original, the next ones carry the duplicate stamp.
    assert await documents.record_print(db, admin, doc_id, "printer") == 1
    sheet = await documents.print_sheet(db, admin, doc_id)
    assert "المثنی" in document_html(sheet, sheet.print_count + 1)


async def test_issued_queued_while_offline_and_retried_safely(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id, doc_id = await _approved(linked, admin)
    server.online = False
    await documents.post_document(db, admin, doc_id)
    await engine.tick(admin)
    assert server.requests[1]["status"] == "approved"
    server.online = True
    server.drop_next = 1  # saved as issued, answer lost: the retry with the same number is fine
    await engine.tick(admin)
    await engine.tick(admin)
    assert server.requests[1]["status"] == "issued"
    async with db.session() as s:
        assert not (await s.scalars(select(ServerOutbox).where(ServerOutbox.op == OutboxOp.ISSUED))).all()
    assert (await goods.get_request(db, admin, request_id)).row.status == RequestStatus.ISSUED


async def test_linked_voucher_keeps_the_approved_lines(linked, admin):
    db = linked["db"]
    _request_id, doc_id = await _approved(linked, admin)
    detail = await documents.get_document(db, admin, doc_id)
    changed = detail.input
    changed.lines = [documents.LineInput(linked["bags"], linked["unit"], Decimal(25))]
    with pytest.raises(ValidationError) as exc:
        await documents.update_document(db, admin, doc_id, detail.version_id, changed)
    assert exc.value.message == linked_rules.LOCKED
    other = (await documents.get_document(db, admin, doc_id)).input
    other.person_id = None
    with pytest.raises(ValidationError):
        await documents.update_document(db, admin, doc_id, detail.version_id, other)
    same = (await documents.get_document(db, admin, doc_id)).input
    same.description = "برای سرویس بهداشتی"  # only the description (and date) may change
    await documents.update_document(db, admin, doc_id, detail.version_id, same)
    with pytest.raises(ValidationError) as exc:
        await documents.delete_draft(db, admin, doc_id)
    assert exc.value.message == linked_rules.NO_DELETE
    # Lines changed behind the service's back are still refused at posting.
    async with db.session() as s:
        doc = await s.get(Document, doc_id)
        doc.lines[0].qty = doc.lines[0].base_qty = Decimal(30)
    with pytest.raises(ValidationError) as exc:
        await documents.post_document(db, admin, doc_id)
    assert exc.value.message == linked_rules.LINES_CHANGED
    assert await stock(db, linked["bags"]) == 50


async def test_old_draft_is_dated_on_the_day_it_is_printed(linked, admin):
    db = linked["db"]
    _request_id, doc_id = await _approved(linked, admin)
    yesterday = dt.date.today() - dt.timedelta(days=1)
    async with db.session() as s:
        doc = await s.get(Document, doc_id)
        doc.doc_date = yesterday
    await documents.post_document(db, admin, doc_id)
    detail = await documents.get_document(db, admin, doc_id)
    from caspian.core import jalali

    expected = dt.date.today() if jalali.fiscal_year_of(dt.date.today()) == jalali.fiscal_year_of(yesterday) \
        else yesterday
    assert detail.input.doc_date == expected


async def test_cancel_when_goods_were_not_delivered(linked, admin):
    db = linked["db"]
    request_id, doc_id = await _approved(linked, admin)
    await documents.post_document(db, admin, doc_id)
    await documents.cancel_document(db, admin, doc_id, "کالا تحویل نشد")
    assert await stock(db, linked["bags"]) == 50
    async with db.session() as s:
        assert (await s.get(GoodsRequest, request_id)).status == RequestStatus.ISSUED


async def test_request_whose_item_was_deactivated(linked, admin):
    """Report gap: the approved request's item is deactivated before its voucher is made. Nothing is
    made, the request says why (no crash), and the voucher comes once the item is active again."""
    from caspian.services import items, protected
    from caspian.services.protected import ProtectedAction
    from conftest import ADMIN_PIN

    db, server, engine = linked["db"], linked["server"], linked["engine"]
    request_id = await goods.create_request(db, admin, request_input(linked))
    await engine.tick(admin)
    approval = await protected.approve(db, admin, ProtectedAction.DEACTIVATE_ITEM, "admin", ADMIN_PIN)
    await items.set_item_active(db, admin, linked["bags"], False, approval)
    server.approve(1)
    result = await engine.tick(admin)
    assert result.events and result.events[0].kind == "approved"
    async with db.session() as s:
        assert await s.scalar(select(Document.id).where(Document.server_request_id == 1)) is None
    row = (await goods.get_request(db, admin, request_id)).row
    assert row.status == RequestStatus.APPROVED and row.document_id is None
    assert "ساخت حواله خروج" in row.last_error and "غیرفعال" in row.last_error
    await items.set_item_active(db, admin, linked["bags"], True)
    await engine.tick(admin)
    row = (await goods.get_request(db, admin, request_id)).row
    assert row.document_id is not None and row.last_error == ""
    assert await stock(db, linked["bags"]) == 50


async def test_voucher_waits_for_someone_allowed_to_make_it(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    await goods.create_request(db, admin, request_input(linked))
    await engine.tick(admin)
    server.approve(1)
    await users.create_user(db, admin, "viewer1", "بیننده", "Str0ngPass1", "viewer")
    viewer = (await auth.login(db, "viewer1", "Str0ngPass1")).actor
    await engine.tick(viewer)
    async with db.session() as s:
        assert await s.scalar(select(Document.id).where(Document.server_request_id == 1)) is None
    await engine.tick(admin)
    async with db.session() as s:
        assert await s.scalar(select(Document.id).where(Document.server_request_id == 1)) is not None
