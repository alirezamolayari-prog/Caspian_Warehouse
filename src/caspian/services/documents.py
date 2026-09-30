"""Warehouse documents: draft -> posted (ledger + balances) -> cancelled (reversal).

Quantities are entered in any unit defined for the item and converted to the
item's base unit (`base_qty`) using the item's own conversion factors; the client
never supplies the factor.
"""

import datetime as dt
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import flag_modified

from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import normalize, to_ascii_digits, to_persian_digits
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
    Stocktake,
    StocktakeLine,
    StocktakeStatus,
    Unit,
    User,
    Warehouse,
)
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import ConcurrencyError, NotFound, StocktakeFrozen, ValidationError
from caspian.services.fiscal_state import ensure_open_year
from caspian.services.protected import Approval, ProtectedAction, consume

NUMBER_RETRIES = 3
log = logging.getLogger(__name__)

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
# Shown before the number so two «سند ۱» of different types can't be confused (ر-۱، ح-۱ …).
DOC_PREFIX = {
    DocType.RECEIPT: "ر", DocType.ISSUE: "ح", DocType.TRANSFER: "ت", DocType.ADJUSTMENT: "ص",
    DocType.OPENING: "م", DocType.LOAN_OUT: "ا", DocType.LOAN_RETURN: "ب",
}
PERSON_LABELS = {
    DocType.RECEIPT: "تأمین‌کننده", DocType.ISSUE: "تحویل‌گیرنده", DocType.LOAN_OUT: "تحویل‌گیرنده",
    DocType.LOAN_RETURN: "برگشت‌دهنده",
}


_PREFIXED = re.compile(r"^([رحتصمآاب])\s*-?\s*(\d+)$")


def number_text(doc_type: DocType, number: int) -> str:
    return f"{DOC_PREFIX[doc_type]}-{to_persian_digits(number)}"


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
    print_count: int = 0

    @property
    def number_text(self) -> str:
        return number_text(self.doc_type, self.number)

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
    if data.doc_date > dt.date.today():
        raise ValidationError("تاریخ سند نمی‌تواند بعد از امروز باشد. تاریخ را بررسی کنید.")
    await ensure_open_year(s, jalali.fiscal_year_of(data.doc_date))
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
        unit = await s.get(Unit, line.unit_id)
        if unit is not None and not unit.allow_decimal and qty != qty.to_integral_value():
            raise ValidationError(f"ردیف {no}: مقدار «{item.name}» به «{unit.name}» باید عدد صحیح باشد.")
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
            hint = pending_hint(await _pending_incoming_in(s, item_id, wh_id, doc.id),
                                item.base_unit.name)
            raise ValidationError(
                f"موجودی «{item.name}» در «{wh.name}» کافی نیست "
                f"(موجود: {format_qty(balance.qty)}، نیاز: {format_qty(-change)})."
                + (f" {hint}" if hint else "")
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


async def _stocktake_override(s: AsyncSession, actor: Actor, doc: Document, approval: Approval | None,
                              exempt_stocktake_id: int | None = None) -> dict:
    """Stock of items being counted must not move until the stocktake is approved or cancelled,
    otherwise the adjustment is computed against a stale snapshot (#7). An admin may override
    with a PIN. Returns audit details for an override (empty if none was needed)."""
    warehouses = [doc.warehouse_id] + ([doc.dest_warehouse_id] if doc.dest_warehouse_id else [])
    stmt = (select(Stocktake.number, Warehouse.name, Item.name)
            .join(StocktakeLine, StocktakeLine.stocktake_id == Stocktake.id)
            .join(Warehouse, Warehouse.id == Stocktake.warehouse_id)
            .join(Item, Item.id == StocktakeLine.item_id)
            .where(Stocktake.status.in_([StocktakeStatus.OPEN, StocktakeStatus.COUNTED]),
                   Stocktake.warehouse_id.in_(warehouses),
                   StocktakeLine.item_id.in_({ln.item_id for ln in doc.lines}))
            .order_by(Stocktake.number).limit(1))
    if exempt_stocktake_id is not None:
        stmt = stmt.where(Stocktake.id != exempt_stocktake_id)
    hit = (await s.execute(stmt)).first()
    if hit is None:
        return {}
    number, warehouse, item = hit
    if approval is None:
        raise StocktakeFrozen(
            f"انبارگردانی شماره {to_persian_digits(number)} در «{warehouse}» در جریان است و موجودی "
            f"«{item}» تا تأیید یا لغو آن قفل است. تغییر آن فقط با تأیید مدیر (PIN) ممکن است.")
    return {"stocktake_override": number,
            "approved_by_id": consume(approval, ProtectedAction.STOCKTAKE_OVERRIDE, actor)}


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
    if prefixed := _PREFIXED.match(raw):
        doc_type = next((t for t, p in DOC_PREFIX.items() if p == prefixed[1]), None)
        stmt = stmt.where(Document.doc_type == doc_type, Document.number == int(prefixed[2]))
    elif raw:
        conds = [Person.name_normalized.contains(normalize(query), autoescape=True),
                 Document.description.contains(query.strip(), autoescape=True)]
        if raw.isdigit():
            conds.append(Document.number == int(raw))
        stmt = stmt.where(or_(*conds))
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    return [
        DocumentRow(d.id, d.doc_type, d.number, d.fiscal_year, d.doc_date, d.status, wh,
                    dwh or "", person or "", n, creator or "", d.description, d.print_count)
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


@dataclass(frozen=True)
class PrintLine:
    code: str
    name: str
    unit: str
    qty: Decimal
    unit_price: Decimal | None
    notes: str

    @property
    def amount(self) -> Decimal | None:
        return None if self.unit_price is None else self.qty * self.unit_price


@dataclass(frozen=True)
class PrintSheet:
    """Everything the printed receipt/issue form shows."""

    id: int
    doc_type: DocType
    number_text: str
    type_name: str
    status: DocStatus
    doc_date: dt.date
    warehouse: str
    dest_warehouse: str
    person: str
    person_label: str
    description: str
    lines: list[PrintLine]
    company: str
    created_by: str
    posted_by: str
    print_count: int  # copies issued so far
    last_printed_by: str

    @property
    def total_amount(self) -> Decimal | None:
        amounts = [ln.amount for ln in self.lines if ln.amount is not None]
        return sum(amounts, Decimal(0)) if amounts else None

    @property
    def trackable(self) -> bool:
        """Drafts can be printed for checking, but only final documents count as issued copies."""
        return self.status != DocStatus.DRAFT


async def print_sheet(db: Database, actor: Actor, doc_id: int) -> PrintSheet:
    actor.require(Perm.DOCUMENTS_VIEW)
    async with db.session() as s:
        doc = await _load(s, doc_id)

        async def name_of(model, pk, attr="name") -> str:
            row = await s.get(model, pk) if pk else None
            return getattr(row, attr) if row else ""

        lines = []
        for ln in doc.lines:
            item = await s.get(Item, ln.item_id)
            lines.append(PrintLine(item.code, item.name, await name_of(Unit, ln.unit_id), ln.qty,
                                   ln.unit_price, ln.notes))
        company = await s.get(AppSetting, "company_name")
        return PrintSheet(
            doc.id, doc.doc_type, number_text(doc.doc_type, doc.number), DOC_TYPE_NAMES[doc.doc_type],
            doc.status, doc.doc_date, await name_of(Warehouse, doc.warehouse_id),
            await name_of(Warehouse, doc.dest_warehouse_id), await name_of(Person, doc.person_id),
            PERSON_LABELS.get(doc.doc_type, "طرف حساب"), doc.description, lines,
            str(company.value) if company and company.value else "",
            await name_of(User, doc.created_by_id, "username"),
            await name_of(User, doc.posted_by_id, "username"), doc.print_count,
            await name_of(User, doc.last_printed_by_id, "username"))


async def record_print(db: Database, actor: Actor, doc_id: int, kind: str) -> int:
    """Count one issued copy (kind: "printer" or "pdf"); returns its copy number."""
    actor.require(Perm.DOCUMENTS_VIEW)
    async with db.session(actor.user_id) as s:
        doc = await _load(s, doc_id)
        if doc.status == DocStatus.DRAFT:
            raise ValidationError("چاپ پیش‌نویس ثبت نمی‌شود؛ ابتدا سند را ثبت نهایی کنید.")
        doc.print_count += 1
        doc.last_printed_at = dt.datetime.now()
        doc.last_printed_by_id = actor.user_id
        audit.record(s, actor, "document.printed", "document", doc.id,
                     {"type": doc.doc_type.value, "number": doc.number, "copy": doc.print_count,
                      "kind": kind})
        return doc.print_count


@dataclass(frozen=True)
class PendingIn:
    """Stock that will arrive in a warehouse once a draft is posted."""

    document_id: int
    number_text: str
    base_qty: Decimal


async def _pending_incoming_in(s: AsyncSession, item_id: int, warehouse_id: int,
                               exclude_doc: int | None = None) -> list[PendingIn]:
    into = or_(
        and_(Document.doc_type.in_([DocType.RECEIPT, DocType.OPENING, DocType.LOAN_RETURN,
                                    DocType.ADJUSTMENT]), Document.warehouse_id == warehouse_id),
        and_(Document.doc_type == DocType.TRANSFER, Document.dest_warehouse_id == warehouse_id))
    stmt = (select(Document.id, Document.doc_type, Document.number, func.sum(DocumentLine.base_qty))
            .join(DocumentLine, DocumentLine.document_id == Document.id)
            .where(Document.status == DocStatus.DRAFT, DocumentLine.item_id == item_id, into)
            .group_by(Document.id, Document.doc_type, Document.number).order_by(Document.id))
    if exclude_doc is not None:
        stmt = stmt.where(Document.id != exclude_doc)
    return [PendingIn(doc_id, number_text(t, n), qty)
            for doc_id, t, n, qty in (await s.execute(stmt)).all() if qty > 0]


async def pending_incoming(db: Database, actor: Actor, item_id: int, warehouse_id: int) -> list[PendingIn]:
    """Drafts that would add this item to this warehouse (e.g. an import not posted yet, #9)."""
    actor.require(Perm.DOCUMENTS_VIEW)
    async with db.session() as s:
        return await _pending_incoming_in(s, item_id, warehouse_id)


def pending_hint(pending: list[PendingIn], unit_name: str) -> str:
    if not pending:
        return ""
    total = sum((p.base_qty for p in pending), Decimal(0))
    numbers = "، ".join(p.number_text for p in pending)
    return f"{format_qty(total)} {unit_name} در پیش‌نویس {numbers} منتظر ثبت نهایی است."


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


@dataclass(frozen=True)
class PendingSummary:
    """What the dashboard's «پیش‌نویس‌های در انتظار» card shows: value and hint from one query."""

    draft_documents: int
    open_imports: int

    @property
    def total(self) -> int:
        return self.draft_documents + self.open_imports

    @property
    def hint(self) -> str:
        return (f"{to_persian_digits(self.draft_documents)} سند، "
                f"{to_persian_digits(self.open_imports)} ورود اطلاعات")


async def pending_summary(db: Database) -> PendingSummary:
    from caspian.db.models import BatchStatus, ImportBatch

    async with db.session() as s:
        drafts = await s.scalar(select(func.count()).select_from(Document)
                                .where(Document.status == DocStatus.DRAFT))
        batches = await s.scalar(select(func.count()).select_from(ImportBatch)
                                 .where(ImportBatch.status == BatchStatus.OPEN))
    return PendingSummary(drafts or 0, batches or 0)


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
    # Two PCs saving the same document type at the same moment can pick the same number;
    # the unique constraint rejects the second one, which simply retries with the next number.
    for attempt in range(NUMBER_RETRIES):
        try:
            async with db.session(actor.user_id) as s:
                return (await create_document_in(s, actor, data)).id
        except IntegrityError:
            if attempt == NUMBER_RETRIES - 1:
                raise
            log.info("Document number collision; retrying")
    raise AssertionError("unreachable")


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
        await ensure_open_year(s, doc.fiscal_year)
        from caspian.services import imports  # imports depends on this module

        await imports.reopen_for_deleted_document(s, actor, doc.id)
        audit.record(s, actor, "document.draft_deleted", "document", doc.id,
                     {"type": doc.doc_type.value, "number": doc.number})
        await s.delete(doc)


async def post_document(db: Database, actor: Actor, doc_id: int,
                        expected_version: int | None = None, approval: Approval | None = None) -> None:
    """Finalize a draft: write the ledger and update balances atomically.

    `approval` (STOCKTAKE_OVERRIDE) is only needed while a stocktake freezes the items."""
    async with db.session(actor.user_id) as s:
        doc = await _load(s, doc_id)
        if expected_version is not None and doc.version_id != expected_version:
            raise ConcurrencyError()
        await post_document_in(s, actor, doc, approval=approval)


async def post_document_in(s: AsyncSession, actor: Actor, doc: Document, *,
                           approval: Approval | None = None, stocktake_id: int | None = None) -> None:
    """Post inside the caller's transaction. `stocktake_id`: the stocktake whose own adjustment
    this is (exempt from its freeze)."""
    actor.require(Perm.DOCUMENTS_POST)
    if doc.status != DocStatus.DRAFT:
        raise ValidationError("این سند قبلاً ثبت یا ابطال شده است.")
    # Re-validate: items/warehouses may have been deactivated since the draft was saved.
    await _validate(s, _input_of(doc))
    if doc.doc_type == DocType.LOAN_RETURN:
        await _check_loan_return_limits(s, doc)
    if doc.doc_type == DocType.ISSUE and doc.person_id is None:
        raise ValidationError("برای ثبت نهایی حواله خروج، تحویل‌گیرنده را انتخاب کنید.")
    override = await _stocktake_override(s, actor, doc, approval, stocktake_id)
    approver_id = override.pop("approved_by_id", None)
    await _apply_effects(s, doc, reverse=False)
    doc.status = DocStatus.POSTED
    doc.posted_at = dt.datetime.now()
    doc.posted_by_id = actor.user_id
    audit.record(s, actor, "document.posted", "document", doc.id,
                 {"type": doc.doc_type.value, "number": doc.number, **override},
                 approved_by_id=approver_id)


def _input_of(doc: Document) -> DocumentInput:
    return DocumentInput(
        doc_type=doc.doc_type, doc_date=doc.doc_date, warehouse_id=doc.warehouse_id,
        dest_warehouse_id=doc.dest_warehouse_id, person_id=doc.person_id,
        related_document_id=doc.related_document_id, description=doc.description,
        lines=[LineInput(ln.item_id, ln.unit_id, ln.qty, ln.unit_price, ln.notes)
               for ln in doc.lines],
    )


async def cancel_document(db: Database, actor: Actor, doc_id: int, reason: str = "",
                          approval: Approval | None = None) -> None:
    """Void a posted document with reversal ledger rows (history is never deleted)."""
    actor.require(Perm.DOCUMENTS_POST)
    async with db.session(actor.user_id) as s:
        doc = await _load(s, doc_id)
        if doc.status != DocStatus.POSTED:
            raise ValidationError("فقط سند ثبت‌شده قابل ابطال است.")
        await ensure_open_year(s, doc.fiscal_year)
        if doc.doc_type == DocType.LOAN_OUT:
            has_returns = await s.scalar(select(Document.id).where(
                Document.related_document_id == doc.id, Document.status == DocStatus.POSTED))
            if has_returns:
                raise ValidationError(
                    "برای این امانی، برگشت ثبت شده است. ابتدا برگشت‌ها را ابطال کنید."
                )
        override = await _stocktake_override(s, actor, doc, approval)
        approver_id = override.pop("approved_by_id", None)
        await _apply_effects(s, doc, reverse=True)
        doc.status = DocStatus.CANCELLED
        audit.record(s, actor, "document.cancelled", "document", doc.id,
                     {"type": doc.doc_type.value, "number": doc.number, "reason": reason, **override},
                     approved_by_id=approver_id)


async def create_and_post(db: Database, actor: Actor, data: DocumentInput,
                          approval: Approval | None = None) -> int:
    actor.require(Perm.DOCUMENTS_POST)
    """Save then post. If posting fails the document stays as a draft (not lost)."""
    doc_id = await create_document(db, actor, data)
    await post_document(db, actor, doc_id, approval=approval)
    return doc_id
