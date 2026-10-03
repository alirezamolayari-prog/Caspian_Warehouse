"""Read-only data health check (QA round 1, #10).

Validation now blocks these cases, but data entered before it may contain them. Nothing here
changes data: the admin fixes each finding with normal documents (cancel and re-enter, or an
adjustment), so the history stays intact.
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select

from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.db.database import Database
from caspian.db.models import DocStatus, Document, Item, StockBalance, Unit, Warehouse
from caspian.services.actor import Actor
from caspian.services.documents import DOC_TYPE_NAMES, number_text
from caspian.services.fiscal_state import read_state


@dataclass(frozen=True)
class Finding:
    kind: str  # future_date | outside_year | fractional_balance
    title: str
    detail: str
    hint: str


FIX_DOCUMENT = "سند را ابطال کنید و با تاریخ درست دوباره ثبت کنید (پیش‌نویس را ویرایش کنید)."
FIX_BALANCE = "با یک سند «اصلاح موجودی» مقدار را به عدد صحیح برسانید."


async def check(db: Database, actor: Actor, today: dt.date | None = None) -> list[Finding]:
    actor.require(Perm.DOCUMENTS_VIEW)
    today = today or dt.date.today()
    current_year = jalali.fiscal_year_of(today)
    findings: list[Finding] = []
    async with db.session() as s:
        closed = (await read_state(s)).closed_through
        docs = (await s.scalars(select(Document).where(
            Document.status != DocStatus.CANCELLED,
            (Document.doc_date > today) | (Document.fiscal_year > current_year)
            | (Document.fiscal_year <= (closed if closed is not None else -1)))
            .order_by(Document.doc_date))).all()
        for d in docs:
            label = f"{DOC_TYPE_NAMES[d.doc_type]} {number_text(d.doc_type, d.number)}"
            date = jalali.format_date(d.doc_date)
            if d.doc_date > today:
                findings.append(Finding("future_date", label, f"تاریخ {date} بعد از امروز است.",
                                        FIX_DOCUMENT))
            else:
                findings.append(Finding("outside_year", label,
                                        f"تاریخ {date} در سال مالی باز ({current_year}) نیست.", FIX_DOCUMENT))
        balances = (await s.execute(
            select(Item.code, Item.name, Unit.name, Warehouse.name, StockBalance.qty)
            .join(Item, Item.id == StockBalance.item_id)
            .join(Unit, Unit.id == Item.base_unit_id)
            .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
            .where(Unit.allow_decimal.is_(False)))).all()
        for code, name, unit, warehouse, qty in balances:
            if qty != qty.to_integral_value():
                findings.append(Finding(
                    "fractional_balance", f"کالای {code} — {name}",
                    f"موجودی {format_qty(qty)} {unit} در «{warehouse}»؛ «{unit}» فقط عدد صحیح می‌پذیرد.",
                    FIX_BALANCE))
    return findings
