"""Issue and loan-out slips leave the warehouse only with an owner's approval in Caspian Owner.

The slip itself is the request: «ارسال درخواست» queues it for the server, the owners decide on their
phones, and the approval posts it automatically. Nobody (admin included) can post it otherwise; a
pending slip is locked and its goods are reserved; a rejected one is fixed and sent again.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from caspian.db.models import AuditLog, DocStatus, DocType, GoodsRequest, RequestStatus, ServerOutbox
from caspian.services import auth, documents, users
from caspian.services.errors import PermissionDenied, ValidationError
from caspian.services.server_link import goods
from request_helpers import stock

PW = "Str0ngPass1"


def _slip(shop, qty: int = 20, doc_type: DocType = DocType.ISSUE,
          item: str = "bags") -> documents.DocumentInput:
    return documents.DocumentInput(
        doc_type, dt.date.today(), shop["warehouse"],
        [documents.LineInput(shop[item], shop["unit"], Decimal(qty))], person_id=shop["person"],
        location_id=shop["location"].id if doc_type == DocType.ISSUE else None, usage_note="نظافت")


async def _request(db, doc_id: int) -> GoodsRequest:
    async with db.session() as s:
        return await s.scalar(select(GoodsRequest).where(GoodsRequest.document_id == doc_id))


async def _send(linked, admin, qty: int = 20, **kw) -> int:
    db = linked["db"]
    doc_id = await documents.create_document(db, admin, _slip(linked, qty, **kw))
    await goods.submit_document(db, admin, doc_id)
    return doc_id


async def test_full_cycle_send_pending_approve_posts_and_prints(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    doc_id = await _send(linked, admin)
    row = (await documents.list_documents(db, admin, DocType.ISSUE))[0]
    assert row.approval == RequestStatus.QUEUED and row.tone == "pending" and row.status_name == "در صف ارسال"
    with pytest.raises(ValidationError):  # nothing printable before the approval
        await documents.print_sheet(db, admin, doc_id)

    await engine.tick(admin)
    [sent] = server.requests.values()
    assert sent["status"] == "pending" and "حواله خروج" in sent["note"]
    row = (await documents.list_documents(db, admin, DocType.ISSUE))[0]
    assert row.approval == RequestStatus.PENDING and row.tone == "pending"
    assert "در انتظار تأیید" in row.status_name
    assert await stock(db, linked["bags"]) == 50  # nothing leaves while pending

    server.approve(sent["id"])
    await engine.tick(admin)
    detail = await documents.get_document(db, admin, doc_id)
    assert detail.status == DocStatus.POSTED and await stock(db, linked["bags"]) == 30
    row = (await documents.list_documents(db, admin, DocType.ISSUE))[0]
    assert row.tone == "ok" and row.status_name == "ثبت نهایی"
    sheet = await documents.print_sheet(db, admin, doc_id)
    assert sheet.posted_by == admin.display_name  # the sender; the approver is never on paper
    await engine.tick(admin)
    assert server.requests[sent["id"]]["status"] == "issued"
    async with db.session() as s:
        posted = await s.scalar(select(AuditLog).where(AuditLog.action == "document.posted")
                                .order_by(AuditLog.id.desc()))
    assert posted.details["via"] == "owner_approval"


async def test_reject_shows_reason_then_edit_and_resend(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    doc_id = await _send(linked, admin)
    await engine.tick(admin)
    server.reject(1, "زیاد است")
    await engine.tick(admin)
    row = (await documents.list_documents(db, admin, DocType.ISSUE))[0]
    assert row.approval == RequestStatus.REJECTED and row.tone == "bad" and row.rejection_reason == "زیاد است"
    detail = await documents.get_document(db, admin, doc_id)
    data = detail.input
    data.lines[0].qty = Decimal(5)
    await documents.update_document(db, admin, doc_id, detail.version_id, data)  # editable again
    await goods.submit_document(db, admin, doc_id)
    await engine.tick(admin)
    assert len(server.requests) == 1  # the same request, resubmitted (PUT)
    assert server.requests[1]["status"] == "pending" and server.requests[1]["items"][0]["quantity"] == 5
    server.approve(1)
    await engine.tick(admin)
    assert await stock(db, linked["bags"]) == 45


async def test_nobody_posts_an_issue_directly_not_even_admin(linked, admin):
    db = linked["db"]
    assert {DocType.ISSUE, DocType.LOAN_OUT} == documents.NEEDS_OWNER
    doc_id = await documents.create_document(db, admin, _slip(linked, 1))
    with pytest.raises(ValidationError) as exc:
        await documents.post_document(db, admin, doc_id)
    assert exc.value.message == documents.OWNER_ONLY
    with pytest.raises(ValidationError):
        await documents.create_and_post(db, admin, _slip(linked, 1))
    assert await stock(db, linked["bags"]) == 50


async def test_pending_slip_is_locked_and_cannot_be_deleted(linked, admin):
    db = linked["db"]
    doc_id = await _send(linked, admin)
    detail = await documents.get_document(db, admin, doc_id)
    assert detail.awaiting_owner
    with pytest.raises(ValidationError) as exc:
        await documents.update_document(db, admin, doc_id, detail.version_id, detail.input)
    assert exc.value.message == documents.AWAITING
    with pytest.raises(ValidationError):
        await documents.delete_draft(db, admin, doc_id)
    assert await goods.submit_document(db, admin, doc_id) == (await _request(db, doc_id)).id  # no 2nd send


async def test_posted_slip_cannot_be_reopened_and_changed(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    doc_id = await _send(linked, admin)
    await engine.tick(admin)
    server.approve(1)
    await engine.tick(admin)
    detail = await documents.get_document(db, admin, doc_id)
    data = detail.input
    data.lines[0].qty = Decimal(40)
    with pytest.raises(ValidationError):
        await documents.update_document(db, admin, doc_id, detail.version_id, data)
    with pytest.raises(ValidationError):
        await goods.submit_document(db, admin, doc_id)
    assert await stock(db, linked["bags"]) == 30


async def test_withdraw_before_sending_drops_it_quietly(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    doc_id = await _send(linked, admin)
    await goods.withdraw_request(db, admin, doc_id)
    detail = await documents.get_document(db, admin, doc_id)
    assert detail.approval is None and not detail.awaiting_owner  # a plain, editable draft again
    await engine.tick(admin)
    assert not server.requests and not server.messages


async def test_withdraw_while_pending_tells_owners_and_ignores_a_later_approval(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    doc_id = await _send(linked, admin)
    await engine.tick(admin)
    await goods.withdraw_request(db, admin, doc_id)
    await engine.tick(admin)
    assert server.messages[-1]["text"] == goods.WITHDRAWN_MESSAGE
    server.approve(1)  # too late: the warehouse took it back
    await engine.tick(admin)
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.DRAFT
    assert await stock(db, linked["bags"]) == 50
    # sent again later as a new request
    await goods.submit_document(db, admin, doc_id)
    await engine.tick(admin)
    assert len(server.requests) == 2


async def test_only_the_sender_or_the_admin_withdraws(linked, admin):
    db = linked["db"]
    await users.create_user(db, admin, "keeper1", "انباردار", PW, "storekeeper")
    await users.create_user(db, admin, "keeper2", "انباردار ۲", PW, "storekeeper")
    keeper = (await auth.login(db, "keeper1", PW)).actor
    other = (await auth.login(db, "keeper2", PW)).actor
    first = await documents.create_document(db, keeper, _slip(linked, 1))
    await goods.submit_document(db, keeper, first)
    with pytest.raises(PermissionDenied):
        await goods.withdraw_request(db, other, first)
    await goods.withdraw_request(db, keeper, first)
    second = await documents.create_document(db, keeper, _slip(linked, 1))
    await goods.submit_document(db, keeper, second)
    await goods.withdraw_request(db, admin, second)


async def test_pending_goods_are_reserved(linked, admin):
    db = linked["db"]
    await _send(linked, admin, qty=40)  # 50 in stock, 40 held
    second = await documents.create_document(db, admin, _slip(linked, 20))
    problems = await documents.check(db, admin, (await documents.get_document(db, admin, second)).input,
                                     second)
    assert problems and "رزرو" in problems[0].message
    with pytest.raises(ValidationError):
        await goods.submit_document(db, admin, second)
    await goods.submit_document(db, admin, await documents.create_document(db, admin, _slip(linked, 10)))


async def test_offline_stays_queued_and_never_posts_locally(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    server.online = False
    doc_id = await _send(linked, admin)
    await engine.tick(admin)
    assert (await _request(db, doc_id)).status == RequestStatus.QUEUED
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.DRAFT
    server.online = True
    await engine.tick(admin)
    assert (await _request(db, doc_id)).status == RequestStatus.PENDING
    async with db.session() as s:
        assert not await s.scalar(select(func.count()).select_from(ServerOutbox))


async def test_approval_during_a_stocktake_waits_for_it(linked, admin):
    from caspian.services import stocktake

    db, server, engine = linked["db"], linked["server"], linked["engine"]
    doc_id = await _send(linked, admin)
    await engine.tick(admin)
    sid = await stocktake.create_stocktake(db, admin, linked["warehouse"])
    server.approve(1)
    await engine.tick(admin)
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.DRAFT
    assert "انبارگردانی" in (await _request(db, doc_id)).last_error
    await stocktake.cancel(db, admin, sid)
    await engine.tick(admin)
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.POSTED


async def test_assistant_cannot_send_or_withdraw(linked, admin):
    db = linked["db"]
    doc_id = await documents.create_document(db, admin, _slip(linked, 1))
    with pytest.raises(PermissionDenied):
        await goods.submit_document(db, admin.as_ai(), doc_id)
    await goods.submit_document(db, admin, doc_id)
    with pytest.raises(PermissionDenied):
        await goods.withdraw_request(db, admin.as_ai(), doc_id)


async def test_other_documents_post_as_before(linked, admin):
    db = linked["db"]
    doc_id = await documents.create_and_post(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), linked["warehouse"],
        [documents.LineInput(linked["bags"], linked["unit"], Decimal(5))]))
    assert (await documents.get_document(db, admin, doc_id)).status == DocStatus.POSTED
    assert await stock(db, linked["bags"]) == 55


# ----- UI: labels and colours -----


async def test_editor_and_list_show_the_approval_state(qtbot, themes, linked, admin):
    from caspian.core.settings import Settings
    from caspian.db.database import DbConfig
    from caspian.services import master
    from caspian.ui.app_context import AppContext
    from caspian.ui.documents_page import DocumentDialog, DocumentsList
    from caspian.ui.widgets import BADGE_ROLE

    db = linked["db"]
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    lists = (await master.list_warehouses(db), await master.search_persons(db), await master.list_units(db))
    new = DocumentDialog(ctx, DocType.ISSUE, None, *lists, [])
    qtbot.addWidget(new)
    assert new.submit_button.text() == "ارسال درخواست"
    receipt = DocumentDialog(ctx, DocType.RECEIPT, None, *lists, [])
    qtbot.addWidget(receipt)
    assert receipt.submit_button.text() == "ذخیره و ثبت نهایی"

    draft = await documents.create_document(db, admin, _slip(linked, 1))
    sent = await _send(linked, admin, qty=2)
    page = DocumentsList(ctx)
    qtbot.addWidget(page)
    await page.refresh()
    def badge(doc_id: int) -> str:
        page.table.select_id(doc_id)
        return page.table.item(page.table.currentRow(), 6).data(BADGE_ROLE)

    assert badge(draft) == "draft" and badge(sent) == "pending"  # grey draft, lemon waiting
    assert not page.post_button.isEnabled() and not page.print_button.isEnabled()
    assert page.withdraw_button.isEnabled() and page.open_button.text() == "مشاهده"
    pending = DocumentDialog(ctx, DocType.ISSUE, await documents.get_document(db, admin, sent), *lists, [])
    qtbot.addWidget(pending)
    assert pending.submit_button.isHidden() and not pending.print_button.isEnabled()
