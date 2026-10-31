"""Posting in tests the way the app does it now.

Issue and loan-out slips are posted only when an owner approves them in Caspian Owner. Tests that
just need such a slip posted send it with «ارسال درخواست», play the owner's approval and let the
same code the sync runs post it. Other document types are posted directly, as before.
"""

from sqlalchemy import delete, select

from caspian.core.permissions import Perm
from caspian.db.models import Document, GoodsRequest, RequestStatus, ServerOutbox
from caspian.services import documents
from caspian.services.server_link import goods


async def approve(db, request_id: int) -> None:
    """The owner's «تأیید» as the sync stores it (the outbox is considered delivered)."""
    async with db.session() as s:
        req = await s.get(GoodsRequest, request_id)
        req.status = RequestStatus.APPROVED
        req.server_id = req.server_id or 900_000 + request_id
        await s.execute(delete(ServerOutbox).where(ServerOutbox.request_id == request_id))


async def post_approved(db, doc_id: int) -> None:
    """What `issue_link.post_approved_slips` does for one slip, letting its errors through."""
    async with db.session() as s:
        doc = await s.scalar(select(Document).where(Document.id == doc_id).with_for_update())
        req = await documents.request_of_in(s, doc_id)
        await documents.post_approved_in(s, doc, req)


async def approve_slip(db, actor, doc_id: int) -> None:
    actor.require(Perm.DOCUMENTS_POST)  # tests written for posting keep its permission
    await approve(db, await goods.submit_document(db, actor, doc_id))
    await post_approved(db, doc_id)


async def post_document(db, actor, doc_id: int, expected_version: int | None = None, approval=None,
                        via: str | None = None) -> None:
    async with db.session() as s:
        doc = await s.get(Document, doc_id)
        direct = doc is None or doc.doc_type not in documents.NEEDS_OWNER or doc.server_request_id is not None
    if direct:
        await documents.post_document(db, actor, doc_id, expected_version, approval, via=via)
    else:
        await approve_slip(db, actor, doc_id)


async def create_and_post(db, actor, data: documents.DocumentInput, approval=None) -> int:
    if data.doc_type not in documents.NEEDS_OWNER:
        return await documents.create_and_post(db, actor, data, approval)
    actor.require(Perm.DOCUMENTS_POST)
    doc_id = await documents.create_document(db, actor, data)
    await approve_slip(db, actor, doc_id)
    return doc_id
