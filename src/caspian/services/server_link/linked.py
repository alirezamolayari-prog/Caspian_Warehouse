"""Rules for an issue voucher made from an approved goods request (used by services.documents).

Such a voucher keeps exactly the approved lines, recipient and place; it can be printed (and so
posted) only while its request is approved, and posting it queues «issued» for the server.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.db.models import Document, GoodsRequest, OutboxOp, RequestStatus, ServerOutbox
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import ValidationError

NOT_APPROVED = ("این حواله از درخواست کالا ساخته شده و هنوز تأیید مالک را ندارد؛ "
                "پس از تأیید قابل چاپ و ثبت است.")
LOCKED = ("این حواله از درخواست تأییدشده ساخته شده است؛ اقلام، مقدارها، تحویل‌گیرنده، محل مصرف و انبار "
          "آن قابل تغییر نیست.")
NO_DELETE = ("این حواله به درخواست کالای تأییدشده وصل است و حذف نمی‌شود. اگر کالا تحویل نشد، "
             "پس از چاپ آن را ابطال کنید.")
LINES_CHANGED = "اقلام این حواله با درخواست تأییدشده یکی نیست و ثبت نمی‌شود."
MISSING = "درخواست کالای این حواله در این برنامه پیدا نشد."


async def linked_request_in(s: AsyncSession, doc: Document) -> GoodsRequest | None:
    if doc.server_request_id is None:
        return None
    return await s.scalar(select(GoodsRequest).where(GoodsRequest.server_id == doc.server_request_id))


def _key(lines) -> list[tuple]:
    return sorted((ln.item_id, ln.unit_id, ln.qty) for ln in lines)


def check_postable(req: GoodsRequest | None, doc: Document) -> GoodsRequest:
    if req is None or req.document_id is not None:  # a slip sent as a request is posted only by the sync
        raise ValidationError(MISSING)
    if req.status != RequestStatus.APPROVED:
        raise ValidationError(NOT_APPROVED)
    if _key(req.lines) != _key(doc.lines) or req.person_id != doc.person_id:
        raise ValidationError(LINES_CHANGED)
    return req


def mark_issued_in(s: AsyncSession, actor: Actor, req: GoodsRequest, issue_number: str) -> None:
    """Inside the posting transaction: the request is issued here at once; the server hears it via
    the outbox (sent now if online, later if not)."""
    req.status = RequestStatus.ISSUED
    req.issue_number = issue_number
    req.last_error = ""
    s.add(ServerOutbox(request_id=req.id, op=OutboxOp.ISSUED, payload={"issue_number": issue_number},
                       user_id=actor.user_id))
    audit.record(s, actor, "goods_request.issued", "goods_request", req.id,
                 {"server_id": req.server_id, "issue_number": issue_number})
