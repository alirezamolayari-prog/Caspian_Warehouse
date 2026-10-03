"""Reference data: categories, units, warehouses, persons."""

import re
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.core.permissions import Perm
from caspian.core.text import normalize, to_ascii_digits
from caspian.db.database import Database
from caspian.db.models import (
    Category,
    Item,
    ItemBarcode,
    ItemUnit,
    Person,
    PersonKind,
    StockBalance,
    Unit,
    Warehouse,
)
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import ConcurrencyError, NotFound, ValidationError

PERSON_KIND_NAMES = {
    PersonKind.SUPPLIER: "تأمین‌کننده",
    PersonKind.CUSTOMER: "مشتری",
    PersonKind.EMPLOYEE: "کارمند",
    PersonKind.OTHER: "سایر",
}


def _clean(text: str) -> str:
    return " ".join(text.split())


async def _get(s: AsyncSession, model, obj_id: int, label: str):
    obj = await s.get(model, obj_id)
    if obj is None:
        raise NotFound(f"{label} پیدا نشد.")
    return obj


# ----- categories -----


@dataclass(frozen=True)
class CategoryRow:
    id: int
    name: str
    parent_id: int | None
    item_count: int


async def list_categories(db: Database) -> list[CategoryRow]:
    async with db.session() as s:
        counts = (
            select(Item.category_id, func.count().label("n")).group_by(Item.category_id).subquery()
        )
        rows = (await s.execute(
            select(Category, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.category_id == Category.id)
            .order_by(Category.name)
        )).all()
    return [CategoryRow(c.id, c.name, c.parent_id, n) for c, n in rows]


async def save_category(db: Database, actor: Actor, name: str, category_id: int | None = None,
                        parent_id: int | None = None) -> int:
    actor.require(Perm.ITEMS_EDIT)
    name = _clean(name)
    if not name:
        raise ValidationError("نام گروه الزامی است.")
    async with db.session(actor.user_id) as s:
        clash = await s.scalar(select(Category.id).where(
            func.lower(Category.name) == name.lower(), Category.id != (category_id or -1)))
        if clash:
            raise ValidationError("گروهی با این نام وجود دارد.")
        if category_id is None:
            cat = Category(name=name, parent_id=parent_id)
            s.add(cat)
            await s.flush()
            audit.record(s, actor, "category.created", "category", cat.id, {"name": name})
        else:
            cat = await _get(s, Category, category_id, "گروه")
            if parent_id == category_id:
                raise ValidationError("گروه نمی‌تواند زیرگروه خودش باشد.")
            audit.record(s, actor, "category.updated", "category", cat.id,
                         {"name": [cat.name, name]})
            cat.name, cat.parent_id = name, parent_id
        return cat.id


async def delete_category(db: Database, actor: Actor, category_id: int) -> None:
    actor.require_human("حذف گروه")
    actor.require(Perm.ITEMS_EDIT)
    async with db.session(actor.user_id) as s:
        cat = await _get(s, Category, category_id, "گروه")
        if await s.scalar(select(Item.id).where(Item.category_id == category_id).limit(1)):
            raise ValidationError("این گروه کالا دارد و قابل حذف نیست.")
        if await s.scalar(select(Category.id).where(Category.parent_id == category_id).limit(1)):
            raise ValidationError("این گروه زیرگروه دارد و قابل حذف نیست.")
        audit.record(s, actor, "category.deleted", "category", cat.id, {"name": cat.name})
        await s.delete(cat)


# ----- units -----


@dataclass(frozen=True)
class UnitRow:
    id: int
    name: str
    is_active: bool
    allow_decimal: bool = True


async def list_units(db: Database, include_inactive: bool = False) -> list[UnitRow]:
    async with db.session() as s:
        stmt = select(Unit).order_by(Unit.id)
        if not include_inactive:
            stmt = stmt.where(Unit.is_active)
        return [UnitRow(u.id, u.name, u.is_active, u.allow_decimal) for u in (await s.scalars(stmt)).all()]


async def save_unit(db: Database, actor: Actor, name: str, unit_id: int | None = None,
                    allow_decimal: bool | None = None) -> int:
    """`allow_decimal`: None keeps the current setting (new units allow decimals)."""
    actor.require(Perm.ITEMS_EDIT)
    name = _clean(name)
    if not name:
        raise ValidationError("نام واحد الزامی است.")
    async with db.session(actor.user_id) as s:
        if await s.scalar(select(Unit.id).where(Unit.name == name, Unit.id != (unit_id or -1))):
            raise ValidationError("واحدی با این نام وجود دارد.")
        if unit_id is None:
            unit = Unit(name=name, allow_decimal=True if allow_decimal is None else allow_decimal)
            s.add(unit)
            await s.flush()
            audit.record(s, actor, "unit.created", "unit", unit.id,
                         {"name": name, "allow_decimal": unit.allow_decimal})
        else:
            unit = await _get(s, Unit, unit_id, "واحد")
            audit.record(s, actor, "unit.updated", "unit", unit.id, {
                "name": [unit.name, name],
                "allow_decimal": [unit.allow_decimal,
                                  unit.allow_decimal if allow_decimal is None else allow_decimal]})
            unit.name = name
            if allow_decimal is not None:
                unit.allow_decimal = allow_decimal
        return unit.id


async def set_unit_active(db: Database, actor: Actor, unit_id: int, active: bool) -> None:
    actor.require_human("غیرفعال کردن واحد")
    actor.require(Perm.ITEMS_EDIT)
    async with db.session(actor.user_id) as s:
        unit = await _get(s, Unit, unit_id, "واحد")
        if not active:
            used = await s.scalar(select(Item.id).where(Item.base_unit_id == unit_id).limit(1)) \
                or await s.scalar(select(ItemUnit.id).where(ItemUnit.unit_id == unit_id).limit(1)) \
                or await s.scalar(select(ItemBarcode.id).where(ItemBarcode.unit_id == unit_id)
                                  .limit(1))
            if used:
                raise ValidationError("این واحد برای کالاها استفاده شده و قابل غیرفعال‌سازی نیست.")
        unit.is_active = active
        audit.record(s, actor, "unit.activated" if active else "unit.deactivated", "unit",
                     unit.id)


# ----- warehouses -----


@dataclass(frozen=True)
class WarehouseRow:
    id: int
    code: str
    name: str
    is_active: bool
    notes: str


async def list_warehouses(db: Database, include_inactive: bool = False) -> list[WarehouseRow]:
    async with db.session() as s:
        stmt = select(Warehouse).order_by(Warehouse.code)
        if not include_inactive:
            stmt = stmt.where(Warehouse.is_active)
        return [WarehouseRow(w.id, w.code, w.name, w.is_active, w.notes)
                for w in (await s.scalars(stmt)).all()]


async def save_warehouse(db: Database, actor: Actor, code: str, name: str, notes: str = "",
                         warehouse_id: int | None = None) -> int:
    actor.require(Perm.WAREHOUSES_EDIT)
    code, name = to_ascii_digits(code.strip()), _clean(name)
    if not code or not name:
        raise ValidationError("کد و نام انبار الزامی است.")
    async with db.session(actor.user_id) as s:
        if await s.scalar(select(Warehouse.id).where(Warehouse.code == code,
                                                     Warehouse.id != (warehouse_id or -1))):
            raise ValidationError("انباری با این کد وجود دارد.")
        if warehouse_id is None:
            wh = Warehouse(code=code, name=name, notes=notes.strip())
            s.add(wh)
            await s.flush()
            audit.record(s, actor, "warehouse.created", "warehouse", wh.id,
                         {"code": code, "name": name})
        else:
            wh = await _get(s, Warehouse, warehouse_id, "انبار")
            audit.record(s, actor, "warehouse.updated", "warehouse", wh.id,
                         {"code": [wh.code, code], "name": [wh.name, name]})
            wh.code, wh.name, wh.notes = code, name, notes.strip()
        return wh.id


async def set_warehouse_active(db: Database, actor: Actor, warehouse_id: int, active: bool) -> None:
    actor.require_human("غیرفعال کردن انبار")
    actor.require(Perm.WAREHOUSES_EDIT)
    async with db.session(actor.user_id) as s:
        wh = await _get(s, Warehouse, warehouse_id, "انبار")
        if not active:
            others = await s.scalar(select(func.count()).select_from(Warehouse).where(
                Warehouse.is_active, Warehouse.id != warehouse_id))
            if not others:
                raise ValidationError("حداقل یک انبار فعال باید باقی بماند.")
            stock = await s.scalar(select(func.sum(StockBalance.qty)).where(
                StockBalance.warehouse_id == warehouse_id))
            if stock:
                raise ValidationError("این انبار موجودی دارد. ابتدا موجودی را منتقل کنید.")
        wh.is_active = active
        audit.record(s, actor, "warehouse.activated" if active else "warehouse.deactivated",
                     "warehouse", wh.id)


# ----- persons -----


@dataclass(frozen=True)
class PersonRow:
    id: int
    version_id: int
    code: str
    name: str
    kind: PersonKind
    phone: str
    address: str
    is_active: bool

    @property
    def kind_name(self) -> str:
        return PERSON_KIND_NAMES[self.kind]


def _person_row(p: Person) -> PersonRow:
    return PersonRow(p.id, p.version_id, p.code, p.name, p.kind, p.phone, p.address, p.is_active)


async def search_persons(db: Database, query: str = "", kind: PersonKind | None = None,
                         include_inactive: bool = False, limit: int = 500) -> list[PersonRow]:
    stmt = select(Person).order_by(Person.name).limit(limit)
    words = normalize(query).split()
    if words:
        raw = to_ascii_digits(query.strip())
        stmt = stmt.where(or_(
            *[Person.name_normalized.contains(w, autoescape=True) for w in words[:1]],
            Person.code == raw,
            Person.phone.contains(raw, autoescape=True),
        ))
        for w in words[1:]:
            stmt = stmt.where(Person.name_normalized.contains(w, autoescape=True))
    if kind is not None:
        stmt = stmt.where(Person.kind == kind)
    if not include_inactive:
        stmt = stmt.where(Person.is_active)
    async with db.session() as s:
        return [_person_row(p) for p in (await s.scalars(stmt)).all()]


async def _next_person_code(s: AsyncSession) -> str:
    codes = (await s.scalars(select(Person.code))).all()
    return str(max((int(c) for c in codes if c.isdigit()), default=100) + 1)


_PHONE = re.compile(r"^\+?[\d\s()-]+$")


def clean_phone(phone: str) -> str:
    """Digits (Persian or Latin), an optional leading +, spaces, dashes, parentheses (#14)."""
    phone = " ".join(to_ascii_digits(phone or "").split())
    if not phone:
        return ""
    digits = sum(c.isdigit() for c in phone)
    if not _PHONE.match(phone) or not 4 <= digits <= 20:
        raise ValidationError("شماره تلفن نامعتبر است؛ فقط رقم، + در ابتدا، فاصله، خط تیره و پرانتز "
                              "مجاز است.")
    return phone


async def save_person(
    db: Database, actor: Actor, name: str, kind: PersonKind, phone: str = "", address: str = "",
    code: str = "", person_id: int | None = None, expected_version: int | None = None,
) -> int:
    actor.require(Perm.PERSONS_EDIT)
    name = _clean(name)
    if not name:
        raise ValidationError("نام شخص الزامی است.")
    code = to_ascii_digits(code.strip())
    phone = clean_phone(phone)
    async with db.session(actor.user_id) as s:
        if code and await s.scalar(select(Person.id).where(Person.code == code,
                                                           Person.id != (person_id or -1))):
            raise ValidationError("شخصی با این کد وجود دارد.")
        if person_id is None:
            person = Person(code=code or await _next_person_code(s), name=name,
                            name_normalized=normalize(name), kind=kind, phone=phone,
                            address=address.strip())
            s.add(person)
            await s.flush()
            audit.record(s, actor, "person.created", "person", person.id,
                         {"code": person.code, "name": name})
        else:
            person = await _get(s, Person, person_id, "شخص")
            if expected_version is not None and person.version_id != expected_version:
                raise ConcurrencyError()
            audit.record(s, actor, "person.updated", "person", person.id, {"name": name})
            person.code = code or person.code
            person.name, person.name_normalized = name, normalize(name)
            person.kind, person.phone, person.address = kind, phone, address.strip()
        return person.id


async def set_person_active(db: Database, actor: Actor, person_id: int, active: bool) -> None:
    actor.require_human("غیرفعال کردن شخص")
    actor.require(Perm.PERSONS_EDIT)
    async with db.session(actor.user_id) as s:
        person = await _get(s, Person, person_id, "شخص")
        person.is_active = active
        audit.record(s, actor, "person.activated" if active else "person.deactivated",
                     "person", person.id)

