"""Goods requests on this PC («درخواست کالا»).

Registering a request only writes locally (status QUEUED + an outbox row) and returns at once; the
background sync sends it. The requester, place and items are picked from this app's own lists;
the server receives their texts (name, phone, «پلاک ۶۸ · طبقه همکف», item code/name/unit) and the
ids stay here for the issue voucher. The assistant may only prepare a DRAFT request, which the person
sends with «بله» on the confirmation card.
"""

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.db.database import Database
from caspian.db.models import (
    DocStatus,
    Document,
    GoodsRequest,
    GoodsRequestLine,
    GoodsRequestMessage,
    Item,
    OutboxOp,
    Person,
    RequestStatus,
    ServerOutbox,
    StockBalance,
    Unit,
    User,
    Warehouse,
)
from caspian.db.retry import with_retries
from caspian.services import audit, documents, locations
from caspian.services.actor import Actor
from caspian.services.documents import Problem
from caspian.services.errors import NotFound, PermissionDenied, ServiceError, ValidationError

STATUS_NAMES = {
    RequestStatus.DRAFT: "پیش‌نویس دستیار",
    RequestStatus.QUEUED: "در صف ارسال",
    RequestStatus.PENDING: "در انتظار تأیید مالک",
    RequestStatus.APPROVED: "تأیید شد",
    RequestStatus.REJECTED: "رد شد",
    RequestStatus.ISSUED: "حواله صادر شد",
    RequestStatus.WITHDRAWN: "لغو شد",
}
OWNER_NAME = "مالک"  # owners are never named on this side (the server hides who decided)
MAX_TEXT = 4000
_PHONE_CHARS = re.compile(r"[^0-9۰-۹+\-() ]")


@dataclass
class RequestLineInput:
    item_id: int
    unit_id: int
    qty: Decimal


@dataclass
class RequestInput:
    person_id: int | None
    location_id: int | None
    floor_id: int | None = None
    note: str = ""
    lines: list[RequestLineInput] = field(default_factory=list)
    warehouse_id: int | None = None  # where the goods will come from; default: the first warehouse


@dataclass(frozen=True)
class RequestLineView:
    item_id: int | None
    unit_id: int | None
    qty: Decimal
    code: str
    name: str
    unit: str

    @property
    def text(self) -> str:
        code = f" (کد {to_persian_digits(self.code)})" if self.code else ""
        return f"{format_qty(self.qty)} {self.unit} {self.name}{code}"


@dataclass(frozen=True)
class RequestRow:
    id: int
    server_id: int | None
    status: RequestStatus
    origin: str
    requester: str
    phone: str
    location: str
    note: str
    lines: tuple[RequestLineView, ...]
    created_at: object
    unseen: bool
    rejection_reason: str
    issue_number: str
    last_error: str
    waiting: bool  # something of this request is still in the outbox
    document_id: int | None = None
    document_status: DocStatus | None = None
    failed: bool = False  # the server refused something of it: «ارسال مجدد» puts it back in the queue
    created_by: str = ""  # «ثبت‌کننده»: the server's name for the registrar, else the local author's
    mine: bool = False  # registered by the person looking at the list
    slip: bool = False  # the request IS this slip (sent with «ارسال درخواست»); posted on approval

    @property
    def number_text(self) -> str:
        return to_persian_digits(self.server_id) if self.server_id else "در صف"

    @property
    def title(self) -> str:
        """«درخواست شمارهٔ ۱۲ — علی رضایی» / «درخواست در صف ارسال — علی رضایی»."""
        head = (f"درخواست شمارهٔ {to_persian_digits(self.server_id)}" if self.server_id
                else "درخواست در صف ارسال")
        return f"{head} — {self.requester}" if self.requester else head

    @property
    def send_status(self) -> str:
        """«وضعیت ارسال»: what happened to it on the way to the owners and back (not the user's note)."""
        if self.last_error:
            return self.last_error
        if self.origin == "remote":
            return "ثبت‌شده خارج از این برنامه (فقط نمایش)"
        if self.status == RequestStatus.DRAFT:
            return "منتظر تأیید شما"
        if self.status == RequestStatus.QUEUED or (self.waiting and self.status != RequestStatus.ISSUED):
            return "در صف ارسال"
        if self.status == RequestStatus.APPROVED and self.slip:
            return "تأیید شد؛ حواله خودکار ثبت نهایی می‌شود"
        if self.status == RequestStatus.APPROVED and self.document_id is None:
            return "حواله خروج به‌زودی ساخته می‌شود"
        if self.status == RequestStatus.APPROVED:
            return "آماده چاپ حواله"
        if self.status == RequestStatus.WITHDRAWN:
            return "انبار درخواست را لغو کرد"
        if self.status == RequestStatus.ISSUED:
            number = f"حواله {to_persian_digits(self.issue_number)}" if self.issue_number else "حواله صادر شد"
            return number + (" (در صف ارسال به سرور)" if self.waiting else "")
        if self.status == RequestStatus.REJECTED and self.rejection_reason:
            return f"دلیل رد: {self.rejection_reason}"
        return "ارسال شد"

    @property
    def status_name(self) -> str:
        name = STATUS_NAMES[self.status]
        if self.status == RequestStatus.ISSUED and self.waiting:
            name += " (در صف ارسال به سرور)"
        return name

    @property
    def items_text(self) -> str:
        return "، ".join(f"{format_qty(ln.qty)} {ln.unit} {ln.name}" for ln in self.lines)

    @property
    def editable(self) -> bool:
        """A rejected request of this PC can be corrected and sent again."""
        return self.status == RequestStatus.REJECTED and self.origin == "local" and not self.waiting


@dataclass(frozen=True)
class RequestDetail:
    row: RequestRow
    input: RequestInput


@dataclass(frozen=True)
class MessageRow:
    id: int
    mine: bool  # sent from the warehouse
    sender: str
    text: str
    created_at: object
    queued: bool


# ----- building a request -----


def clean_phone(phone: str) -> str:
    """What the server accepts: digits (Latin or Persian), spaces and + - ( ), at most 30."""
    return " ".join(_PHONE_CHARS.sub(" ", phone or "").split())[:30]


@dataclass
class _Snapshot:
    person: Person | None = None
    warehouse_id: int | None = None
    location_text: str = ""
    lines: list[GoodsRequestLine] = field(default_factory=list)


async def _collect(s: AsyncSession, data: RequestInput) -> tuple[_Snapshot, list[Problem]]:
    """Every problem at once (the dialog marks all of them), plus what to store."""
    snap, problems = _Snapshot(), []
    if data.person_id is None:
        problems.append(Problem("person", "درخواست‌کننده را از فهرست اشخاص انتخاب کنید."))
    else:
        snap.person = await s.get(Person, data.person_id)
        if snap.person is None or not snap.person.is_active:
            problems.append(Problem("person", "درخواست‌کننده نامعتبر یا غیرفعال است."))
    if data.location_id is None:
        problems.append(Problem("location", "محل مصرف را انتخاب کنید."))
    else:
        try:
            place = await locations.resolve_place_in(s, data.location_id, data.floor_id)
            snap.location_text = locations.place_label(place.location.name_fa, place.location.type,
                                                       place.floor.code, place.floor.name_fa)[:200]
        except ServiceError as exc:
            problems.append(Problem("floor" if exc.message == locations.FLOOR_REQUIRED else "location",
                                    exc.message))
    if data.warehouse_id is None:
        snap.warehouse_id = await s.scalar(select(Warehouse.id).where(Warehouse.is_active)
                                           .order_by(Warehouse.code).limit(1))
    else:
        wh = await s.get(Warehouse, data.warehouse_id)
        snap.warehouse_id = wh.id if wh and wh.is_active else None
    if snap.warehouse_id is None:
        problems.append(Problem("warehouse", "انبار نامعتبر یا غیرفعال است."))
    if not data.lines:
        problems.append(Problem("lines", "حداقل یک کالا به درخواست اضافه کنید."))
    for no, line in enumerate(data.lines, start=1):
        field_name = f"line:{no}"
        row = f"ردیف {to_persian_digits(no)}"
        item = await s.get(Item, line.item_id)
        if item is None or not item.is_active:
            problems.append(Problem(field_name, f"{row}: کالا نامعتبر یا غیرفعال است."))
            continue
        allowed = {item.base_unit_id, *(u.unit_id for u in item.units)}
        unit = await s.get(Unit, line.unit_id)
        if unit is None or line.unit_id not in allowed:
            problems.append(Problem(field_name,
                                    f"{row}: واحد انتخاب‌شده برای «{item.name}» تعریف نشده است."))
            continue
        if line.qty is None or not line.qty.is_finite() or line.qty <= 0:
            problems.append(Problem(field_name, f"{row}: مقدار «{item.name}» باید بزرگ‌تر از صفر باشد."))
            continue
        if not unit.allow_decimal and line.qty != line.qty.to_integral_value():
            problems.append(Problem(field_name,
                                    f"{row}: مقدار «{item.name}» به «{unit.name}» باید عدد صحیح باشد."))
            continue
        snap.lines.append(GoodsRequestLine(line_no=no, item_id=item.id, unit_id=unit.id, qty=line.qty,
                                           item_code=item.code, item_name=item.name, unit_name=unit.name))
    return snap, problems


async def check(db: Database, actor: Actor, data: RequestInput) -> list[Problem]:
    actor.require(Perm.REQUESTS_MANAGE)
    async with db.session() as s:
        _snap, problems = await _collect(s, data)
        await s.rollback()
    return problems


async def _apply(s: AsyncSession, req: GoodsRequest, data: RequestInput, snap: _Snapshot) -> None:
    req.person_id = snap.person.id
    req.requester_name = " ".join(snap.person.name.split())[:100]
    req.requester_phone = clean_phone(snap.person.phone)
    req.warehouse_id = snap.warehouse_id
    req.location_id, req.floor_id = data.location_id, data.floor_id
    req.location_text = snap.location_text
    req.note = data.note.strip()[:MAX_TEXT]
    if req.lines:
        req.lines.clear()
        await s.flush()  # the old rows go before new ones take their line numbers
    req.lines.extend(snap.lines)


async def _ready(s: AsyncSession, data: RequestInput) -> _Snapshot:
    snap, problems = await _collect(s, data)
    if problems:
        raise ValidationError(problems[0].message)
    return snap


async def create_request(db: Database, actor: Actor, data: RequestInput,
                         client_request_id: str | None = None) -> int:
    """Save a request and queue it for sending. The same `client_request_id` twice (a double click,
    a retry) returns the first request instead of making another. An AI actor only gets a DRAFT."""
    actor.require(Perm.REQUESTS_MANAGE)
    cid = str(uuid.UUID(client_request_id)) if client_request_id else str(uuid.uuid4())
    try:
        return await _create(db, actor, data, cid)
    except IntegrityError:  # the same id saved a moment earlier (two clicks at once)
        async with db.session() as s:
            existing = await s.scalar(select(GoodsRequest.id).where(GoodsRequest.client_request_id == cid))
        if existing is None:
            raise
        return existing


async def _create(db: Database, actor: Actor, data: RequestInput, cid: str) -> int:
    async with db.session(actor) as s:
        existing = await s.scalar(select(GoodsRequest.id).where(GoodsRequest.client_request_id == cid))
        if existing is not None:
            return existing
        snap = await _ready(s, data)
        status = RequestStatus.DRAFT if actor.is_ai else RequestStatus.QUEUED
        req = GoodsRequest(client_request_id=cid, status=status, origin="local", lines=[])
        await _apply(s, req, data, snap)
        s.add(req)
        await s.flush()
        if status == RequestStatus.QUEUED:
            s.add(ServerOutbox(request_id=req.id, op=OutboxOp.CREATE, user_id=actor.user_id))
        audit.record(s, actor, "goods_request.created", "goods_request", req.id,
                     {"lines": len(snap.lines), "status": status.value})
        return req.id


async def _load(s: AsyncSession, request_id: int) -> GoodsRequest:
    req = await s.get(GoodsRequest, request_id)
    if req is None:
        raise NotFound("درخواست کالا پیدا نشد.")
    return req


async def _waiting(s: AsyncSession, request_id: int) -> bool:
    return bool(await s.scalar(select(func.count()).select_from(ServerOutbox).where(
        ServerOutbox.request_id == request_id, ServerOutbox.op != OutboxOp.MESSAGE)))


async def resubmit_request(db: Database, actor: Actor, request_id: int, data: RequestInput) -> None:
    """Correct a rejected request (e.g. smaller quantities) and send it to the owners again."""
    actor.require(Perm.REQUESTS_MANAGE)
    actor.require_human("ارسال دوباره درخواست کالا")

    async def attempt() -> None:
        async with db.session(actor) as s:
            req = await _load(s, request_id)
            if req.status != RequestStatus.REJECTED or req.origin != "local" or await _waiting(s, req.id):
                raise ValidationError("فقط درخواست ردشده‌ای که در همین برنامه ثبت شده قابل ویرایش و "
                                      "ارسال دوباره است.")
            snap = await _ready(s, data)
            await _apply(s, req, data, snap)
            req.status = RequestStatus.QUEUED
            req.unseen = False
            req.last_error = ""
            s.add(ServerOutbox(request_id=req.id, op=OutboxOp.RESUBMIT, user_id=actor.user_id))
            audit.record(s, actor, "goods_request.resubmitted", "goods_request", req.id,
                         {"server_id": req.server_id, "lines": len(snap.lines)})

    await with_retries(attempt)


async def retry_failed(db: Database, actor: Actor, request_id: int) -> bool:
    """«ارسال مجدد»: what the server refused goes back to the queue unchanged (same
    `client_request_id`, so the server never makes a second request). False if nothing had failed."""
    actor.require(Perm.REQUESTS_MANAGE)
    actor.require_human("ارسال مجدد درخواست کالا")

    async def attempt() -> bool:
        async with db.session(actor) as s:
            await _load(s, request_id)
            done = await s.execute(update(ServerOutbox).where(
                ServerOutbox.request_id == request_id, ServerOutbox.failed).values(
                failed=False, last_error="", last_status=None))
            if not done.rowcount:
                return False
            await s.execute(update(GoodsRequest).where(GoodsRequest.id == request_id).values(last_error=""))
            audit.record(s, actor, "goods_request.retried", "goods_request", request_id)
            return True

    return await with_retries(attempt)


# ----- issue / loan-out slips: the slip itself is the request -----

WITHDRAWN_MESSAGE = "این درخواست توسط انبار لغو شد؛ لطفاً آن را تأیید نکنید."
NOT_SENDABLE = "فقط پیش‌نویس حواله خروج یا خروج امانی برای تأیید مالک ارسال می‌شود."
NOT_WITHDRAWABLE = "این سند درخواست در انتظار تأییدی ندارد."
_STATE_OPS = (OutboxOp.CREATE, OutboxOp.RESUBMIT)


async def _slip_snapshot(s: AsyncSession, doc: Document) -> tuple[Person | None, str, list[GoodsRequestLine]]:
    """What the owners see of the slip: recipient, place and lines (texts as on the slip)."""
    person = await s.get(Person, doc.person_id) if doc.person_id else None
    place = await documents.place_text_in(s, doc)
    if not place:  # the server needs a place; a loan has none
        who = f" — {person.name}" if person else ""
        place = f"{documents.DOC_TYPE_NAMES[doc.doc_type]}{who}"
    lines = []
    for ln in doc.lines:
        item, unit = await s.get(Item, ln.item_id), await s.get(Unit, ln.unit_id)
        lines.append(GoodsRequestLine(line_no=ln.line_no, item_id=item.id, unit_id=unit.id, qty=ln.qty,
                                      item_code=item.code, item_name=item.name, unit_name=unit.name))
    return person, place[:200], lines


def _slip_note(doc: Document) -> str:
    head = f"{documents.DOC_TYPE_NAMES[doc.doc_type]} {documents.number_text(doc.doc_type, doc.number)}"
    rest = " — ".join(t for t in (doc.usage_note.strip(), doc.description.strip()) if t)
    return (f"{head}: {rest}" if rest else head)[:MAX_TEXT]


async def _fill_from_slip(s: AsyncSession, req: GoodsRequest, doc: Document) -> None:
    person, place, lines = await _slip_snapshot(s, doc)
    req.person_id = person.id if person else None
    req.requester_name = " ".join(person.name.split())[:100] if person else ""
    req.requester_phone = clean_phone(person.phone) if person else ""
    req.warehouse_id = doc.warehouse_id
    req.location_id, req.floor_id = doc.location_id, doc.consumption_floor_id
    req.location_text = place
    req.note = _slip_note(doc)
    if req.lines:
        req.lines.clear()
        await s.flush()
    req.lines.extend(lines)


async def _reserve_in(s: AsyncSession, doc: Document) -> None:
    """Stock check again inside the sending transaction, with the balances of the slip's items
    locked: two PCs sending slips for the same goods at once can't both reserve the last ones."""
    item_ids = sorted({ln.item_id for ln in doc.lines})
    await s.execute(select(StockBalance.item_id).where(
        StockBalance.warehouse_id == doc.warehouse_id, StockBalance.item_id.in_(item_ids))
        .order_by(StockBalance.item_id).with_for_update())
    lines = list(doc.lines)
    if problems := await documents._posting_problems(s, documents._input_of(doc), lines, doc.id):
        raise ValidationError(problems[0].message)


async def submit_document(db: Database, actor: Actor, doc_id: int) -> int:
    """«ارسال درخواست»: send an issue / loan-out slip to the owners. It is saved in the outbox at
    once (sent now if online, later if not) and is posted only when an owner approves it — there is
    no local way round that. After a rejection the edited slip goes again as a resubmit of the same
    request. Returns the request id; pressing twice never sends twice."""
    actor.require_human("ارسال درخواست تأیید")
    actor.require(Perm.DOCUMENTS_EDIT)
    detail = await documents.get_document(db, actor, doc_id)
    if detail.input.doc_type not in documents.NEEDS_OWNER or detail.status != DocStatus.DRAFT:
        raise ValidationError(NOT_SENDABLE)
    if problems := await documents.check(db, actor, detail.input, doc_id):
        raise ValidationError(problems[0].message)

    async def attempt() -> int:
        async with db.session(actor) as s:
            doc = await s.scalar(select(Document).where(Document.id == doc_id).with_for_update())
            if (doc is None or doc.status != DocStatus.DRAFT or doc.doc_type not in documents.NEEDS_OWNER
                    or doc.server_request_id is not None):
                raise ValidationError(NOT_SENDABLE)
            if doc.version_id != detail.version_id:  # changed after the check above
                raise ValidationError("سند هم‌زمان تغییر کرد؛ دوباره «ارسال درخواست» را بزنید.")
            req = await documents.request_of_in(s, doc.id)
            if req is not None and req.status in documents.AWAITING_OWNER:
                return req.id  # already sent
            await _reserve_in(s, doc)
            if req is not None and req.status == RequestStatus.REJECTED and not await _waiting(s, req.id):
                await _fill_from_slip(s, req, doc)
                req.status, req.unseen, req.last_error = RequestStatus.QUEUED, False, ""
                s.add(ServerOutbox(request_id=req.id, op=OutboxOp.RESUBMIT, user_id=actor.user_id))
                action = "goods_request.resubmitted"
            else:
                if req is not None:  # withdrawn earlier: that request stays in the history
                    req.document_id = None
                    await s.flush()
                req = GoodsRequest(client_request_id=str(uuid.uuid4()), status=RequestStatus.QUEUED,
                                   origin="local", document_id=doc.id, lines=[])
                await _fill_from_slip(s, req, doc)
                s.add(req)
                await s.flush()
                s.add(ServerOutbox(request_id=req.id, op=OutboxOp.CREATE, user_id=actor.user_id))
                action = "goods_request.created"
            audit.record(s, actor, action, "goods_request", req.id,
                         {"document": doc.id, "type": doc.doc_type.value, "number": doc.number})
            return req.id

    return await with_retries(attempt)


async def withdraw_request(db: Database, actor: Actor, doc_id: int) -> None:
    """«لغو درخواست»: the sender takes back a slip that is waiting for the owners. The slip becomes an
    ordinary draft again. What the server has not seen yet is simply dropped; otherwise the owners get
    a message, and a later approval of this request is ignored (no stock moves)."""
    actor.require_human("لغو درخواست")
    actor.require(Perm.DOCUMENTS_EDIT)

    async def attempt() -> None:
        async with db.session(actor) as s:
            doc = await s.get(Document, doc_id)
            req = await documents.request_of_in(s, doc_id) if doc else None
            if req is None or doc.status != DocStatus.DRAFT or req.status not in documents.AWAITING_OWNER:
                raise ValidationError(NOT_WITHDRAWABLE)
            if doc.created_by_id != actor.user_id and not actor.is_admin:
                raise PermissionDenied("فقط ثبت‌کننده سند (یا مدیر سیستم) می‌تواند درخواست را لغو کند.")
            unsent = list((await s.scalars(select(ServerOutbox).where(
                ServerOutbox.request_id == req.id, ServerOutbox.op.in_(_STATE_OPS),
                ServerOutbox.attempts == 0))).all())
            for row in unsent:
                await s.delete(row)
            # A create or resubmit that never left this PC: the server has nothing waiting for owners.
            server_waiting = not (req.status == RequestStatus.QUEUED and unsent)
            req.status, req.document_id, req.last_error = RequestStatus.WITHDRAWN, None, ""
            if server_waiting:
                message = GoodsRequestMessage(request_id=req.id, sender_name=actor.display_name,
                                              sender_role="warehouse", text=WITHDRAWN_MESSAGE,
                                              user_id=actor.user_id)
                s.add(message)
                await s.flush()
                s.add(ServerOutbox(request_id=req.id, op=OutboxOp.MESSAGE, message_id=message.id,
                                   user_id=actor.user_id))
            audit.record(s, actor, "goods_request.withdrawn", "goods_request", req.id,
                         {"document": doc.id, "server_id": req.server_id})

    await with_retries(attempt)


# ----- assistant drafts -----


@dataclass(frozen=True)
class RequestConfirmation:
    """What the person confirms before a request the assistant prepared goes to the owners."""

    request_id: int
    title: str
    lines: list[str]
    details: list[tuple[str, str]]

    @property
    def text(self) -> str:
        rows = [self.title, *(f"• {line}" for line in self.lines)]
        rows += [f"{label}: {value}" for label, value in self.details if value]
        return "\n".join(rows)


async def confirmation(db: Database, actor: Actor, request_id: int) -> RequestConfirmation:
    actor.require(Perm.REQUESTS_MANAGE)
    async with db.session() as s:
        req = await _load(s, request_id)
        lines = [_line_view(ln).text for ln in req.lines]
        return RequestConfirmation(req.id, "درخواست کالا", lines, [
            ("درخواست‌کننده", req.requester_name), ("تلفن", to_persian_digits(req.requester_phone)),
            ("محل مصرف", req.location_text), ("توضیحات", req.note)])


async def _own_draft(s: AsyncSession, actor: Actor, request_id: int) -> GoodsRequest:
    req = await _load(s, request_id)
    if req.status != RequestStatus.DRAFT:
        raise ValidationError("این درخواست دیگر پیش‌نویس نیست.")
    if req.created_by_id != actor.user_id:
        raise PermissionDenied("این پیش‌نویس را کاربر دیگری ساخته است.")
    return req


async def confirm_assistant_request(db: Database, actor: Actor, request_id: int) -> None:
    """«بله، بفرست» on the card: the person sends the request the assistant prepared."""
    actor.require_human("ارسال درخواست کالا")
    actor.require(Perm.REQUESTS_MANAGE)
    async with db.session(actor) as s:
        req = await _own_draft(s, actor, request_id)
        req.status = RequestStatus.QUEUED
        s.add(ServerOutbox(request_id=req.id, op=OutboxOp.CREATE, user_id=actor.user_id))
        audit.record(s, actor, "goods_request.confirmed", "goods_request", req.id, {"via": "assistant"})


async def discard_assistant_request(db: Database, actor: Actor, request_id: int) -> None:
    """«نه» on the card: the draft goes away; nothing was sent."""
    actor.require_human("حذف پیش‌نویس درخواست")
    actor.require(Perm.REQUESTS_MANAGE)
    async with db.session(actor) as s:
        req = await _own_draft(s, actor, request_id)
        audit.record(s, actor, "goods_request.assistant_draft_discarded", "goods_request", req.id,
                     {"via": "assistant"})
        await s.delete(req)


# ----- reading -----


def _line_view(ln: GoodsRequestLine) -> RequestLineView:
    return RequestLineView(ln.item_id, ln.unit_id, ln.qty, ln.item_code, ln.item_name, ln.unit_name)


async def _rows(s: AsyncSession, requests: list[GoodsRequest], viewer: int | None = None) -> list[RequestRow]:
    server_ids = [r.server_id for r in requests if r.server_id is not None]
    docs = {sid: (doc_id, status) for doc_id, status, sid in (await s.execute(
        select(Document.id, Document.status, Document.server_request_id)
        .where(Document.server_request_id.in_(server_ids)))).all()} if server_ids else {}
    slip_ids = [r.document_id for r in requests if r.document_id is not None]
    slips = dict((await s.execute(select(Document.id, Document.status).where(
        Document.id.in_(slip_ids)))).all()) if slip_ids else {}
    waiting = set((await s.scalars(select(ServerOutbox.request_id).where(
        ServerOutbox.request_id.in_([r.id for r in requests]),
        ServerOutbox.op != OutboxOp.MESSAGE))).all()) if requests else set()
    failed = set((await s.scalars(select(ServerOutbox.request_id).where(
        ServerOutbox.request_id.in_([r.id for r in requests]),
        ServerOutbox.failed))).all()) if requests else set()
    author_ids = {r.created_by_id for r in requests if r.created_by_id}
    authors = {uid: (name or username, sid) for uid, name, username, sid in (await s.execute(
        select(User.id, User.full_name, User.username, User.server_user_id).where(
            User.id.in_(author_ids)))).all()} if author_ids else {}
    me = authors.get(viewer) if viewer else None
    if viewer and me is None:
        me = (None, await s.scalar(select(User.server_user_id).where(User.id == viewer)))
    rows = []
    for r in requests:
        doc_id, doc_status = ((r.document_id, slips.get(r.document_id)) if r.document_id is not None
                              else docs.get(r.server_id, (None, None)))
        author, _ = authors.get(r.created_by_id, ("", None))
        mine = bool(viewer) and (r.created_by_id == viewer if r.created_by_server_id is None
                                 else r.created_by_server_id == (me[1] if me else None))
        rows.append(RequestRow(
            r.id, r.server_id, r.status, r.origin, r.requester_name, r.requester_phone, r.location_text,
            r.note, tuple(_line_view(ln) for ln in r.lines), r.created_at, r.unseen, r.rejection_reason,
            r.issue_number, r.last_error, r.id in waiting, doc_id, doc_status, r.id in failed,
            r.created_by_name or (author if r.origin == "local" else ""), mine,
            r.document_id is not None))
    return rows


OPEN_STATUSES = (RequestStatus.QUEUED, RequestStatus.PENDING, RequestStatus.APPROVED, RequestStatus.REJECTED)


async def list_requests(db: Database, actor: Actor, status: RequestStatus | None = None,
                        limit: int = 300, mine: bool = False, open_only: bool = False) -> list[RequestRow]:
    """`mine`: only what the person looking registered («درخواست‌های من»). `open_only`: what still
    needs someone: waiting for the owners or rejected (an approved slip until it is posted)."""
    actor.require(Perm.REQUESTS_MANAGE)
    async with db.session() as s:
        stmt = select(GoodsRequest).order_by(GoodsRequest.id.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(GoodsRequest.status == status)
        if open_only:
            stmt = stmt.where(GoodsRequest.status.in_(OPEN_STATUSES))
        rows = await _rows(s, list((await s.scalars(stmt)).all()), actor.user_id)
    return [r for r in rows if r.mine] if mine else rows


async def get_request(db: Database, actor: Actor, request_id: int) -> RequestDetail:
    actor.require(Perm.REQUESTS_MANAGE)
    async with db.session() as s:
        req = await _load(s, request_id)
        [row] = await _rows(s, [req], actor.user_id)
        data = RequestInput(req.person_id, req.location_id, req.floor_id, req.note,
                            [RequestLineInput(ln.item_id, ln.unit_id, ln.qty) for ln in req.lines
                             if ln.item_id is not None and ln.unit_id is not None], req.warehouse_id)
        return RequestDetail(row, data)


async def mark_seen(db: Database, actor: Actor, request_id: int) -> None:
    """One UPDATE without reading the row first: the sync may change the same request at this moment
    (MariaDB 1020 «Record has changed since last read» was shown for the old read-then-write)."""
    actor.require(Perm.REQUESTS_MANAGE)

    async def attempt() -> None:
        async with db.session() as s:
            await s.execute(update(GoodsRequest).where(GoodsRequest.id == request_id, GoodsRequest.unseen)
                            .values(unseen=False).execution_options(synchronize_session=False))

    await with_retries(attempt)


async def dashboard_counts(db: Database) -> tuple[int, int]:
    """(sent and waiting for an owner, queued ones included; rejected and waiting to be fixed)."""
    async with db.session() as s:
        rows = dict((await s.execute(select(GoodsRequest.status, func.count()).where(
            GoodsRequest.origin == "local",
            GoodsRequest.status.in_((RequestStatus.QUEUED, RequestStatus.PENDING, RequestStatus.REJECTED)))
            .group_by(GoodsRequest.status))).all())
    return (rows.get(RequestStatus.QUEUED, 0) + rows.get(RequestStatus.PENDING, 0),
            rows.get(RequestStatus.REJECTED, 0))


async def unseen_count(db: Database) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(GoodsRequest).where(GoodsRequest.unseen)) or 0


# ----- chat -----


async def list_messages(db: Database, actor: Actor, request_id: int) -> list[MessageRow]:
    actor.require(Perm.REQUESTS_MANAGE)
    async with db.session() as s:
        await _load(s, request_id)
        rows = (await s.scalars(select(GoodsRequestMessage)
                                .where(GoodsRequestMessage.request_id == request_id)
                                .order_by(GoodsRequestMessage.created_at, GoodsRequestMessage.id))).all()
    return [MessageRow(m.id, m.sender_role == "warehouse",
                       OWNER_NAME if m.sender_role == "owner" else m.sender_name, m.text, m.created_at,
                       m.server_id is None) for m in rows]


async def queue_message(db: Database, actor: Actor, request_id: int, text: str) -> int:
    """A chat message to the owners; sent by the background sync like everything else."""
    actor.require(Perm.REQUESTS_MANAGE)
    actor.require_human("ارسال پیام به مالکان")
    text = text.strip()
    if not text:
        raise ValidationError("متن پیام خالی است.")
    if len(text) > MAX_TEXT:
        raise ValidationError(f"پیام حداکثر {to_persian_digits(MAX_TEXT)} حرف می‌تواند باشد.")
    async with db.session(actor) as s:
        req = await _load(s, request_id)
        if req.status == RequestStatus.DRAFT:
            raise ValidationError("ابتدا درخواست را ارسال کنید.")
        message = GoodsRequestMessage(request_id=req.id, sender_name=actor.display_name,
                                      sender_role="warehouse", text=text,
                                      user_id=actor.user_id)
        s.add(message)
        await s.flush()
        s.add(ServerOutbox(request_id=req.id, op=OutboxOp.MESSAGE, message_id=message.id,
                           user_id=actor.user_id))
        return message.id


# ----- what the server receives -----


def payload_of(req: GoodsRequest) -> dict:
    return {
        "client_request_id": req.client_request_id,
        "requester_name": req.requester_name,
        "requester_phone": req.requester_phone or None,
        "consumption_location": req.location_text,
        "note": req.note or None,
        "items": [{"item_code": (ln.item_code or "")[:50] or None, "name": ln.item_name[:200],
                   "quantity": float(ln.qty), "unit": ln.unit_name[:50]} for ln in req.lines],
    }
