"""Approved requests → stock leaves the warehouse.

A slip sent with «ارسال درخواست» (`GoodsRequest.document_id`) is posted here, automatically, as soon
as its approval has arrived — the only way an issue / loan-out slip is ever posted.

Older requests registered on their own get a draft issue voucher («حواله خروج»), made once per request.

The draft carries the request's server id, the requester as recipient, the picked place and exactly
the approved lines. It is made as the signed-in person (who needs `documents.edit`); while nobody
with that right is signed in, approved requests simply wait. If it can't be made (an item was
deactivated, the fiscal year is closed…) the reason is shown on the request.
"""

import datetime as dt
import logging

from sqlalchemy import exists, select
from sqlalchemy.exc import OperationalError

from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.db.database import Database
from caspian.db.models import DocStatus, DocType, Document, GoodsRequest, RequestStatus
from caspian.services import documents
from caspian.services.access import ReloginRequired
from caspian.services.actor import Actor
from caspian.services.errors import ServiceError

log = logging.getLogger(__name__)


def _input_of(req: GoodsRequest) -> documents.DocumentInput:
    return documents.DocumentInput(
        DocType.ISSUE, dt.date.today(), req.warehouse_id,
        [documents.LineInput(ln.item_id, ln.unit_id, ln.qty) for ln in req.lines],
        person_id=req.person_id, description=f"درخواست کالا شماره {to_persian_digits(req.server_id)}",
        location_id=req.location_id, floor_id=req.floor_id, usage_note=req.note[:500],
        server_request_id=req.server_id)


async def ensure_issue_drafts(db: Database, actor: Actor | None) -> list[int]:
    """Make the missing draft vouchers of approved requests; returns the new document ids."""
    if actor is None or actor.is_ai or not actor.can(Perm.DOCUMENTS_EDIT):
        return []
    async with db.session() as s:
        waiting = list((await s.scalars(select(GoodsRequest).where(
            GoodsRequest.status == RequestStatus.APPROVED, GoodsRequest.origin == "local",
            GoodsRequest.server_id.is_not(None), GoodsRequest.document_id.is_(None),
            ~exists().where(Document.server_request_id == GoodsRequest.server_id)))).all())
    created = []
    for req in waiting:
        try:
            doc_id = await _make(db, actor, req)
        except ReloginRequired:
            # The signed-in user was deactivated or changed role: not the request's fault. Stop; the
            # window sends the user to the login screen and the next signed-in person makes it.
            return created
        except ServiceError as exc:
            log.warning("Issue voucher for request %s not made: %s", req.server_id, exc.message)
            await _set_error(db, req.id, f"ساخت حواله خروج: {exc.message}")
            continue
        if doc_id is not None:
            created.append(doc_id)
    return created


async def _make(db: Database, actor: Actor, req: GoodsRequest) -> int | None:
    data = _input_of(req)

    async def attempt() -> int | None:
        async with db.session(actor) as s:
            # Another PC (or an earlier attempt) may have made it meanwhile.
            if await s.scalar(select(Document.id).where(Document.server_request_id == req.server_id)):
                return None
            doc = await documents.create_document_in(s, actor, data)
            row = await s.get(GoodsRequest, req.id)
            row.last_error = ""
            return doc.id

    return await documents._with_retries(attempt, numbering=True)


async def post_approved_slips(db: Database) -> list[int]:
    """Post every slip whose request the owners approved; returns the posted document ids. A slip
    that can't be posted yet (a stocktake in progress, stock taken meanwhile…) keeps its reason on the
    request and is tried again on the next round."""
    async with db.session() as s:
        waiting = list((await s.execute(
            select(GoodsRequest.id, Document.id).join(Document, GoodsRequest.document_id == Document.id)
            .where(GoodsRequest.status == RequestStatus.APPROVED, Document.status == DocStatus.DRAFT)
            .order_by(GoodsRequest.decided_at, GoodsRequest.id))).all())
    posted = []
    for request_id, doc_id in waiting:
        try:
            if await _post_one(db, request_id, doc_id):
                posted.append(doc_id)
        except ServiceError as exc:
            log.warning("Approved slip %s not posted: %s", doc_id, exc.message)
            await _set_error(db, request_id, f"ثبت خودکار پس از تأیید انجام نشد: {exc.message}")
    return posted


async def _post_one(db: Database, request_id: int, doc_id: int) -> bool:
    async def attempt() -> bool:
        async with db.session() as s:
            # Another PC may be posting the same slip: the row lock makes one of them wait and skip.
            doc = await s.scalar(select(Document).where(Document.id == doc_id).with_for_update())
            req = await s.get(GoodsRequest, request_id)
            if doc is None or req is None or doc.status != DocStatus.DRAFT:
                return False
            if req.status != RequestStatus.APPROVED or req.document_id != doc.id:
                return False
            await documents.post_approved_in(s, doc, req)
            req.last_error = ""
            return True

    try:
        return await documents._with_retries(attempt)
    except OperationalError:
        log.exception("Posting approved slip %s failed", doc_id)
        return False


async def _set_error(db: Database, request_id: int, text: str) -> None:
    async with db.session() as s:
        row = await s.get(GoodsRequest, request_id)
        if row is not None:
            row.last_error = text
