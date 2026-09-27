"""Warehouse documents: draft -> posted (ledger + balances) -> cancelled (reversal).

Quantities are entered in any unit defined for the item and converted to the
item's base unit (`base_qty`) using the item's own conversion factors; the client
never supplies the factor.
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import flag_modified

from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import normalize, to_ascii_digits
from caspian.db.database import Database
from caspian.db.models import (
    AppSetting,
    DocStatus,
    DocType,
    Document,
    DocumentLine,
    Item,
    Person,
    StockBalance,
    StockLedger,
    Unit,
    User,
    Warehouse,
)
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import ConcurrencyError, NotFound, ValidationError

DOC_TYPE_NAMES = {
    DocType.OPENING: "موجودی اول دوره",
    DocType.RECEIPT: "رسید ورود",
    DocType.ISSUE: "حواله خروج",
    DocType.TRANSFER: "انتقال بین انبارها",
    DocType.ADJUSTMENT: "اصلاح موجودی",
    DocType.LOAN_OUT: "خروج امانی",
    DocType.LOAN_RETURN: "برگشت امانی",
}
STATUS_NAMES = {
    DocStatus.DRAFT: "پیش‌نویس",
    DocStatus.POSTED: "ثبت نهایی",
    DocStatus.CANCELLED: "ابطال شده",
}
# Sign of the effect on the source warehouse; ADJUSTMENT lines carry their own sign.
_SOURCE_SIGN = {
    DocType.OPENING: 1, DocType.RECEIPT: 1, DocType.ISSUE: -1, DocType.TRANSFER: -1,
    DocType.ADJUSTMENT: 1, DocType.LOAN_OUT: -1, DocType.LOAN_RETURN: 1,
}
NEEDS_PERSON = {DocType.LOAN_OUT, DocType.LOAN_RETURN}


@dataclass
class LineInput:
    item_id: int
    unit_id: int
    qty: Decimal
    unit_price: Decimal | None = None
    notes: str = ""


@dataclass
class DocumentInput:
    doc_type: DocType
    doc_date: dt.date
    warehouse_id: int
    lines: list[LineInput] = field(default_factory=list)
    dest_warehouse_id: int | None = None
    person_id: int | None = None
    related_document_id: int | None = None
    description: str = ""


@dataclass(frozen=True)
class DocumentRow:
    id: int
    doc_type: DocType
    number: int
    fiscal_year: int
    doc_date: dt.date
    status: DocStatus
    warehouse: str
    dest_warehouse: str
    person: str
    line_count: int
    created_by: str
    description: str

    @property
    def type_name(self) -> str:
        return DOC_TYPE_NAMES[self.doc_type]

    @property
    def status_name(self) -> str:
        return STATUS_NAMES[self.status]


@dataclass(frozen=True)
class LineView:
    item_id: int
    item_code: str
    item_name: str
    unit_id: int
    unit_name: str
    qty: Decimal
    base_qty: Decimal
    unit_price: Decimal | None
    notes: str


@dataclass(frozen=True)
class DocumentDetail:
    id: int
    version_id: int
    number: int
    fiscal_year: int
    status: DocStatus
    input: DocumentInput
    lines: list[LineView]


@dataclass(frozen=True)
class LoanRow:
    document_id: int
    number: int
    doc_date: dt.date
    person_id: int
    person: str
    item_id: int
    item_code: str
    item_name: str
    base_unit: str
    loaned: Decimal
    returned: Decimal

    @property
    def outstanding(self) -> Decimal:
        return self.loaned - self.returned

    @property
    def days_out(self) -> int:
        return (dt.date.today() - self.doc_date).days


# ----- helpers -----


async def _allow_negative(s: AsyncSession) -> bool:
    setting = await s.get(AppSetting, "allow_negative_stock")
    return bool(setting and setting.value)


async def _load(s: AsyncSession, doc_id: int) -> Document:
    doc = await s.get(Document, doc_id)
    if doc is None:
        raise NotFound("سند پیدا نشد.")
    return doc


def _item_factors(item: Item) -> dict[int, Decimal]:
    factors = {item.base_unit_id: Decimal(1)}
    factors.update({u.unit_id: u.factor for u in item.units})
    return factors


async def _validate(s: AsyncSession, data: DocumentInput) -> list[DocumentLine]:
    """Validate header + lines; returns ready DocumentLine objects (unattached)."""
    wh = await s.get(Warehouse, data.warehouse_id)
    if wh is None or not wh.is_active:
        raise ValidationError("انبار انتخاب‌شده نامعتبر یا غیرفعال است.")
    if data.doc_type == DocType.TRANSFER:
        dest = await s.get(Warehouse, data.dest_warehouse_id) if data.dest_warehouse_id else None
        if dest is None or not dest.is_active:
            raise ValidationError("انبار مقصد را انتخاب کنید.")
        if dest.id == wh.id:
            raise ValidationError("انبار مبدأ و مقصد نمی‌توانند یکسان باشند.")
    elif data.dest_warehouse_id is not None:
        raise ValidationError("انبار مقصد فقط برای سند انتقال معنا دارد.")
    if data.person_id is not None:
        person = await s.get(Person, data.person_id)
        if person is None or not person.is_active:
            raise ValidationError("طرف حساب نامعتبر یا غیرفعال است.")
    if data.doc_type in NEEDS_PERSON and data.person_id is None:
        raise ValidationError("برای سند امانی، تحویل‌گیرنده (شخص) الزامی است.")
    if data.doc_type == DocType.LOAN_RETURN:
        loan = await s.get(Document, data.related_document_id) if data.related_document_id \
            else None
        if loan is None or loan.doc_type != DocType.LOAN_OUT or loan.status != DocStatus.POSTED:
            raise ValidationError("سند خروج امانی مرتبط را انتخاب کنید.")
        if loan.person_id != data.person_id:
            raise ValidationError("شخص برگشت‌دهنده با تحویل‌گیرنده امانی یکسان نیست.")
    elif data.related_document_id is not None:
        raise ValidationError("سند مرتبط فقط برای برگشت امانی است.")
    if not data.lines:
        raise ValidationError("سند باید حداقل یک ردیف کالا داشته باشد.")

    lines: list[DocumentLine] = []
    for no, line in enumerate(data.lines, start=1):
        item = await s.get(Item, line.item_id)
        if item is None:
            raise ValidationError(f"ردیف {no}: کالا پیدا نشد.")
        if not item.is_active:
            raise ValidationError(f"ردیف {no}: کالای «{item.name}» غیرفعال است.")
        if data.doc_type in (DocType.LOAN_OUT, DocType.LOAN_RETURN) and not item.is_returnable:
            raise ValidationError(f"ردیف {no}: کالای «{item.name}» امانی/برگشتی تعریف نشده است.")
        factors = _item_factors(item)
        if line.unit_id not in factors:
            raise ValidationError(f"ردیف {no}: واحد انتخاب‌شده برای «{item.name}» تعریف نشده است.")
        qty = line.qty
        if qty is None or qty == 0 or (qty < 0 and data.doc_type != DocType.ADJUSTMENT):
            raise ValidationError(f"ردیف {no}: مقدار «{item.name}» باید بزرگ‌تر از صفر باشد.")
        if line.unit_price is not None and line.unit_price < 0:
            raise ValidationError(f"ردیف {no}: فی نمی‌تواند منفی باشد.")
        factor = factors[line.unit_id]
        lines.append(DocumentLine(
            line_no=no, item_id=item.id, unit_id=line.unit_id, qty=qty, factor=factor,
            base_qty=qty * factor, unit_price=line.unit_price, notes=line.notes.strip(),
        ))
    return lines


def _effects(doc: Document) -> list[tuple[DocumentLine, int, Decimal]]:
    """(line, warehouse_id, signed base qty) for every ledger row a posting creates."""
    sign = _SOURCE_SIGN[doc.doc_type]
    out = []
    for line in doc.lines:
        out.append((line, doc.warehouse_id, sign * line.base_qty))
        if doc.doc_type == DocType.TRANSFER:
            out.append((line, doc.dest_warehouse_id, line.base_qty))
    return out


async def _apply_effects(s: AsyncSession, doc: Document, reverse: bool) -> None:
    """Write ledger rows and update balances; refuse to go negative unless allowed."""
    totals: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for line, wh_id, qty in _effects(doc):
        qty = -qty if reverse else qty
        totals[(line.item_id, wh_id)] += qty
        s.add(StockLedger(document_id=doc.id, line_id=line.id, item_id=line.item_id,
                          warehouse_id=wh_id, doc_date=doc.doc_date, qty_change=qty))
    allow_negative = await _allow_negative(s)
    for (item_id, wh_id), change in sorted(totals.items()):
        balance = await s.scalar(
            select(StockBalance).where(StockBalance.item_id == item_id,
                                       StockBalance.warehouse_id == wh_id).with_for_update()
        )
        if balance is None:
            balance = StockBalance(item_id=item_id, warehouse_id=wh_id, qty=Decimal(0))
            s.add(balance)
        new_qty = balance.qty + change
        if new_qty < 0 and change < 0 and not allow_negative:
            item = await s.get(Item, item_id)
            wh = await s.get(Warehouse, wh_id)
            raise ValidationError(
                f"موجودی «{item.name}» در «{wh.name}» کافی نیست "
                f"(موجود: {format_qty(balance.qty)}، نیاز: {format_qty(-change)})."
            )
        balance.qty = new_qty


async def _next_number(s: AsyncSession, doc_type: DocType, fiscal_year: int) -> int:
    current = await s.scalar(
        select(func.max(Document.number))
        .where(Document.doc_type == doc_type, Document.fiscal_year == fiscal_year)
        .with_for_update()
    )
    return (current or 0) + 1


def _set_header(doc: Document, data: DocumentInput) -> None:
    doc.doc_date = data.doc_date
    doc.warehouse_id = data.warehouse_id
    doc.dest_warehouse_id = data.dest_warehouse_id
    doc.person_id = data.person_id
    doc.related_document_id = data.related_document_id
    doc.description = data.description.strip()


async def _check_loan_return_limits(s: AsyncSession, doc: Document) -> None:
    """A return can't bring back more than is still outstanding on its loan."""
    outstanding = {
        r.item_id: r.outstanding
        for r in await _loan_rows(s, loan_id=doc.related_document_id, exclude_doc=doc.id)
    }
    returning: dict[int, Decimal] = defaultdict(Decimal)
    for line in doc.lines:
        returning[line.item_id] += line.base_qty
    for item_id, qty in returning.items():
        if qty > outstanding.get(item_id, Decimal(0)):
            item = await s.get(Item, item_id)
            raise ValidationError(f"مقدار برگشتی «{item.name}» بیشتر از مانده امانی است.")


# ----- queries -----


async def list_documents(
    db: Database, actor: Actor, doc_type: DocType | None = None, status: DocStatus | None = None,
    query: str = "", date_from: dt.date | None = None, date_to: dt.date | None = None,
    limit: int = 500,
) -> list[DocumentRow]:
    actor.require(Perm.DOCUMENTS_VIEW)
    dest = aliased(Warehouse)
    lines = (select(DocumentLine.document_id, func.count().label("n"))
             .group_by(DocumentLine.document_id).subquery())
    stmt = (
        select(Document, Warehouse.name, dest.name, Person.name, func.coalesce(lines.c.n, 0),
               User.username)
        .join(Warehouse, Document.warehouse_id == Warehouse.id)
        .outerjoin(dest, Document.dest_warehouse_id == dest.id)
        .outerjoin(Person, Document.person_id == Person.id)
        .outerjoin(lines, lines.c.document_id == Document.id)
        .outerjoin(User, Document.created_by_id == User.id)
        .order_by(Document.doc_date.desc(), Document.id.desc())
        .limit(limit)
    )
    if doc_type is not None:
        stmt = stmt.where(Document.doc_type == doc_type)
    if status is not None:
        stmt = stmt.where(Document.status == status)
    if date_from is not None:
        stmt = stmt.where(Document.doc_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Document.doc_date <= date_to)
    raw = to_ascii_digits(query.strip())
    if raw:
        conds = [Person.name_normalized.contains(normalize(query), autoescape=True),
                 Document.description.contains(query.strip(), autoescape=True)]
        if raw.isdigit():
            conds.append(Document.number == int(raw))
        stmt = stmt.where(or_(*conds))
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    return [
        DocumentRow(d.id, d.doc_type, d.number, d.fiscal_year, d.doc_date, d.status, wh,
                    dwh or "", person or "", n, creator or "", d.description)
        for d, wh, dwh, person, n, creator in rows
    ]


async def get_document(db: Database, actor: Actor, doc_id: int) -> DocumentDetail:
    actor.require(Perm.DOCUMENTS_VIEW)
    async with db.session() as s:
        doc = await _load(s, doc_id)
        views = []
        for line in doc.lines:
            item = await s.get(Item, line.item_id)
            unit = await s.get(Unit, line.unit_id)
            views.append(LineView(line.item_id, item.code, item.name, line.unit_id, unit.name,
                                  line.qty, line.base_qty, line.unit_price, line.notes))
        data = DocumentInput(
            doc_type=doc.doc_type, doc_date=doc.doc_date, warehouse_id=doc.warehouse_id,
            dest_warehouse_id=doc.dest_warehouse_id, person_id=doc.person_id,
            related_document_id=doc.related_document_id, description=doc.description,
            lines=[LineInput(v.item_id, v.unit_id, v.qty, v.unit_price, v.notes) for v in views],
        )
        return DocumentDetail(doc.id, doc.version_id, doc.number, doc.fiscal_year, doc.status,
                              data, views)


async def stock_by_warehouse(db: Database, item_id: int) -> list[tuple[str, Decimal]]:
    async with db.session() as s:
        rows = await s.execute(
            select(Warehouse.name, StockBalance.qty)
            .join(Warehouse, StockBalance.warehouse_id == Warehouse.id)
            .where(StockBalance.item_id == item_id).order_by(Warehouse.code)
        )
        return [(name, qty) for name, qty in rows.all()]


async def _loan_rows(s: AsyncSession, person_id: int | None = None, loan_id: int | None = None,
                     exclude_doc: int | None = None) -> list[LoanRow]:
    loan_filter = [Document.doc_type == DocType.LOAN_OUT, Document.status == DocStatus.POSTED]
    if person_id is not None:
        loan_filter.append(Document.person_id == person_id)
    if loan_id is not None:
        loan_filter.append(Document.id == loan_id)
    loaned = (await s.execute(
        select(Document.id, Document.number, Document.doc_date, Document.person_id, Person.name,
               DocumentLine.item_id, func.sum(DocumentLine.base_qty))
        .join(DocumentLine, DocumentLine.document_id == Document.id)
        .join(Person, Person.id == Document.person_id)
        .where(*loan_filter)
        .group_by(Document.id, Document.number, Document.doc_date, Document.person_id,
                  Person.name, DocumentLine.item_id)
        .order_by(Document.doc_date, Document.id)
    )).all()
    returned_stmt = (
        select(Document.related_document_id, DocumentLine.item_id, func.sum(DocumentLine.base_qty))
        .join(DocumentLine, DocumentLine.document_id == Document.id)
        .where(Document.doc_type == DocType.LOAN_RETURN, Document.status == DocStatus.POSTED)
        .group_by(Document.related_document_id, DocumentLine.item_id)
    )
    if exclude_doc is not None:
        returned_stmt = returned_stmt.where(Document.id != exclude_doc)
    returned = {(d, i): q for d, i, q in (await s.execute(returned_stmt)).all()}
    rows = []
    for doc_id, number, date, pid, pname, item_id, qty in loaned:
        item = await s.get(Item, item_id)
        rows.append(LoanRow(doc_id, number, date, pid, pname, item_id, item.code, item.name,
                            item.base_unit.name, qty, returned.get((doc_id, item_id), Decimal(0))))
    return rows


async def outstanding_loans(db: Database, actor: Actor, person_id: int | None = None,
                            loan_id: int | None = None) -> list[LoanRow]:
    actor.require(Perm.DOCUMENTS_VIEW)
    async with db.session() as s:
        return [r for r in await _loan_rows(s, person_id, loan_id) if r.outstanding > 0]


async def pending_counts(db: Database) -> tuple[int, int]:
    """(draft documents awaiting posting, open loan lines)."""
    async with db.session() as s:
        drafts = await s.scalar(select(func.count()).select_from(Document)
                                .where(Document.status == DocStatus.DRAFT))
        loans = [r for r in await _loan_rows(s) if r.outstanding > 0]
    return drafts or 0, len(loans)


# ----- commands -----


async def create_document_in(s: AsyncSession, actor: Actor, data: DocumentInput) -> Document:
    """Create a draft inside the caller's transaction (used by imports)."""
    actor.require(Perm.DOCUMENTS_EDIT)
    lines = await _validate(s, data)
    year = jalali.fiscal_year_of(data.doc_date)
    doc = Document(doc_type=data.doc_type, fiscal_year=year,
                   number=await _next_number(s, data.doc_type, year),
                   status=DocStatus.DRAFT, doc_date=data.doc_date,
                   warehouse_id=data.warehouse_id)
    _set_header(doc, data)
    doc.lines = lines
    s.add(doc)
    await s.flush()
    audit.record(s, actor, "document.created", "document", doc.id,
                 {"type": doc.doc_type.value, "number": doc.number, "lines": len(lines)})
    return doc


async def create_document(db: Database, actor: Actor, data: DocumentInput) -> int:
    async with db.session(actor.user_id) as s:
        return (await create_document_in(s, actor, data)).id


async def update_document(db: Database, actor: Actor, doc_id: int, expected_version: int,
                          data: DocumentInput) -> None:
    actor.require(Perm.DOCUMENTS_EDIT)
    async with db.session(actor.user_id) as s:
        doc = await _load(s, doc_id)
        if doc.version_id != expected_version:
            raise ConcurrencyError()
        if doc.status != DocStatus.DRAFT:
            raise ValidationError("فقط سند پیش‌نویس قابل ویرایش است.")
        if data.doc_type != doc.doc_type:
            raise ValidationError("نوع سند قابل تغییر نیست.")
        if jalali.fiscal_year_of(data.doc_date) != doc.fiscal_year:
            raise ValidationError("تاریخ سند باید در همان سال مالی بماند.")
        lines = await _validate(s, data)
        _set_header(doc, data)
        doc.lines.clear()
        await s.flush()
        doc.lines.extend(lines)
        flag_modified(doc, "description")  # line-only edits must still bump the version
        await s.flush()
        audit.record(s, actor, "document.updated", "document", doc.id, {"lines": len(lines)})


async def delete_draft(db: Database, actor: Actor, doc_id: int) -> None:
    actor.require(Perm.DOCUMENTS_EDIT)
    async with db.session(actor.user_id) as s:
        doc = await _load(s, doc_id)
        if doc.status != DocStatus.DRAFT:
            raise ValidationError("فقط سند پیش‌نویس قابل حذف است. سند ثبت‌شده را ابطال کنید.")
        audit.record(s, actor, "document.draft_deleted", "document", doc.id,
                     {"type": doc.doc_type.value, "number": doc.number})
        await s.delete(doc)


async def post_document(db: Database, actor: Actor, doc_id: int,
                        expected_version: int | None = None) -> None:
    """Finalize a draft: write the ledger and update balances atomically."""
    actor.require(Perm.DOCUMENTS_POST)
    async with db.session(actor.user_id) as s:
        doc = await _load(s, doc_id)
        if expected_version is not None and doc.version_id != expected_version:
            raise ConcurrencyError()
        if doc.status != DocStatus.DRAFT:
            raise ValidationError("این سند قبلاً ثبت یا ابطال شده است.")
        # Re-validate: items/warehouses may have been deactivated since the draft was saved.
        current = _input_of(doc)
        await _validate(s, current)
        if doc.doc_type == DocType.LOAN_RETURN:
            await _check_loan_return_limits(s, doc)
        await _apply_effects(s, doc, reverse=False)
        doc.status = DocStatus.POSTED
        doc.posted_at = dt.datetime.now()
        doc.posted_by_id = actor.user_id
        audit.record(s, actor, "document.posted", "document", doc.id,
                     {"type": doc.doc_type.value, "number": doc.number})


def _input_of(doc: Document) -> DocumentInput:
    return DocumentInput(
        doc_type=doc.doc_type, doc_date=doc.doc_date, warehouse_id=doc.warehouse_id,
        dest_warehouse_id=doc.dest_warehouse_id, person_id=doc.person_id,
        related_document_id=doc.related_document_id, description=doc.description,
        lines=[LineInput(ln.item_id, ln.unit_id, ln.qty, ln.unit_price, ln.notes)
               for ln in doc.lines],
    )


async def cancel_document(db: Database, actor: Actor, doc_id: int, reason: str = "") -> None:
    """Void a posted document with reversal ledger rows (history is never deleted)."""
    actor.require(Perm.DOCUMENTS_POST)
    async with db.session(actor.user_id) as s:
        doc = await _load(s, doc_id)
        if doc.status != DocStatus.POSTED:
            raise ValidationError("فقط سند ثبت‌شده قابل ابطال است.")
        if doc.doc_type == DocType.LOAN_OUT:
            has_returns = await s.scalar(select(Document.id).where(
                Document.related_document_id == doc.id, Document.status == DocStatus.POSTED))
            if has_returns:
                raise ValidationError(
                    "برای این امانی، برگشت ثبت شده است. ابتدا برگشت‌ها را ابطال کنید."
                )
        await _apply_effects(s, doc, reverse=True)
        doc.status = DocStatus.CANCELLED
        audit.record(s, actor, "document.cancelled", "document", doc.id,
                     {"type": doc.doc_type.value, "number": doc.number, "reason": reason})


async def create_and_post(db: Database, actor: Actor, data: DocumentInput) -> int:
    actor.require(Perm.DOCUMENTS_POST)
    """Save then post. If posting fails the document stays as a draft (not lost)."""
    doc_id = await create_document(db, actor, data)
    await post_document(db, actor, doc_id)
    return doc_id
