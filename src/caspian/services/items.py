"""Items: search, create/edit, units & barcodes, activation (protected) and deletion."""

import asyncio
import bisect
import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

from rapidfuzz import fuzz, process
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import normalize, to_ascii_digits
from caspian.db.database import Database
from caspian.db.models import (
    Category,
    DocType,
    DocumentLine,
    ImportLine,
    Item,
    ItemBarcode,
    ItemUnit,
    StockBalance,
    StockLedger,
    Stocktake,
    StocktakeLine,
    StocktakeStatus,
    Unit,
)
from caspian.services import audit, documents
from caspian.services.actor import Actor
from caspian.services.errors import ConcurrencyError, NotFound, SimilarItems, ValidationError
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
    on_hand: Decimal | None  # None when the viewer may not see stock (blind counters)
    reorder_point: Decimal | None
    is_active: bool
    is_returnable: bool

    @property
    def below_reorder(self) -> bool:
        return (self.reorder_point is not None and self.on_hand is not None
                and self.on_hand <= self.reorder_point)


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
    # Exact code match first, then by code (#29: items are listed by code by default).
    stmt = stmt.order_by((Item.code == raw).desc(), Item.code).limit(limit)
    if only_below_reorder:
        actor.require(Perm.STOCK_VIEW)
    show_stock = actor.can(Perm.STOCK_VIEW)
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    return [
        ItemRow(item.id, item.code, item.name, cat or "", unit,
                Decimal(q or 0) if show_stock else None,
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


# ----- one item per real product (QA round 2, feature A) -----

SIMILAR_SCORE = 85  # rapidfuzz ratio on normalized names; tuned in tests/test_similar_items.py
_NUMBERS = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class SimilarItem:
    id: int
    code: str
    name: str
    unit: str
    stock: Decimal
    score: int

    def label(self) -> str:
        return f"{self.code} – {self.name} – موجودی {format_qty(self.stock)} {self.unit}"


def similarity(a: str, b: str) -> int:
    """0–100 for two normalized names. Names whose numbers differ («پیچ ۴ در ۴۰» / «پیچ ۴ در ۵۰»,
    «لیتر ۱» / «لیتر ۲») are different products, never similar."""
    if a == b:
        return 100
    if _NUMBERS.findall(a) != _NUMBERS.findall(b):
        return 0
    return round(fuzz.ratio(a, b))


async def similar_items_in(s: AsyncSession, name: str, exclude_id: int | None = None,
                           limit: int = 5) -> list[SimilarItem]:
    """Active items whose name equals or closely resembles `name` (best first)."""
    key = normalize(" ".join(name.split()))
    if not key:
        return []
    rows = (await s.execute(select(Item.id, Item.name_normalized).where(Item.is_active))).all()
    names = {i: n for i, n in rows if i != exclude_id}
    hits = process.extract(key, names, scorer=fuzz.ratio, score_cutoff=SIMILAR_SCORE, limit=limit * 4)
    scored = sorted(((similarity(key, n), i) for n, _sc, i in hits), reverse=True)
    ids = [i for sc, i in scored if sc >= SIMILAR_SCORE][:limit]
    if not ids:
        return []
    on_hand = _on_hand_subquery()
    found = {row[0]: row for row in (await s.execute(
        select(Item.id, Item.code, Item.name, Unit.name, func.coalesce(on_hand.c.on_hand, 0))
        .join(Unit, Unit.id == Item.base_unit_id).outerjoin(on_hand, on_hand.c.item_id == Item.id)
        .where(Item.id.in_(ids)))).all()}
    score = {i: sc for sc, i in scored}
    return [SimilarItem(i, found[i][1], found[i][2], found[i][3], Decimal(found[i][4] or 0), score[i])
            for i in ids if i in found]


async def similar_items(db: Database, name: str, exclude_id: int | None = None,
                        limit: int = 5) -> list[SimilarItem]:
    async with db.session() as s:
        return await similar_items_in(s, name, exclude_id, limit)


async def _check_name(s: AsyncSession, name: str, exclude_id: int | None, allow_similar: bool) -> None:
    """Exact (normalized) duplicates are always refused; similar names need an admin approval."""
    found = await similar_items_in(s, name, exclude_id)
    exact = [f for f in found if f.score == 100]
    if exact:
        raise ValidationError("کالایی با همین نام وجود دارد: " + "؛ ".join(f.label() for f in exact)
                              + ". همان کالا را استفاده کنید.")
    if found and not allow_similar:
        raise SimilarItems("کالای مشابه وجود دارد: " + "؛ ".join(f.label() for f in found)
                           + ". یکی از آن‌ها را انتخاب کنید؛ ایجاد کالای جدید نیاز به تأیید مدیر دارد.",
                           found)


def similar_ok(approval: Approval | None, actor: Actor) -> int | None:
    """Consume a CREATE_SIMILAR_ITEM approval (never for the AI); returns the approver id."""
    return consume(approval, ProtectedAction.CREATE_SIMILAR_ITEM, actor) if approval is not None else None


_GROUPS_CACHE: dict[str, tuple[tuple, list[list[int]]]] = {}  # recomputed when active items change


def _group_ids(names: dict[int, str]) -> list[list[int]]:
    """Connected groups of similar names. Pure CPU work: run it in a thread.

    Names whose numbers differ are never similar, so only names with the same digit signature are
    compared, and ratio >= 85 needs similar lengths, so each name only meets a narrow length window.
    """
    buckets: dict[tuple, list[tuple[int, int, str]]] = {}
    for item_id, key in names.items():
        buckets.setdefault(tuple(_NUMBERS.findall(key)), []).append((len(key), item_id, key))
    parent = {i: i for i in names}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for bucket in buckets.values():
        bucket.sort()
        lengths = [b[0] for b in bucket]
        for n, (length, item_id, key) in enumerate(bucket):
            # ratio = 2*matches/(len_a+len_b) <= 2*len_a/(len_a+len_b): longer names can't reach 85.
            hi = bisect.bisect_right(lengths, length * (200 - SIMILAR_SCORE) // SIMILAR_SCORE)
            window = bucket[n + 1:hi]
            if not window:
                continue
            for _key, _score, k in process.extract(key, [w[2] for w in window], scorer=fuzz.ratio,
                                                   score_cutoff=SIMILAR_SCORE, limit=None):
                parent[root(window[k][1])] = root(item_id)
    groups: dict[int, list[int]] = {}
    for i in names:
        groups.setdefault(root(i), []).append(i)
    return [sorted(g) for g in groups.values() if len(g) > 1]


async def duplicate_groups(db: Database, actor: Actor) -> list[list[SimilarItem]]:
    """Groups of active items that are the same or similar products (for the health check and the
    Items filter). Data is never changed here; merge them with merge_items."""
    actor.require(Perm.ITEMS_VIEW)
    async with db.session() as s:
        fingerprint = tuple((await s.execute(select(func.count(Item.id), func.max(Item.updated_at),
                                                    func.sum(Item.id)).where(Item.is_active))).one())
        cached = _GROUPS_CACHE.get(str(db.url))
        if cached and cached[0] == fingerprint:
            groups = cached[1]
        else:
            rows = (await s.execute(select(Item.id, Item.name_normalized).where(Item.is_active))).all()
            groups = None
    if groups is None:
        groups = await asyncio.to_thread(_group_ids, dict(rows))  # never blocks the window
        _GROUPS_CACHE[str(db.url)] = (fingerprint, groups)
    if not groups:
        return []
    ids = [i for g in groups for i in g]
    on_hand = _on_hand_subquery()
    async with db.session() as s:
        info = {r[0]: r for r in (await s.execute(
            select(Item.id, Item.code, Item.name, Unit.name, func.coalesce(on_hand.c.on_hand, 0))
            .join(Unit, Unit.id == Item.base_unit_id).outerjoin(on_hand, on_hand.c.item_id == Item.id)
            .where(Item.id.in_(ids)))).all()}
    out = [[SimilarItem(i, info[i][1], info[i][2], info[i][3], Decimal(info[i][4] or 0), 100)
            for i in g] for g in groups]
    return sorted((sorted(g, key=lambda x: x.code) for g in out), key=lambda g: g[0].code)


@dataclass(frozen=True)
class MergeResult:
    target_code: str
    merged_codes: list[str]
    moved_lines: int


async def merge_items(db: Database, actor: Actor, target_id: int, source_ids: list[int],
                      approval: Approval | None, reason: str = "") -> MergeResult:
    """Combine duplicates into one item: documents, ledger, balances, barcodes and units of the
    sources move to the target; the sources are deactivated (codes kept). History is preserved
    (re-pointed, audited with the full mapping); a backup restores the old state if ever needed."""
    actor.require_human("ادغام کالا")
    actor.require(Perm.ITEMS_MERGE)
    sources = [i for i in dict.fromkeys(source_ids) if i != target_id]
    if not sources:
        raise ValidationError("کالای دیگری برای ادغام انتخاب نشده است.")
    if not reason.strip():
        raise ValidationError("علت ادغام را بنویسید.")
    approver_id = consume(approval, ProtectedAction.MERGE_ITEMS, actor)
    async with db.session(actor.user_id) as s:
        target = await _load(s, target_id)
        items_ = [await _load(s, i) for i in sources]
        target_units = {u.unit_id: u.factor for u in target.units}
        for src in items_:
            if src.base_unit_id != target.base_unit_id:
                raise ValidationError(f"واحد اصلی «{src.name}» با «{target.name}» یکسان نیست؛ "
                                      "ادغام ممکن نیست.")
            for u in src.units:
                if u.unit_id in target_units and target_units[u.unit_id] != u.factor:
                    raise ValidationError(f"ضریب واحد «{u.unit.name}» در «{src.name}» با کالای مقصد "
                                          "فرق دارد.")
        busy = await s.scalar(select(Stocktake.number).join(StocktakeLine).where(
            Stocktake.status.in_([StocktakeStatus.OPEN, StocktakeStatus.COUNTED]),
            StocktakeLine.item_id.in_([target_id, *sources])))
        if busy:
            raise ValidationError(f"این کالاها در انبارگردانی باز شماره {busy} هستند؛ پس از آن "
                                  "ادغام کنید.")
        moved = 0
        mapping = {}
        for src in items_:
            lines = (await s.execute(update(DocumentLine).where(DocumentLine.item_id == src.id)
                                     .values(item_id=target.id))).rowcount or 0
            await s.execute(update(StockLedger).where(StockLedger.item_id == src.id)
                            .values(item_id=target.id))
            await s.execute(update(ImportLine).where(ImportLine.match_item_id == src.id)
                            .values(match_item_id=target.id))
            for bal in (await s.scalars(select(StockBalance).where(StockBalance.item_id == src.id))).all():
                into = await s.get(StockBalance, (target.id, bal.warehouse_id))
                if into is None:
                    s.add(StockBalance(item_id=target.id, warehouse_id=bal.warehouse_id, qty=bal.qty))
                else:
                    into.qty += bal.qty
                await s.delete(bal)
            for barcode in list(src.barcodes):
                src.barcodes.remove(barcode)
                await s.flush()
                target.barcodes.append(ItemBarcode(barcode=barcode.barcode, unit_id=barcode.unit_id))
            for u in list(src.units):
                if u.unit_id not in target_units:
                    target.units.append(ItemUnit(unit_id=u.unit_id, factor=u.factor))
                    target_units[u.unit_id] = u.factor
            src.is_active = False
            src.name = f"{src.name} (ادغام‌شده در {target.code})"[:255]
            src.name_normalized = normalize(src.name)
            flag_modified(src, "description")
            mapping[src.code] = {"document_lines": lines}
            moved += lines
        flag_modified(target, "description")
        audit.record(s, actor, "item.merged", "item", target.id,
                     {"target": target.code, "sources": mapping, "reason": reason.strip()},
                     approved_by_id=approver_id)
        return MergeResult(target.code, [i.code for i in items_], moved)


async def create_item_in(s: AsyncSession, actor: Actor, data: ItemInput,
                         allow_similar: bool = False) -> Item:
    """Create inside the caller's transaction (used by imports). `allow_similar` only after a
    consumed CREATE_SIMILAR_ITEM approval (see similar_ok)."""
    actor.require(Perm.ITEMS_EDIT)
    data = await _validate(s, data, None)
    await _check_name(s, data.name, None, allow_similar)
    item = Item(code=data.code, name=data.name, name_normalized="", base_unit_id=0)
    _apply(item, data)
    s.add(item)
    await s.flush()
    await s.refresh(item, ["units", "barcodes"])
    audit.record(s, actor, "item.created", "item", item.id, _snapshot(item))
    return item


async def same_name_items(db: Database, name: str, exclude_id: int | None = None) -> list[tuple[str, str]]:
    """(code, name) of active items whose normalized name equals `name` (#13: warn, don't block)."""
    key = normalize(" ".join(name.split()))
    if not key:
        return []
    stmt = select(Item.code, Item.name).where(Item.is_active, Item.name_normalized == key)
    if exclude_id is not None:
        stmt = stmt.where(Item.id != exclude_id)
    async with db.session() as s:
        return [(c, n) for c, n in (await s.execute(stmt)).all()]


async def create_item(db: Database, actor: Actor, data: ItemInput,
                      similar_approval: Approval | None = None) -> int:
    approver = similar_ok(similar_approval, actor)
    async with db.session(actor.user_id) as s:
        item = await create_item_in(s, actor, data, allow_similar=approver is not None)
        if approver is not None:
            audit.record(s, actor, "item.similar_created", "item", item.id, {"name": item.name},
                         approved_by_id=approver)
        return item.id


@dataclass(frozen=True)
class OpeningStock:
    """Quantity (in the base unit) already on the shelf when the item is defined."""

    warehouse_id: int
    qty: Decimal
    unit_price: Decimal | None = None


@dataclass(frozen=True)
class CreatedItem:
    item_id: int
    document_id: int | None  # the OPENING document, if any
    posted: bool


async def create_item_with_opening(db: Database, actor: Actor, data: ItemInput,
                                   opening: OpeningStock | None,
                                   similar_approval: Approval | None = None) -> CreatedItem:
    """New item plus its opening stock (#12). Stock only ever changes through documents: this
    creates an OPENING document and posts it when the user may post (else it stays a draft).
    Item and document are saved together or not at all."""
    approver = similar_ok(similar_approval, actor)
    async with db.session(actor.user_id) as s:
        item = await create_item_in(s, actor, data, allow_similar=approver is not None)
        if approver is not None:
            audit.record(s, actor, "item.similar_created", "item", item.id, {"name": item.name},
                         approved_by_id=approver)
        if opening is None:
            return CreatedItem(item.id, None, False)
        doc_id, posted = await _opening_document_in(s, actor, item, opening)
        return CreatedItem(item.id, doc_id, posted)


async def _opening_document_in(s: AsyncSession, actor: Actor, item: Item,
                               opening: OpeningStock) -> tuple[int, bool]:
    """The explicit opening-stock path: an OPENING document, posted when the user may post."""
    doc = await documents.create_document_in(s, actor, documents.DocumentInput(
        DocType.OPENING, dt.date.today(), opening.warehouse_id,
        [documents.LineInput(item.id, item.base_unit_id, opening.qty, opening.unit_price,
                             "موجودی اولیه هنگام تعریف کالا")],
        description=f"موجودی اولیه «{item.name}»"))
    posted = actor.can(Perm.DOCUMENTS_POST)
    if posted:
        await documents.post_document_in(s, actor, doc)
    return doc.id, posted


async def _update_item_in(s: AsyncSession, actor: Actor, item_id: int, expected_version: int,
                          data: ItemInput, similar_approval: Approval | None) -> Item:
    actor.require(Perm.ITEMS_EDIT)
    item = await _load(s, item_id)
    if item.version_id != expected_version:
        raise ConcurrencyError()
    data = await _validate(s, data, item_id)
    if normalize(data.name) != item.name_normalized:  # a rename must not create a duplicate
        await _check_name(s, data.name, item_id, similar_ok(similar_approval, actor) is not None)
    if data.base_unit_id != item.base_unit_id and await _has_movements(s, item_id):
        raise ValidationError("واحد اصلی کالایی که گردش دارد قابل تغییر نیست.")
    if data.code != item.code and await _has_movements(s, item_id):  # #23
        raise ValidationError("کد کالایی که در سندی استفاده شده قابل تغییر نیست.")
    before = _snapshot(item)
    _apply(item, data)
    # Unit/barcode-only edits touch child rows; force a version bump on the item itself.
    flag_modified(item, "description")
    await s.flush()
    after = _snapshot(item)
    changes = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if changes:
        audit.record(s, actor, "item.updated", "item", item.id, changes)
    return item


async def update_item(
    db: Database, actor: Actor, item_id: int, expected_version: int, data: ItemInput,
    similar_approval: Approval | None = None,
) -> None:
    async with db.session(actor.user_id) as s:
        await _update_item_in(s, actor, item_id, expected_version, data, similar_approval)


async def update_item_with_opening(db: Database, actor: Actor, item_id: int, expected_version: int,
                                   data: ItemInput, opening: OpeningStock | None,
                                   similar_approval: Approval | None = None) -> CreatedItem:
    """Edit an existing item and, only if the user explicitly entered one, add its opening stock
    through an OPENING document — together or not at all (the New Item dialog after the user picked
    an existing similar item)."""
    async with db.session(actor.user_id) as s:
        item = await _update_item_in(s, actor, item_id, expected_version, data, similar_approval)
        if opening is None:
            return CreatedItem(item.id, None, False)
        doc_id, posted = await _opening_document_in(s, actor, item, opening)
        return CreatedItem(item.id, doc_id, posted)


async def set_reorder_points(db: Database, actor: Actor, values: dict[int, Decimal | None]) -> int:
    """Bulk-update reorder points (e.g. accepting burn-rate suggestions). Returns count changed."""
    actor.require(Perm.ITEMS_EDIT)
    changed = 0
    async with db.session(actor.user_id) as s:
        for item_id, value in values.items():
            if value is not None and value < 0:
                raise ValidationError("نقطه سفارش نمی‌تواند منفی باشد.")
            item = await _load(s, item_id)
            if item.reorder_point != value:
                audit.record(s, actor, "item.updated", "item", item.id,
                             {"reorder_point": [str(item.reorder_point), str(value)]})
                item.reorder_point = value
                changed += 1
    return changed


async def set_item_active(
    db: Database, actor: Actor, item_id: int, active: bool, approval: Approval | None = None
) -> None:
    """Reactivating needs items.edit; deactivating is a protected action."""
    actor.require_human("فعال/غیرفعال کردن کالا")
    actor.require(Perm.ITEMS_EDIT)
    approver_id = None if active else consume(approval, ProtectedAction.DEACTIVATE_ITEM, actor)
    async with db.session(actor.user_id) as s:
        item = await _load(s, item_id)
        item.is_active = active
        audit.record(s, actor, "item.activated" if active else "item.deactivated", "item",
                     item.id, {"code": item.code, "name": item.name}, approved_by_id=approver_id)


async def delete_item(db: Database, actor: Actor, item_id: int, approval: Approval | None) -> None:
    """Only items that never appeared in a document can be deleted; others are deactivated."""
    actor.require_human("حذف کالا")
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
