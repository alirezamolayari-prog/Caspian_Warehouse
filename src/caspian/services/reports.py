"""Reports as plain tables (shown on screen, exported to Excel/PDF, used by the AI).

Burn-rate analysis is deterministic so it works offline; the AI only adds prose.
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.db.database import Database
from caspian.db.models import (
    AuditLog,
    Category,
    DocStatus,
    DocType,
    Document,
    DocumentLine,
    Item,
    StockBalance,
    StockLedger,
    Unit,
    User,
    Warehouse,
)
from caspian.services.actor import Actor
from caspian.services.documents import DOC_TYPE_NAMES, outstanding_loans
from caspian.services.errors import NotFound


@dataclass(frozen=True)
class Column:
    title: str
    kind: str = "text"  # text | code | qty | money | int | date


@dataclass
class ReportTable:
    title: str
    columns: list[Column]
    rows: list[list[Any]]
    meta: list[str] = field(default_factory=list)
    totals: dict[int, Any] = field(default_factory=dict)  # column index -> total
    row_ids: list[Any] = field(default_factory=list)  # e.g. item ids, for drill-down


def _period(date_from: dt.date | None, date_to: dt.date | None) -> str:
    if not date_from and not date_to:
        return "همه تاریخ‌ها"
    a = jalali.format_date(date_from) if date_from else "ابتدا"
    b = jalali.format_date(date_to) if date_to else "امروز"
    return f"از {a} تا {b}"


async def _warehouse_name(s, warehouse_id: int | None) -> str:
    if warehouse_id is None:
        return "همه انبارها"
    wh = await s.get(Warehouse, warehouse_id)
    if wh is None:
        raise NotFound("انبار پیدا نشد.")
    return wh.name


async def last_purchase_prices(s, item_ids: list[int] | None = None) -> dict[int, Decimal]:
    """Latest posted receipt price per item, converted to the base unit."""
    ranked = (
        select(DocumentLine.item_id, DocumentLine.unit_price, DocumentLine.factor,
               func.row_number().over(partition_by=DocumentLine.item_id,
                                      order_by=(Document.doc_date.desc(), Document.id.desc()))
               .label("rn"))
        .join(Document, Document.id == DocumentLine.document_id)
        .where(Document.doc_type.in_([DocType.RECEIPT, DocType.OPENING]),
               Document.status == DocStatus.POSTED, DocumentLine.unit_price.is_not(None))
    )
    if item_ids is not None:
        ranked = ranked.where(DocumentLine.item_id.in_(item_ids))
    ranked = ranked.subquery()
    rows = (await s.execute(select(ranked.c.item_id, ranked.c.unit_price, ranked.c.factor)
                            .where(ranked.c.rn == 1))).all()
    return {i: (p / f if f else p) for i, p, f in rows}


# ----- stock balance -----


async def stock_balance(db: Database, actor: Actor, warehouse_id: int | None = None,
                        category_id: int | None = None, include_zero: bool = False) -> ReportTable:
    actor.require(Perm.REPORTS_VIEW)
    actor.require(Perm.STOCK_VIEW)
    async with db.session() as s:
        wh_name = await _warehouse_name(s, warehouse_id)
        qty = select(StockBalance.item_id, func.sum(StockBalance.qty).label("qty")) \
            .group_by(StockBalance.item_id)
        if warehouse_id is not None:
            qty = qty.where(StockBalance.warehouse_id == warehouse_id)
        qty = qty.subquery()
        stmt = (select(Item, Category.name, Unit.name, func.coalesce(qty.c.qty, 0))
                .join(Unit, Unit.id == Item.base_unit_id)
                .outerjoin(Category, Category.id == Item.category_id)
                .outerjoin(qty, qty.c.item_id == Item.id)
                .where(Item.is_active).order_by(Item.code))
        if category_id is not None:
            stmt = stmt.where(Item.category_id == category_id)
        rows = (await s.execute(stmt)).all()
        prices = await last_purchase_prices(s)
    out, ids = [], []
    total_value = Decimal(0)
    for item, cat, unit, q in rows:
        q = Decimal(q)
        if not include_zero and q == 0:
            continue
        price = prices.get(item.id)
        value = (q * price) if price is not None else None
        total_value += value or 0
        out.append([item.code, item.name, cat or "", unit, q, item.reorder_point, price, value])
        ids.append(item.id)
    return ReportTable(
        "گزارش موجودی کالا",
        [Column("کد", "code"), Column("نام کالا"), Column("گروه"), Column("واحد"), Column("موجودی", "qty"),
         Column("نقطه سفارش", "qty"), Column("آخرین فی خرید", "money"), Column("ارزش", "money")],
        out, [f"انبار: {wh_name}", f"{to_persian_digits(len(out))} قلم کالا"], {7: total_value}, ids)


# ----- cardex -----


async def cardex(db: Database, actor: Actor, item_id: int, warehouse_id: int | None = None,
                 date_from: dt.date | None = None, date_to: dt.date | None = None) -> ReportTable:
    """Item ledger with running balance. Cancelled documents (and their reversals) are hidden."""
    actor.require(Perm.REPORTS_VIEW)
    actor.require(Perm.STOCK_VIEW)
    async with db.session() as s:
        item = await s.get(Item, item_id)
        if item is None:
            raise NotFound("کالا پیدا نشد.")
        wh_name = await _warehouse_name(s, warehouse_id)
        base = [StockLedger.item_id == item_id, Document.status == DocStatus.POSTED]
        if warehouse_id is not None:
            base.append(StockLedger.warehouse_id == warehouse_id)
        opening = Decimal(0)
        if date_from is not None:
            opening = await s.scalar(
                select(func.coalesce(func.sum(StockLedger.qty_change), 0))
                .join(Document, Document.id == StockLedger.document_id)
                .where(*base, StockLedger.doc_date < date_from)) or Decimal(0)
        stmt = (select(StockLedger, Document, Warehouse.name)
                .join(Document, Document.id == StockLedger.document_id)
                .join(Warehouse, Warehouse.id == StockLedger.warehouse_id)
                .where(*base).order_by(StockLedger.doc_date, StockLedger.id))
        if date_from is not None:
            stmt = stmt.where(StockLedger.doc_date >= date_from)
        if date_to is not None:
            stmt = stmt.where(StockLedger.doc_date <= date_to)
        entries = (await s.execute(stmt)).all()
        unit = (await s.get(Unit, item.base_unit_id)).name
    balance = Decimal(opening)
    rows = [[None, "مانده از قبل", None, "", "", None, None, balance]] if date_from else []
    total_in = total_out = Decimal(0)
    for entry, doc, wh in entries:
        balance += entry.qty_change
        qty_in = entry.qty_change if entry.qty_change > 0 else None
        qty_out = -entry.qty_change if entry.qty_change < 0 else None
        total_in += qty_in or 0
        total_out += qty_out or 0
        rows.append([entry.doc_date, DOC_TYPE_NAMES[doc.doc_type], doc.number, wh,
                     doc.description, qty_in, qty_out, balance])
    return ReportTable(
        f"کاردکس کالا: {item.name} ({item.code})",
        [Column("تاریخ", "date"), Column("نوع سند"), Column("شماره", "int"), Column("انبار"),
         Column("شرح"), Column(f"ورود ({unit})", "qty"), Column(f"خروج ({unit})", "qty"),
         Column("مانده", "qty")],
        rows, [f"انبار: {wh_name}", _period(date_from, date_to)],
        {5: total_in, 6: total_out, 7: balance})


# ----- burn rate / reorder -----


@dataclass(frozen=True)
class ReorderParams:
    lookback_days: int = 90
    lead_time_days: int = 14  # supplier delivery time
    cover_days: int = 60  # how long an order should last
    safety_days: int = 7  # buffer against demand spikes


DEFAULT_PARAMS = ReorderParams()


@dataclass(frozen=True)
class BurnRate:
    item_id: int
    code: str
    name: str
    unit: str
    on_hand: Decimal
    consumed: Decimal  # over the lookback window
    daily: Decimal
    coverage_days: int | None  # None = no consumption
    reorder_point: Decimal | None  # current setting
    suggested_reorder_point: Decimal
    suggested_order_qty: Decimal

    @property
    def suggestion_text(self) -> str:
        """e.g. «۵۰ عدد سفارش دهید؛ حدود ۲٫۵ ماه مصرف را پوشش می‌دهد»."""
        if self.suggested_order_qty <= 0 or self.daily <= 0:
            return "نیازی به سفارش نیست."
        months = (self.suggested_order_qty / self.daily) / Decimal(30)
        return (f"{format_qty(self.suggested_order_qty)} {self.unit} سفارش دهید؛ حدود "
                f"{format_qty(months.quantize(Decimal('0.1')))} ماه مصرف را پوشش می‌دهد.")


def _ceil(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def compute_burn_rate(item_id, code, name, unit, on_hand: Decimal, consumed: Decimal,
                      reorder_point: Decimal | None, params: ReorderParams) -> BurnRate:
    daily = consumed / Decimal(params.lookback_days) if params.lookback_days else Decimal(0)
    coverage = int(on_hand / daily) if daily > 0 else None
    suggested_rp = _ceil(daily * (params.lead_time_days + params.safety_days))
    target = daily * (params.lead_time_days + params.cover_days + params.safety_days)
    order = max(Decimal(0), _ceil(target - on_hand)) if daily > 0 else Decimal(0)
    return BurnRate(item_id, code, name, unit, on_hand, consumed, daily, coverage, reorder_point,
                    suggested_rp, order)


async def burn_rates(db: Database, actor: Actor, params: ReorderParams = DEFAULT_PARAMS,
                     warehouse_id: int | None = None, only_needing_order: bool = False,
                     today: dt.date | None = None) -> list[BurnRate]:
    actor.require(Perm.REPORTS_VIEW)
    actor.require(Perm.STOCK_VIEW)
    today = today or dt.date.today()
    since = today - dt.timedelta(days=params.lookback_days)
    async with db.session() as s:
        used = (select(StockLedger.item_id, func.sum(-StockLedger.qty_change).label("used"))
                .join(Document, Document.id == StockLedger.document_id)
                .where(Document.doc_type == DocType.ISSUE, Document.status == DocStatus.POSTED,
                       StockLedger.doc_date > since, StockLedger.doc_date <= today)
                .group_by(StockLedger.item_id))
        stock = select(StockBalance.item_id, func.sum(StockBalance.qty).label("qty")) \
            .group_by(StockBalance.item_id)
        if warehouse_id is not None:
            used = used.where(StockLedger.warehouse_id == warehouse_id)
            stock = stock.where(StockBalance.warehouse_id == warehouse_id)
        used, stock = used.subquery(), stock.subquery()
        rows = (await s.execute(
            select(Item, Unit.name, func.coalesce(stock.c.qty, 0), func.coalesce(used.c.used, 0))
            .join(Unit, Unit.id == Item.base_unit_id)
            .outerjoin(stock, stock.c.item_id == Item.id)
            .outerjoin(used, used.c.item_id == Item.id)
            .where(Item.is_active).order_by(Item.code))).all()
    result = [compute_burn_rate(i.id, i.code, i.name, unit, Decimal(q), Decimal(c),
                                i.reorder_point, params) for i, unit, q, c in rows]
    if only_needing_order:
        # Same rule as the dashboard's «زیر نقطه سفارش» (at/below the configured reorder point, even
        # with no recent consumption), plus items whose burn rate says they'll run out (#11).
        result = [r for r in result
                  if (r.reorder_point is not None and r.on_hand <= r.reorder_point)
                  or (r.reorder_point is None and r.suggested_order_qty > 0
                      and r.on_hand <= r.suggested_reorder_point)]
    return result


def burn_rate_table(rates: list[BurnRate], params: ReorderParams) -> ReportTable:
    return ReportTable(
        "تحلیل مصرف و پیشنهاد سفارش",
        [Column("کد", "code"), Column("نام کالا"), Column("واحد"), Column("موجودی", "qty"),
         Column(to_persian_digits(f"مصرف {params.lookback_days} روز"), "qty"), Column("مصرف روزانه", "qty"),
         Column("پوشش (روز)", "int"), Column("نقطه سفارش فعلی", "qty"),
         Column("نقطه سفارش پیشنهادی", "qty"), Column("مقدار سفارش پیشنهادی", "qty")],
        [[r.code, r.name, r.unit, r.on_hand, r.consumed, r.daily.quantize(Decimal("0.01")),
          r.coverage_days, r.reorder_point, r.suggested_reorder_point, r.suggested_order_qty]
         for r in rates],
        [to_persian_digits(
            f"بازه مصرف: {params.lookback_days} روز گذشته — زمان تحویل: {params.lead_time_days} روز — "
            f"پوشش سفارش: {params.cover_days} روز — ذخیره اطمینان: {params.safety_days} روز")],
        row_ids=[r.item_id for r in rates],
    )


# ----- loans -----


async def loans_report(db: Database, actor: Actor) -> ReportTable:
    actor.require(Perm.REPORTS_VIEW)
    rows = await outstanding_loans(db, actor)
    return ReportTable(
        "گزارش امانی‌های باز",
        [Column("شماره امانی", "int"), Column("تاریخ", "date"), Column("تحویل‌گیرنده"),
         Column("کد", "code"), Column("کالا"), Column("مانده", "qty"), Column("واحد"), Column("روز", "int")],
        [[r.number, r.doc_date, r.person, r.item_code, r.item_name, r.outstanding, r.base_unit,
          r.days_out] for r in rows],
        [f"{to_persian_digits(len(rows))} قلم امانی باز"])


# ----- user activity -----

ACTION_NAMES = {
    "auth.login": "ورود به سیستم", "auth.logout": "خروج از سیستم",
    "auth.login_failed": "ورود ناموفق", "auth.locked": "قفل شدن حساب",
    "auth.password_changed": "تغییر رمز عبور", "auth.pin_set": "تنظیم PIN",
    "approval.granted": "تأیید عملیات حساس", "approval.denied": "رد تأیید عملیات حساس",
    "approval.refused_ai": "رد درخواست دستیار هوشمند",
    "user.created": "ایجاد کاربر", "user.updated": "ویرایش کاربر",
    "user.role_changed": "تغییر نقش کاربر", "user.password_reset": "بازنشانی رمز",
    "user.activated": "فعال‌سازی کاربر", "user.deactivated": "غیرفعال‌سازی کاربر",
    "item.created": "ایجاد کالا", "item.updated": "ویرایش کالا", "item.deleted": "حذف کالا",
    "item.activated": "فعال‌سازی کالا", "item.deactivated": "غیرفعال‌سازی کالا",
    "item.overwritten_by_import": "بازنویسی کالا از ورود اطلاعات",
    "document.created": "ایجاد سند", "document.updated": "ویرایش سند",
    "document.posted": "ثبت نهایی سند", "document.cancelled": "ابطال سند",
    "document.draft_deleted": "حذف پیش‌نویس سند",
    "document.assistant_draft_discarded": "رد پیش‌نویس دستیار",
    "location.created": "ایجاد محل مصرف", "location.updated": "ویرایش محل مصرف",
    "location.activated": "فعال‌سازی محل مصرف", "location.deactivated": "غیرفعال‌سازی محل مصرف",
    "locations.imported": "ورود محل‌های مصرف از فایل", "locations.settings": "تنظیمات محل مصرف",
    "import.batch_created": "ایجاد پیش‌نویس ورود اطلاعات",
    "import.batch_applied": "اعمال ورود اطلاعات", "import.batch_discarded": "حذف پیش‌نویس ورود",
    "stocktake.created": "شروع انبارگردانی", "stocktake.submitted": "ثبت شمارش",
    "stocktake.approved": "تأیید انبارگردانی", "stocktake.cancelled": "لغو انبارگردانی",
}


async def user_activity(db: Database, actor: Actor, user_id: int | None = None,
                        date_from: dt.date | None = None, date_to: dt.date | None = None,
                        limit: int = 5000) -> ReportTable:
    actor.require(Perm.REPORTS_VIEW)
    actor.require(Perm.USERS_MANAGE)  # other people's activity is admin-level information
    approver = aliased(User)
    stmt = (select(AuditLog, User.username, approver.username)
            .outerjoin(User, User.id == AuditLog.user_id)
            .outerjoin(approver, approver.id == AuditLog.approved_by_id)
            .order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit))
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if date_from is not None:
        stmt = stmt.where(AuditLog.at >= dt.datetime.combine(date_from, dt.time.min))
    if date_to is not None:
        stmt = stmt.where(AuditLog.at <= dt.datetime.combine(date_to, dt.time.max))
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
        who = "همه کاربران"
        if user_id is not None:
            user = await s.get(User, user_id)
            who = user.username if user else "?"
    out = []
    for entry, username, approved_by in rows:
        details = dict(entry.details or {})
        action = ACTION_NAMES.get(entry.action, entry.action)
        # Who really did it: the assistant on the user's behalf, or the user confirming its draft.
        if details.pop("via_ai", False):
            action += f" — توسط دستیار از طرف {details.pop('on_behalf_of', username or '')}"
        elif details.get("via") == "assistant":
            details.pop("via")
            action += f" — به پیشنهاد دستیار، با تأیید {username or ''}"
        summary = "، ".join(f"{k}: {v}" for k, v in details.items()
                           if k not in ("before",))[:200]
        out.append([entry.at, username or "سیستم", action,
                    f"{entry.entity_type or ''} {entry.entity_id or ''}".strip(), summary,
                    entry.machine or "", approved_by or ""])
    return ReportTable(
        "گزارش فعالیت کاربران",
        [Column("زمان", "date"), Column("کاربر"), Column("عملیات"), Column("موضوع"),
         Column("جزئیات"), Column("رایانه"), Column("تأییدکننده")],
        out, [f"کاربر: {who}", _period(date_from, date_to)])



# ----- consumption by floor / location (docs/specs/caspian_consumption_location_spec.md §2.8) -----

NO_PLACE = "بدون محل مصرف"


@dataclass(frozen=True)
class ConsumptionRow:
    """One group of posted issue vouchers. Codes, not ids: also what the owner app receives."""

    location_code: str | None
    location: str  # display text, e.g. «پلاک ۶۸ · همکف» (empty when grouping by floor)
    floor_code: str | None
    floor: str
    floor_sort: int
    documents: int
    items: int
    qty: Decimal  # base-unit quantity; meaningful when one item is filtered
    value: Decimal  # at the latest purchase price


def month_start(today: dt.date | None = None) -> dt.date:
    """First day of the current Jalali month («this month» in reports and questions)."""
    j = jalali.to_jalali(today or dt.date.today())
    return j.replace(day=1).togregorian()


async def consumption(db: Database, actor: Actor, group_by: str = "floor",
                      date_from: dt.date | None = None, date_to: dt.date | None = None,
                      item_id: int | None = None, person_id: int | None = None,
                      warehouse_id: int | None = None) -> list[ConsumptionRow]:
    """Posted issue vouchers grouped by the floor (building order, every floor listed) or by the
    location + floor stored on each voucher. Deactivated locations still appear: history is never
    hidden. Vouchers from before locations existed are grouped as «بدون محل مصرف»."""
    from caspian.db.models import Floor, Location
    from caspian.services.locations import FloorRow, place_label

    actor.require(Perm.REPORTS_VIEW)
    if group_by not in ("floor", "location"):
        raise ValueError(group_by)
    async with db.session() as s:
        stmt = (select(Document.id, Document.location_id, Document.consumption_floor_id,
                       DocumentLine.item_id, DocumentLine.base_qty)
                .join(DocumentLine, DocumentLine.document_id == Document.id)
                .where(Document.doc_type == DocType.ISSUE, Document.status == DocStatus.POSTED))
        if date_from:
            stmt = stmt.where(Document.doc_date >= date_from)
        if date_to:
            stmt = stmt.where(Document.doc_date <= date_to)
        if item_id is not None:
            stmt = stmt.where(DocumentLine.item_id == item_id)
        if person_id is not None:
            stmt = stmt.where(Document.person_id == person_id)
        if warehouse_id is not None:
            stmt = stmt.where(Document.warehouse_id == warehouse_id)
        lines = (await s.execute(stmt)).all()
        floors = {f.id: f for f in (await s.scalars(select(Floor))).all()}
        places = {loc.id: loc for loc in (await s.scalars(select(Location))).unique().all()}
        prices = await last_purchase_prices(s, sorted({ln.item_id for ln in lines}))

    groups: dict[tuple, dict] = {}
    if group_by == "floor":
        for f in sorted(floors.values(), key=lambda f: f.sort_order):
            if f.is_active:
                groups[(f.id,)] = {"docs": set(), "items": set(), "qty": Decimal(0), "value": Decimal(0)}
    for doc_id, location_id, floor_id, line_item, qty in lines:
        key = (floor_id,) if group_by == "floor" else (location_id, floor_id)
        g = groups.setdefault(key, {"docs": set(), "items": set(), "qty": Decimal(0), "value": Decimal(0)})
        g["docs"].add(doc_id)
        g["items"].add(line_item)
        g["qty"] += qty
        g["value"] += qty * prices.get(line_item, Decimal(0))

    rows = []
    for key, g in groups.items():
        floor = floors.get(key[-1]) if key[-1] is not None else None
        floor_text = FloorRow(0, floor.code, floor.name_fa, 0, True).display if floor else NO_PLACE
        location_code, location_text = None, ""
        if group_by == "location":
            loc = places.get(key[0]) if key[0] is not None else None
            location_code = loc.code if loc else None
            location_text = place_label(loc.name_fa, loc.type, floor.code if floor else None,
                                        floor.name_fa if floor else None) if loc else NO_PLACE
        rows.append(ConsumptionRow(location_code, location_text, floor.code if floor else None, floor_text,
                                   floor.sort_order if floor else 999, len(g["docs"]), len(g["items"]),
                                   g["qty"], g["value"]))
    if group_by == "floor":
        rows.sort(key=lambda r: r.floor_sort)
    else:
        rows.sort(key=lambda r: (-r.documents, -r.value, r.floor_sort))
    return rows


async def consumption_table(db: Database, actor: Actor, group_by: str = "floor",
                            date_from: dt.date | None = None, date_to: dt.date | None = None,
                            item_id: int | None = None, person_id: int | None = None,
                            warehouse_id: int | None = None) -> ReportTable:
    rows = await consumption(db, actor, group_by, date_from, date_to, item_id, person_id, warehouse_id)
    meta = [_period(date_from, date_to)]
    async with db.session() as s:
        if item_id is not None:
            item = await s.get(Item, item_id)
            unit = await s.get(Unit, item.base_unit_id) if item else None
            meta.append(f"کالا: {item.name} ({unit.name if unit else ''})" if item else "")
        if person_id is not None:
            from caspian.db.models import Person

            person = await s.get(Person, person_id)
            meta.append(f"درخواست‌کننده / تحویل‌گیرنده: {person.name}" if person else "")
        if warehouse_id is not None:
            meta.append(await _warehouse_name(s, warehouse_id))
    columns = [Column("طبقه")] if group_by == "floor" else [Column("محل مصرف"), Column("طبقه")]
    columns += [Column("تعداد حواله", "int"), Column("تعداد اقلام", "int")]
    if item_id is not None:
        columns.append(Column("مقدار", "qty"))
    columns.append(Column("ارزش تقریبی (ریال)", "money"))
    table_rows = []
    for r in rows:
        cells = [r.floor] if group_by == "floor" else [r.location, r.floor]
        cells += [r.documents, r.items]
        if item_id is not None:
            cells.append(r.qty)
        cells.append(r.value)
        table_rows.append(cells)
    value_col = len(columns) - 1
    totals = {value_col: sum((r.value for r in rows), Decimal(0)),
              columns.index(next(c for c in columns if c.title == "تعداد حواله")):
                  sum(r.documents for r in rows)}
    if item_id is not None:
        totals[value_col - 1] = sum((r.qty for r in rows), Decimal(0))
    title = "مصرف به تفکیک طبقه" if group_by == "floor" else "مصرف به تفکیک محل"
    return ReportTable(title, columns, table_rows, [m for m in meta if m], totals,
                       row_ids=[r.location_code or r.floor_code for r in rows])
