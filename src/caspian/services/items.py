"""Items: search, create/edit, units & barcodes, activation (protected) and deletion."""

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from caspian.core.permissions import Perm
from caspian.core.text import normalize, to_ascii_digits
from caspian.db.database import Database
from caspian.db.models import (
    Category,
    DocumentLine,
    Item,
    ItemBarcode,
    ItemUnit,
    StockBalance,
    Unit,
)
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import ConcurrencyError, NotFound, ValidationError
from caspian.services.protected import Approval, ProtectedAction, consume

FIRST_CODE = 1001


@dataclass
class ItemInput:
    code: str
    name: str
    base_unit_id: int
    category_id: int | None = None
    reorder_point: Decimal | None = None
    reorder_qty: Decimal | None = None
    is_returnable: bool = False
    description: str = ""
    # (unit_id, factor): 1 unit = factor base units
    units: list[tuple[int, Decimal]] = field(default_factory=list)
    # (barcode, unit_id or None for the base unit)
    barcodes: list[tuple[str, int | None]] = field(default_factory=list)


@dataclass(frozen=True)
class ItemRow:
    id: int
    code: str
    name: str
    category: str
    base_unit: str
    on_hand: Decimal
    reorder_point: Decimal | None
    is_active: bool
    is_returnable: bool

    @property
    def below_reorder(self) -> bool:
        return self.reorder_point is not None and self.on_hand <= self.reorder_point


@dataclass(frozen=True)
class ItemDetail:
    id: int
    version_id: int
    input: ItemInput
    is_active: bool
    has_movements: bool


# ----- queries -----


def _on_hand_subquery():
    return (
        select(StockBalance.item_id, func.sum(StockBalance.qty).label("on_hand"))
        .group_by(StockBalance.item_id)
        .subquery()
    )


def _search_filter(query: str):
    """All words must match the name, or the whole query matches code/barcode."""
    raw = to_ascii_digits(query.strip())
    words = normalize(query).split()
    if not words:
        return None
    by_name = and_(*(Item.name_normalized.contains(w, autoescape=True) for w in words))
    by_code = Item.code.startswith(raw, autoescape=True)
    by_barcode = exists().where(ItemBarcode.item_id == Item.id, ItemBarcode.barcode == raw)
    return or_(by_name, by_code, by_barcode)


async def search_items(
    db: Database, actor: Actor, query: str = "", include_inactive: bool = False,
    category_id: int | None = None, only_below_reorder: bool = False, limit: int = 500,
) -> list[ItemRow]:
    actor.require(Perm.ITEMS_VIEW)
    on_hand = _on_hand_subquery()
    qty = func.coalesce(on_hand.c.on_hand, 0)
    stmt = (
        select(Item, Category.name, Unit.name, qty)
        .join(Unit, Item.base_unit_id == Unit.id)
        .outerjoin(Category, Item.category_id == Category.id)
        .outerjoin(on_hand, on_hand.c.item_id == Item.id)
    )
    if (cond := _search_filter(query)) is not None:
        stmt = stmt.where(cond)
    if not include_inactive:
        stmt = stmt.where(Item.is_active)
    if category_id is not None:
        stmt = stmt.where(Item.category_id == category_id)
    if only_below_reorder:
        stmt = stmt.where(Item.reorder_point.is_not(None), qty <= Item.reorder_point)
    raw = to_ascii_digits(query.strip())
    # Exact code matches first, then by name.
    stmt = stmt.order_by((Item.code == raw).desc(), Item.name).limit(limit)
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    return [
        ItemRow(item.id, item.code, item.name, cat or "", unit, Decimal(q or 0),
                item.reorder_point, item.is_active, item.is_returnable)
        for item, cat, unit, q in rows
    ]


async def count_summary(db: Database) -> tuple[int, int]:
    """(active items, active items at/below their reorder point)."""
    on_hand = _on_hand_subquery()
    qty = func.coalesce(on_hand.c.on_hand, 0)
    async with db.session() as s:
        total = await s.scalar(select(func.count()).select_from(Item).where(Item.is_active))
        low = await s.scalar(
            select(func.count()).select_from(Item)
            .outerjoin(on_hand, on_hand.c.item_id == Item.id)
            .where(Item.is_active, Item.reorder_point.is_not(None), qty <= Item.reorder_point)
        )
    return total or 0, low or 0


async def _has_movements(s: AsyncSession, item_id: int) -> bool:
    return bool(await s.scalar(select(DocumentLine.id).where(DocumentLine.item_id == item_id)
                               .limit(1)))


async def _load(s: AsyncSession, item_id: int) -> Item:
    item = await s.get(Item, item_id)
    if item is None:
        raise NotFound("کالا پیدا نشد.")
    return item


async def get_item(db: Database, actor: Actor, item_id: int) -> ItemDetail:
    actor.require(Perm.ITEMS_VIEW)
    async with db.session() as s:
        item = await _load(s, item_id)
        data = ItemInput(
            code=item.code, name=item.name, base_unit_id=item.base_unit_id,
            category_id=item.category_id, reorder_point=item.reorder_point,
            reorder_qty=item.reorder_qty, is_returnable=item.is_returnable,
            description=item.description,
            units=[(u.unit_id, u.factor) for u in item.units],
            barcodes=[(b.barcode, b.unit_id) for b in item.barcodes],
        )
        return ItemDetail(item.id, item.version_id, data, item.is_active,
                          await _has_movements(s, item.id))


async def lookup_barcode(db: Database, barcode: str) -> tuple[int, int | None] | None:
    """(item_id, unit_id) for a scanned barcode, or None."""
    async with db.session() as s:
        row = (await s.execute(
            select(ItemBarcode.item_id, ItemBarcode.unit_id)
            .where(ItemBarcode.barcode == to_ascii_digits(barcode.strip()))
        )).first()
    return (row[0], row[1]) if row else None


async def next_code_in(s: AsyncSession) -> str:
    codes = (await s.scalars(select(Item.code))).all()
    numeric = [int(c) for c in codes if c.isdigit()]
    return str(max(numeric, default=FIRST_CODE - 1) + 1)


async def next_code(db: Database) -> str:
    async with db.session() as s:
        return await next_code_in(s)


# ----- validation -----


async def _validate(s: AsyncSession, data: ItemInput, item_id: int | None) -> ItemInput:
    code = to_ascii_digits(data.code.strip())
    name = " ".join(data.name.split())
    if not code:
        raise ValidationError("کد کالا الزامی است.")
    if not name:
        raise ValidationError("نام کالا الزامی است.")
    clash = await s.scalar(select(Item.id).where(Item.code == code, Item.id != (item_id or -1)))
    if clash:
        raise ValidationError(f"کد «{code}» قبلاً برای کالای دیگری ثبت شده است.")
    if await s.get(Unit, data.base_unit_id) is None:
        raise ValidationError("واحد اصلی نامعتبر است.")
    if data.category_id is not None and await s.get(Category, data.category_id) is None:
        raise ValidationError("گروه کالا نامعتبر است.")
    for value, label in ((data.reorder_point, "نقطه سفارش"), (data.reorder_qty, "مقدار سفارش")):
        if value is not None and value < 0:
            raise ValidationError(f"{label} نمی‌تواند منفی باشد.")

    unit_ids = {data.base_unit_id}
    for unit_id, factor in data.units:
        if unit_id in unit_ids:
            raise ValidationError("هر واحد فقط یک بار (و غیر از واحد اصلی) قابل تعریف است.")
        if factor is None or factor <= 0:
            raise ValidationError("ضریب تبدیل واحد باید بزرگ‌تر از صفر باشد.")
        unit_ids.add(unit_id)

    barcodes: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    for barcode, unit_id in data.barcodes:
        barcode = to_ascii_digits(barcode.strip())
        if not barcode:
            continue
        if barcode in seen:
            raise ValidationError(f"بارکد «{barcode}» تکراری است.")
        if unit_id is not None and unit_id not in unit_ids:
            raise ValidationError(f"واحد بارکد «{barcode}» برای این کالا تعریف نشده است.")
        owner = await s.scalar(
            select(Item.name).join(ItemBarcode, ItemBarcode.item_id == Item.id)
            .where(ItemBarcode.barcode == barcode, Item.id != (item_id or -1))
        )
        if owner:
            raise ValidationError(f"بارکد «{barcode}» متعلق به کالای «{owner}» است.")
        seen.add(barcode)
        barcodes.append((barcode, None if unit_id == data.base_unit_id else unit_id))

    return ItemInput(code, name, data.base_unit_id, data.category_id, data.reorder_point,
                     data.reorder_qty, data.is_returnable, data.description.strip(),
                     list(data.units), barcodes)


def _apply(item: Item, data: ItemInput) -> None:
    item.code = data.code
    item.name = data.name
    item.name_normalized = normalize(data.name)
    item.base_unit_id = data.base_unit_id
    item.category_id = data.category_id
    item.reorder_point = data.reorder_point
    item.reorder_qty = data.reorder_qty
    item.is_returnable = data.is_returnable
    item.description = data.description
    wanted_units = dict(data.units)
    for existing in list(item.units):
        if existing.unit_id not in wanted_units:
            item.units.remove(existing)
        else:
            existing.factor = wanted_units.pop(existing.unit_id)
    item.units.extend(ItemUnit(unit_id=u, factor=f) for u, f in wanted_units.items())
    wanted_barcodes = dict(data.barcodes)
    for existing in list(item.barcodes):
        if existing.barcode not in wanted_barcodes:
            item.barcodes.remove(existing)
        else:
            existing.unit_id = wanted_barcodes.pop(existing.barcode)
    item.barcodes.extend(ItemBarcode(barcode=b, unit_id=u) for b, u in wanted_barcodes.items())


def _snapshot(item: Item) -> dict:
    return {
        "code": item.code, "name": item.name, "base_unit_id": item.base_unit_id,
        "category_id": item.category_id,
        "reorder_point": str(item.reorder_point) if item.reorder_point is not None else None,
        "reorder_qty": str(item.reorder_qty) if item.reorder_qty is not None else None,
        "is_returnable": item.is_returnable, "description": item.description,
        "units": sorted((u.unit_id, str(u.factor)) for u in item.units),
        "barcodes": sorted(b.barcode for b in item.barcodes),
    }


# ----- commands -----


async def create_item_in(s: AsyncSession, actor: Actor, data: ItemInput) -> Item:
    """Create inside the caller's transaction (used by imports)."""
    actor.require(Perm.ITEMS_EDIT)
    data = await _validate(s, data, None)
    item = Item(code=data.code, name=data.name, name_normalized="", base_unit_id=0)
    _apply(item, data)
    s.add(item)
    await s.flush()
    await s.refresh(item, ["units", "barcodes"])
    audit.record(s, actor, "item.created", "item", item.id, _snapshot(item))
    return item


async def create_item(db: Database, actor: Actor, data: ItemInput) -> int:
    async with db.session(actor.user_id) as s:
        return (await create_item_in(s, actor, data)).id


async def update_item(
    db: Database, actor: Actor, item_id: int, expected_version: int, data: ItemInput
) -> None:
    actor.require(Perm.ITEMS_EDIT)
    async with db.session(actor.user_id) as s:
        item = await _load(s, item_id)
        if item.version_id != expected_version:
            raise ConcurrencyError()
        data = await _validate(s, data, item_id)
        if data.base_unit_id != item.base_unit_id and await _has_movements(s, item_id):
            raise ValidationError("واحد اصلی کالایی که گردش دارد قابل تغییر نیست.")
        before = _snapshot(item)
        _apply(item, data)
        # Unit/barcode-only edits touch child rows; force a version bump on the item itself.
        flag_modified(item, "description")
        await s.flush()
        after = _snapshot(item)
        changes = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
        if changes:
            audit.record(s, actor, "item.updated", "item", item.id, changes)


async def set_item_active(
    db: Database, actor: Actor, item_id: int, active: bool, approval: Approval | None = None
) -> None:
    """Reactivating needs items.edit; deactivating is a protected action."""
    actor.require(Perm.ITEMS_EDIT)
    approver_id = None if active else consume(approval, ProtectedAction.DEACTIVATE_ITEM, actor)
    async with db.session(actor.user_id) as s:
        item = await _load(s, item_id)
        item.is_active = active
        audit.record(s, actor, "item.activated" if active else "item.deactivated", "item",
                     item.id, {"code": item.code, "name": item.name}, approved_by_id=approver_id)


async def delete_item(db: Database, actor: Actor, item_id: int, approval: Approval | None) -> None:
    """Only items that never appeared in a document can be deleted; others are deactivated."""
    actor.require(Perm.ITEMS_EDIT)
    async with db.session(actor.user_id) as s:
        item = await _load(s, item_id)
        if await _has_movements(s, item_id):
            raise ValidationError("این کالا در اسناد انبار استفاده شده و قابل حذف نیست. "
                                  "به‌جای حذف، آن را غیرفعال کنید.")
        approver_id = consume(approval, ProtectedAction.DELETE_ITEM, actor)
        snapshot = _snapshot(item)
        await s.delete(item)
        audit.record(s, actor, "item.deleted", "item", item_id, snapshot,
                     approved_by_id=approver_id)


async def has_movements(db: Database, item_id: int) -> bool:
    async with db.session() as s:
        return await _has_movements(s, item_id)
