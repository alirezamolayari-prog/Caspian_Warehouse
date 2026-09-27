"""Blind stocktake.

1. `create_stocktake` snapshots the system quantity of every active item (optionally one
   category) in a warehouse.
2. Counters record what they physically see (`record_counts`); they are never shown
   system quantities (`count_sheet` omits them).
3. `submit_counts` locks the counts.
4. Approvers review `discrepancy_report` and `approve`, which posts an ADJUSTMENT
   document for the differences (counted - snapshot).
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.core.permissions import Perm
from caspian.core.text import to_ascii_digits
from caspian.db.database import Database
from caspian.db.models import (
    Category,
    DocStatus,
    DocType,
    Document,
    Item,
    ItemBarcode,
    StockBalance,
    Stocktake,
    StocktakeLine,
    StocktakeStatus,
    Unit,
    User,
    Warehouse,
)
from caspian.services import audit, documents
from caspian.services.actor import Actor
from caspian.services.errors import NotFound, ValidationError

STATUS_NAMES = {
    StocktakeStatus.OPEN: "در حال شمارش",
    StocktakeStatus.COUNTED: "شمارش ثبت شده",
    StocktakeStatus.APPROVED: "تأیید و اعمال شده",
    StocktakeStatus.CANCELLED: "لغو شده",
}


@dataclass(frozen=True)
class StocktakeRow:
    id: int
    number: int
    warehouse: str
    category: str
    title: str
    status: StocktakeStatus
    snapshot_at: dt.datetime
    total: int
    counted: int
    created_by: str

    @property
    def status_name(self) -> str:
        return STATUS_NAMES[self.status]


@dataclass(frozen=True)
class SheetLine:
    """A line as a counter sees it: no system quantity."""

    id: int
    line_no: int
    item_id: int
    code: str
    name: str
    unit: str
    barcodes: tuple[str, ...]
    counted_qty: Decimal | None
    note: str


@dataclass(frozen=True)
class CountSheet:
    row: StocktakeRow
    lines: list[SheetLine]


@dataclass(frozen=True)
class DiffLine:
    line_no: int
    item_id: int
    code: str
    name: str
    unit: str
    system_qty: Decimal
    counted_qty: Decimal
    note: str

    @property
    def difference(self) -> Decimal:
        return self.counted_qty - self.system_qty


@dataclass(frozen=True)
class DiscrepancyReport:
    row: StocktakeRow
    lines: list[DiffLine]  # every counted line (differences and matches)
    movements_since_snapshot: int  # posted documents in the warehouse after the snapshot

    @property
    def differences(self) -> list[DiffLine]:
        return [ln for ln in self.lines if ln.difference != 0]


async def _load(s: AsyncSession, stocktake_id: int) -> Stocktake:
    st = await s.get(Stocktake, stocktake_id)
    if st is None:
        raise NotFound("انبارگردانی پیدا نشد.")
    return st


async def _row(s: AsyncSession, st: Stocktake) -> StocktakeRow:
    wh = await s.get(Warehouse, st.warehouse_id)
    cat = await s.get(Category, st.category_id) if st.category_id else None
    user = await s.get(User, st.created_by_id) if st.created_by_id else None
    return StocktakeRow(st.id, st.number, wh.name, cat.name if cat else "همه گروه‌ها", st.title,
                        st.status, st.snapshot_at, len(st.lines),
                        sum(ln.counted_qty is not None for ln in st.lines),
                        user.username if user else "")


# ----- queries -----


async def list_stocktakes(db: Database, actor: Actor, limit: int = 200) -> list[StocktakeRow]:
    actor.require(Perm.STOCKTAKE_RUN)
    async with db.session() as s:
        rows = (await s.scalars(select(Stocktake).order_by(Stocktake.id.desc()).limit(limit))).all()
        return [await _row(s, st) for st in rows]


async def count_sheet(db: Database, actor: Actor, stocktake_id: int) -> CountSheet:
    actor.require(Perm.STOCKTAKE_RUN)
    async with db.session() as s:
        st = await _load(s, stocktake_id)
        items = {i.id: i for i in (await s.scalars(
            select(Item).where(Item.id.in_([ln.item_id for ln in st.lines])))).all()}
        units = dict((await s.execute(select(Unit.id, Unit.name))).all())
        lines = [
            SheetLine(ln.id, ln.line_no, ln.item_id, items[ln.item_id].code,
                      items[ln.item_id].name, units[items[ln.item_id].base_unit_id],
                      tuple(b.barcode for b in items[ln.item_id].barcodes), ln.counted_qty,
                      ln.note)
            for ln in st.lines
        ]
        return CountSheet(await _row(s, st), lines)


async def discrepancy_report(db: Database, actor: Actor, stocktake_id: int) -> DiscrepancyReport:
    """System vs counted. Only for approvers, and only once counting is locked."""
    actor.require(Perm.STOCKTAKE_APPROVE)
    actor.require(Perm.STOCK_VIEW)
    async with db.session() as s:
        st = await _load(s, stocktake_id)
        if st.status == StocktakeStatus.OPEN:
            raise ValidationError("گزارش مغایرت پس از ثبت نهایی شمارش در دسترس است.")
        items = {i.id: i for i in (await s.scalars(
            select(Item).where(Item.id.in_([ln.item_id for ln in st.lines])))).all()}
        units = dict((await s.execute(select(Unit.id, Unit.name))).all())
        lines = [
            DiffLine(ln.line_no, ln.item_id, items[ln.item_id].code, items[ln.item_id].name,
                     units[items[ln.item_id].base_unit_id], ln.system_qty,
                     ln.counted_qty if ln.counted_qty is not None else Decimal(0), ln.note)
            for ln in st.lines
        ]
        moved = await s.scalar(
            select(func.count()).select_from(Document).where(
                Document.status == DocStatus.POSTED, Document.posted_at > st.snapshot_at,
                (Document.warehouse_id == st.warehouse_id)
                | (Document.dest_warehouse_id == st.warehouse_id),
                Document.id != (st.adjustment_document_id or -1),
            )
        )
        return DiscrepancyReport(await _row(s, st), lines, moved or 0)


async def find_line(db: Database, stocktake_id: int, code_or_barcode: str) -> int | None:
    """Line id for a scanned barcode or typed item code, within this stocktake."""
    key = to_ascii_digits(code_or_barcode.strip())
    if not key:
        return None
    async with db.session() as s:
        item_id = await s.scalar(select(ItemBarcode.item_id).where(ItemBarcode.barcode == key)) \
            or await s.scalar(select(Item.id).where(Item.code == key))
        if item_id is None:
            return None
        return await s.scalar(select(StocktakeLine.id).where(
            StocktakeLine.stocktake_id == stocktake_id, StocktakeLine.item_id == item_id))


# ----- commands -----


async def create_stocktake(db: Database, actor: Actor, warehouse_id: int,
                           category_id: int | None = None, title: str = "") -> int:
    actor.require(Perm.STOCKTAKE_RUN)
    async with db.session(actor.user_id) as s:
        wh = await s.get(Warehouse, warehouse_id)
        if wh is None or not wh.is_active:
            raise ValidationError("انبار نامعتبر یا غیرفعال است.")
        busy = await s.scalar(select(Stocktake.id).where(
            Stocktake.warehouse_id == warehouse_id,
            Stocktake.status.in_([StocktakeStatus.OPEN, StocktakeStatus.COUNTED])))
        if busy:
            raise ValidationError("برای این انبار یک انبارگردانی باز وجود دارد. ابتدا آن را "
                                  "تأیید یا لغو کنید.")
        stmt = select(Item).where(Item.is_active).order_by(Item.code)
        if category_id is not None:
            stmt = stmt.where(Item.category_id == category_id)
        item_list = (await s.scalars(stmt)).all()
        if not item_list:
            raise ValidationError("کالایی برای شمارش وجود ندارد.")
        balances = dict((await s.execute(
            select(StockBalance.item_id, StockBalance.qty)
            .where(StockBalance.warehouse_id == warehouse_id))).all())
        number = (await s.scalar(select(func.max(Stocktake.number))) or 0) + 1
        st = Stocktake(number=number, warehouse_id=warehouse_id, category_id=category_id,
                       title=title.strip(), snapshot_at=dt.datetime.now())
        st.lines = [StocktakeLine(line_no=i, item_id=item.id,
                                  system_qty=balances.get(item.id, Decimal(0)))
                    for i, item in enumerate(item_list, start=1)]
        s.add(st)
        await s.flush()
        audit.record(s, actor, "stocktake.created", "stocktake", st.id,
                     {"number": number, "warehouse_id": warehouse_id, "lines": len(st.lines)})
        return st.id


async def record_counts(db: Database, actor: Actor, stocktake_id: int,
                        counts: dict[int, tuple[Decimal | None, str]]) -> None:
    """counts: line_id -> (counted qty or None to clear, note)."""
    actor.require(Perm.STOCKTAKE_RUN)
    async with db.session(actor.user_id) as s:
        st = await _load(s, stocktake_id)
        if st.status != StocktakeStatus.OPEN:
            raise ValidationError("شمارش این انبارگردانی قفل شده است.")
        lines = {ln.id: ln for ln in st.lines}
        now = dt.datetime.now()
        for line_id, (qty, note) in counts.items():
            line = lines.get(line_id)
            if line is None:
                raise ValidationError("ردیف متعلق به این انبارگردانی نیست.")
            if qty is not None and qty < 0:
                raise ValidationError(f"ردیف {line.line_no}: مقدار شمارش نمی‌تواند منفی باشد.")
            if qty != line.counted_qty or note.strip() != line.note:
                line.counted_qty = qty
                line.note = note.strip()
                line.counted_by_id = actor.user_id
                line.counted_at = now


async def submit_counts(db: Database, actor: Actor, stocktake_id: int,
                        missing_as_zero: bool = False) -> None:
    actor.require(Perm.STOCKTAKE_RUN)
    async with db.session(actor.user_id) as s:
        st = await _load(s, stocktake_id)
        if st.status != StocktakeStatus.OPEN:
            raise ValidationError("این انبارگردانی قبلاً ثبت نهایی شده است.")
        missing = [ln for ln in st.lines if ln.counted_qty is None]
        if missing and not missing_as_zero:
            raise ValidationError(f"{len(missing)} ردیف شمارش نشده است. آن‌ها را بشمارید یا "
                                  "گزینه «صفر در نظر گرفتن شمارش‌نشده‌ها» را انتخاب کنید.")
        for ln in missing:
            ln.counted_qty = Decimal(0)
            ln.counted_by_id = actor.user_id
            ln.counted_at = dt.datetime.now()
        st.status = StocktakeStatus.COUNTED
        st.submitted_at = dt.datetime.now()
        st.submitted_by_id = actor.user_id
        audit.record(s, actor, "stocktake.submitted", "stocktake", st.id,
                     {"missing_as_zero": len(missing)})


async def approve(db: Database, actor: Actor, stocktake_id: int) -> int | None:
    """Post an adjustment for all differences. Returns the document id (None if no diffs)."""
    actor.require(Perm.STOCKTAKE_APPROVE)
    actor.require(Perm.DOCUMENTS_POST)
    async with db.session(actor.user_id) as s:
        st = await _load(s, stocktake_id)
        if st.status != StocktakeStatus.COUNTED:
            raise ValidationError("فقط انبارگردانی با شمارش ثبت‌شده قابل تأیید است.")
        doc_id = None
        diffs = [ln for ln in st.lines if ln.counted_qty != ln.system_qty]
        if diffs:
            base_units = dict((await s.execute(
                select(Item.id, Item.base_unit_id)
                .where(Item.id.in_([ln.item_id for ln in diffs])))).all())
            doc = await documents.create_document_in(s, actor, documents.DocumentInput(
                DocType.ADJUSTMENT, dt.date.today(), st.warehouse_id,
                [documents.LineInput(ln.item_id, base_units[ln.item_id],
                                     ln.counted_qty - ln.system_qty,
                                     notes=f"انبارگردانی {st.number} — ردیف {ln.line_no}")
                 for ln in diffs],
                description=f"اصلاحیه انبارگردانی شماره {st.number}"))
            await documents.post_document_in(s, actor, doc)
            doc_id = st.adjustment_document_id = doc.id
        st.status = StocktakeStatus.APPROVED
        st.approved_at = dt.datetime.now()
        st.approved_by_id = actor.user_id
        audit.record(s, actor, "stocktake.approved", "stocktake", st.id,
                     {"differences": len(diffs), "document_id": doc_id})
        return doc_id


async def cancel(db: Database, actor: Actor, stocktake_id: int) -> None:
    actor.require(Perm.STOCKTAKE_APPROVE)
    async with db.session(actor.user_id) as s:
        st = await _load(s, stocktake_id)
        if st.status not in (StocktakeStatus.OPEN, StocktakeStatus.COUNTED):
            raise ValidationError("این انبارگردانی قابل لغو نیست.")
        st.status = StocktakeStatus.CANCELLED
        audit.record(s, actor, "stocktake.cancelled", "stocktake", st.id)
