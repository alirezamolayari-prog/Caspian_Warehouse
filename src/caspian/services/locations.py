"""Consumption locations (محل مصرف) — docs/specs/caspian_consumption_location_spec.md, Part 2.

One entry fills the place of an issue voucher: typing a plate fills its floor; stairs, elevators
and «مشاعات عمومی طبقه» need a floor chosen by the user. Codes are the cross-system keys.
Locations are never deleted, only deactivated; documents store their own floor so later edits
never rewrite history.
"""

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.core.permissions import Perm
from caspian.core.text import normalize_location, to_persian_digits
from caspian.db import location_seed
from caspian.db.database import Database
from caspian.db.models import AppSetting, Floor, Location, LocationAlias, LocationType
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import NotFound, ValidationError

PHASE1_MESSAGE = "این پلاک مربوط به فاز ۱ است"
FLOOR_REQUIRED = "برای این محل، طبقه را انتخاب کنید."
TYPE_NAMES = {
    LocationType.PLATE: "پلاک",
    LocationType.NAMED: "مکان ثابت",
    LocationType.VERTICAL: "پله / آسانسور",
    LocationType.GENERAL: "مشاعات طبقه",
    LocationType.FLOOR_PLACE: "پارکینگ / بام",
}
_PLATE_WORD = normalize_location("پلاک")
_DIGITS_AFTER_LATIN = re.compile(r"(?<![A-Za-z])\d+")


def fa_digits(text: str) -> str:
    """Persian digits for display, except inside Latin codes: «پلاک ۶۸» but «پارکینگ P1»."""
    return _DIGITS_AFTER_LATIN.sub(lambda m: to_persian_digits(m.group()), text)


class Phase1Plate(ValidationError):
    def __init__(self) -> None:
        super().__init__(PHASE1_MESSAGE)


@dataclass(frozen=True)
class FloorRow:
    id: int
    code: str
    name_fa: str
    sort_order: int
    is_active: bool

    @property
    def display(self) -> str:
        """One style everywhere (QA Part A #5): every storey says «طبقه …» («طبقه همکف»,
        «طبقه زیرهمکف», «طبقه دوم», «طبقه بام»); parking levels keep their own name («پارکینگ P1»)."""
        return self.name_fa if self.code.startswith("P") else f"طبقه {self.name_fa}"


@dataclass(frozen=True)
class LocationRow:
    id: int
    code: str
    name_fa: str
    type: LocationType
    floor: FloorRow | None
    needs_floor: bool
    merged_group: str | None
    is_active: bool
    note: str
    aliases: tuple[str, ...] = ()
    keys: frozenset[str] = field(default=frozenset(), compare=False)

    def label(self, floor: FloorRow | None = None) -> str:
        """«پلاک ۶۸ · همکف», «آسانسور باری اسدنژاد · طبقه دوم», «پارکینگ P1»."""
        name = fa_digits(self.name_fa)
        if self.type == LocationType.FLOOR_PLACE:
            return name
        floor = floor or self.floor
        return f"{name} · {floor.display}" if floor else name


def place_label(location_name: str, location_type: LocationType | str | None, floor_code: str | None,
                floor_name: str | None) -> str:
    """Same text as LocationRow.label for rows read straight from documents/reports."""
    if not location_name:
        return ""
    name = fa_digits(location_name)
    if str(location_type) in (LocationType.FLOOR_PLACE, "FLOOR_PLACE") or not floor_name:
        return name
    floor = FloorRow(0, floor_code or "", floor_name, 0, True)
    return f"{name} · {floor.display}"


def _floor_row(f: Floor | None) -> FloorRow | None:
    return FloorRow(f.id, f.code, f.name_fa, f.sort_order, f.is_active) if f else None


def _location_row(loc: Location) -> LocationRow:
    aliases = tuple(a.alias for a in loc.aliases)
    keys = {normalize_location(loc.code), normalize_location(loc.name_fa)}
    keys.update(a.alias_normalized for a in loc.aliases)
    if loc.type == LocationType.PLATE:
        keys.add(_PLATE_WORD + normalize_location(loc.code))
    return LocationRow(loc.id, loc.code, loc.name_fa, loc.type, _floor_row(loc.floor), loc.needs_floor,
                       loc.merged_group, loc.is_active, loc.note, aliases, frozenset(k for k in keys if k))


# ----- settings -----


@dataclass(frozen=True)
class LocationSettings:
    version: int
    phase1_plates: str
    required_on_issue: bool


def parse_ranges(text: str) -> set[int]:
    """«1-6,46-57,83-92» → {1..6, 46..57, 83..92}. Bad parts are ignored."""
    numbers: set[int] = set()
    for part in normalize_location(text).split(","):
        bounds = part.split("/") if "/" in part else part.split("-")
        try:
            low, high = int(bounds[0]), int(bounds[-1])
        except ValueError:
            continue
        numbers.update(range(min(low, high), max(low, high) + 1))
    return numbers


async def _settings_in(s: AsyncSession) -> AppSetting:
    row = await s.get(AppSetting, location_seed.SETTINGS_KEY)
    if row is None:
        row = AppSetting(key=location_seed.SETTINGS_KEY, value=dict(location_seed.DEFAULT_SETTINGS))
        s.add(row)
    return row


def _settings_value(value: dict | None) -> LocationSettings:
    value = value or {}
    return LocationSettings(int(value.get("version", 1)),
                            str(value.get("phase1_plates", location_seed.DEFAULT_SETTINGS["phase1_plates"])),
                            bool(value.get("required_on_issue", True)))


async def read_settings(db: Database) -> LocationSettings:
    async with db.session() as s:
        row = await s.get(AppSetting, location_seed.SETTINGS_KEY)
        return _settings_value(row.value if row else None)


async def required_on_issue_in(s: AsyncSession) -> bool:
    row = await s.get(AppSetting, location_seed.SETTINGS_KEY)
    return _settings_value(row.value if row else None).required_on_issue


def _bump_version(row: AppSetting) -> int:
    value = dict(row.value or {})
    value["version"] = int(value.get("version", 1)) + 1
    row.value = value
    return value["version"]


async def save_settings(db: Database, actor: Actor, phase1_plates: str, required_on_issue: bool) -> None:
    actor.require(Perm.LOCATIONS_MANAGE)
    actor.require_human("تغییر تنظیمات محل مصرف")
    async with db.session(actor.user_id) as s:
        row = await _settings_in(s)
        value = dict(row.value or {})
        value.update(phase1_plates=phase1_plates.strip(), required_on_issue=required_on_issue)
        row.value = value
        audit.record(s, actor, "locations.settings", details={
            "phase1_plates": phase1_plates.strip(), "required_on_issue": required_on_issue})


# ----- the matching index (used by LocationPicker; ~150 rows, searched in memory) -----


@dataclass
class LocationIndex:
    locations: list[LocationRow]
    floors: list[FloorRow]
    phase1: set[int]

    def find(self, code: str) -> LocationRow | None:
        return next((loc for loc in self.locations if loc.code == code), None)

    def _phase1_check(self, key: str) -> None:
        number = key.removeprefix(_PLATE_WORD).split("/")[0]
        if number.isdigit() and int(number) in self.phase1 and self.exact_or_none(key) is None:
            raise Phase1Plate()

    def exact_or_none(self, key: str) -> LocationRow | None:
        hits = [loc for loc in self.locations if loc.is_active and key in loc.keys]
        return hits[0] if len(hits) == 1 else None

    def exact(self, text: str) -> LocationRow | None:
        """The one active location the input names exactly (a plate number, a code, a name or an
        alias), else None. Raises Phase1Plate for a Phase 1 plate."""
        key = normalize_location(text)
        if not key:
            return None
        plate = key.removeprefix(_PLATE_WORD)
        for loc in self.locations:
            if loc.is_active and loc.type == LocationType.PLATE and normalize_location(loc.code) == plate:
                return loc
        self._phase1_check(key)
        return self.exact_or_none(key)

    def search(self, text: str, limit: int = 30) -> list[LocationRow]:
        """Active locations matching code, name or alias: exact, then prefix, then contains."""
        key = normalize_location(text)
        active = [loc for loc in self.locations if loc.is_active]
        if not key:
            return active[:limit]
        self._phase1_check(key)
        ranked = []
        for loc in active:
            if key in loc.keys:
                rank = 0
            elif any(k.startswith(key) for k in loc.keys):
                rank = 1
            elif any(key in k for k in loc.keys):
                rank = 2
            else:
                continue
            ranked.append((rank, loc))
        ranked.sort(key=lambda r: r[0])
        return [loc for _rank, loc in ranked[:limit]]


async def _all_locations(s: AsyncSession) -> list[Location]:
    return list((await s.scalars(select(Location).order_by(Location.id))).unique().all())


async def _all_floors(s: AsyncSession) -> list[Floor]:
    return list((await s.scalars(select(Floor).order_by(Floor.sort_order))).all())


async def load_index(db: Database) -> LocationIndex:
    async with db.session() as s:
        locations = [_location_row(loc) for loc in await _all_locations(s)]
        floors = [_floor_row(f) for f in await _all_floors(s)]
        settings = await s.get(AppSetting, location_seed.SETTINGS_KEY)
    return LocationIndex(locations, floors, parse_ranges(_settings_value(
        settings.value if settings else None).phase1_plates))


async def list_floors(db: Database, include_inactive: bool = False) -> list[FloorRow]:
    async with db.session() as s:
        return [_floor_row(f) for f in await _all_floors(s) if include_inactive or f.is_active]


async def list_locations(db: Database, query: str = "", location_type: LocationType | None = None,
                         floor_id: int | None = None, include_inactive: bool = True) -> list[LocationRow]:
    index = await load_index(db)
    rows = index.search(query, limit=10_000) if query.strip() else index.locations
    if include_inactive and query.strip():  # search() skips inactive ones; the admin list shows them
        key = normalize_location(query)
        rows += [loc for loc in index.locations if not loc.is_active and any(key in k for k in loc.keys)]
    return [loc for loc in rows
            if (include_inactive or loc.is_active)
            and (location_type is None or loc.type == location_type)
            and (floor_id is None or (loc.floor and loc.floor.id == floor_id))]


# ----- place on a document -----


@dataclass(frozen=True)
class Place:
    location: Location
    floor: Floor


async def resolve_place_in(s: AsyncSession, location_id: int, floor_id: int | None,
                           allow_inactive: bool = False) -> Place:
    """The (location, floor) pair a document stores. Fixed-floor locations always use their own
    floor; stairs/elevators/«مشاعات» need one chosen by the user (spec §2.6.4)."""
    location = await s.get(Location, location_id)
    if location is None:
        raise NotFound("محل مصرف پیدا نشد.")
    if not location.is_active and not allow_inactive:
        raise ValidationError(f"محل مصرف «{location.name_fa}» غیرفعال است.")
    if location.needs_floor:
        if floor_id is None:
            raise ValidationError(FLOOR_REQUIRED)
        floor = await s.get(Floor, floor_id)
        if floor is None or (not floor.is_active and not allow_inactive):
            raise ValidationError("طبقه انتخاب‌شده معتبر نیست.")
    else:
        floor = location.floor
        if floor is None:
            raise ValidationError(f"طبقه محل «{location.name_fa}» تعریف نشده است.")
    return Place(location, floor)


# ----- admin: add / edit / deactivate (spec §2.7) -----


@dataclass
class LocationInput:
    code: str
    name_fa: str
    type: LocationType
    floor_id: int | None = None
    aliases: list[str] = field(default_factory=list)
    merged_group: str = ""
    note: str = ""


async def _alias_owner(s: AsyncSession, key: str, exclude_id: int | None) -> Location | None:
    stmt = (select(Location).join(LocationAlias, LocationAlias.location_id == Location.id)
            .where(LocationAlias.alias_normalized == key))
    if exclude_id is not None:
        stmt = stmt.where(Location.id != exclude_id)
    return (await s.scalars(stmt.limit(1))).first()


async def _apply_input(s: AsyncSession, loc: Location, data: LocationInput) -> None:
    code, name = data.code.strip(), " ".join(data.name_fa.split())
    if not code or not name:
        raise ValidationError("کد و نام محل را وارد کنید.")
    other = (await s.scalars(select(Location).where(Location.code == code))).first()
    if other is not None and other.id != loc.id:
        raise ValidationError(f"کد «{code}» برای «{other.name_fa}» استفاده شده است.")
    needs_floor = data.type in (LocationType.VERTICAL, LocationType.GENERAL)
    if needs_floor:
        floor_id = None
    else:
        if data.floor_id is None:
            raise ValidationError("طبقه این محل را انتخاب کنید.")
        floor_id = data.floor_id
    keys: dict[str, str] = {}
    for alias in data.aliases:
        alias = " ".join(alias.split())
        key = normalize_location(alias)
        if not key or key in keys:
            continue
        owner = await _alias_owner(s, key, loc.id)
        if owner is not None:
            raise ValidationError(f"نام دیگر «{alias}» برای محل «{owner.name_fa}» ثبت شده است.")
        keys[key] = alias
    loc.code, loc.name_fa, loc.type = code, name, data.type
    loc.floor_id, loc.needs_floor = floor_id, needs_floor
    loc.merged_group, loc.note = data.merged_group.strip() or None, data.note.strip()
    current = {a.alias_normalized: a for a in loc.aliases}
    for key, alias in keys.items():
        if key in current:
            current[key].alias = alias
        else:
            loc.aliases.append(LocationAlias(alias=alias, alias_normalized=key))
    for key, row in current.items():
        if key not in keys:
            loc.aliases.remove(row)


def _snapshot(loc: Location) -> dict:
    return {"code": loc.code, "name": loc.name_fa, "type": str(loc.type), "floor_id": loc.floor_id,
            "aliases": sorted(a.alias for a in loc.aliases), "merged_group": loc.merged_group,
            "note": loc.note, "is_active": loc.is_active}


async def save_location(db: Database, actor: Actor, data: LocationInput,
                        location_id: int | None = None) -> int:
    actor.require(Perm.LOCATIONS_MANAGE)
    actor.require_human("تغییر محل‌های مصرف")
    async with db.session(actor.user_id) as s:
        if location_id is None:
            loc = Location(code="", name_fa="", type=data.type, is_active=True, note="")
            loc.aliases = []
            s.add(loc)
            before = None
        else:
            loc = await s.get(Location, location_id)
            if loc is None:
                raise NotFound("محل مصرف پیدا نشد.")
            before = _snapshot(loc)
        await _apply_input(s, loc, data)
        await s.flush()
        after = _snapshot(loc)
        version = _bump_version(await _settings_in(s))
        if before is None:
            audit.record(s, actor, "location.created", "location", loc.id, {**after, "version": version})
        else:
            changes = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
            audit.record(s, actor, "location.updated", "location", loc.id, {**changes, "version": version})
        return loc.id


async def set_location_active(db: Database, actor: Actor, location_id: int, active: bool) -> None:
    actor.require(Perm.LOCATIONS_MANAGE)
    actor.require_human("غیرفعال‌سازی محل مصرف")
    async with db.session(actor.user_id) as s:
        loc = await s.get(Location, location_id)
        if loc is None:
            raise NotFound("محل مصرف پیدا نشد.")
        if loc.is_active == active:
            return
        loc.is_active = active
        version = _bump_version(await _settings_in(s))
        audit.record(s, actor, "location.activated" if active else "location.deactivated", "location",
                     loc.id, {"code": loc.code, "version": version})


# ----- CSV re-import with preview (spec §2.7), same statuses as the draft-first imports -----


class PreviewStatus:
    NEW = "NEW"
    CHANGED = "CHANGED"
    UNCHANGED = "UNCHANGED"
    ERROR = "ERROR"


PREVIEW_NAMES = {PreviewStatus.NEW: "جدید", PreviewStatus.CHANGED: "تغییر",
                 PreviewStatus.UNCHANGED: "بدون تغییر", PreviewStatus.ERROR: "خطا"}


@dataclass
class PreviewRow:
    row: location_seed.SeedRow
    status: str
    changes: list[str] = field(default_factory=list)
    message: str = ""


def _row_input(row: location_seed.SeedRow, floor_ids: dict[str, int]) -> LocationInput:
    return LocationInput(row.code, row.name, LocationType(row.type), floor_ids.get(row.floor_code),
                         row.aliases, row.merged_with, row.note)


async def preview_csv(db: Database, actor: Actor, text: str) -> list[PreviewRow]:
    """What applying the file would change. Nothing is written. Rows missing from the file are left
    alone (deactivate them by hand), so a partial file can never hide history."""
    actor.require(Perm.LOCATIONS_MANAGE)
    rows = location_seed.parse_csv(text)
    async with db.session() as s:
        existing = {loc.code: loc for loc in await _all_locations(s)}
    alias_owner: dict[str, str] = {}
    for loc in existing.values():
        for a in loc.aliases:
            alias_owner.setdefault(a.alias_normalized, loc.code)
    seen_codes: set[str] = set()
    result = []
    for row in rows:
        if row.error:
            result.append(PreviewRow(row, PreviewStatus.ERROR, message=row.error))
            continue
        if row.code in seen_codes:
            result.append(PreviewRow(row, PreviewStatus.ERROR,
                                     message=f"کد «{row.code}» در فایل تکراری است."))
            continue
        seen_codes.add(row.code)
        clash = next((a for a in row.aliases
                      if alias_owner.get(normalize_location(a), row.code) != row.code), None)
        if clash is not None:
            owner = alias_owner[normalize_location(clash)]
            result.append(PreviewRow(row, PreviewStatus.ERROR,
                                     message=f"نام دیگر «{clash}» متعلق به محل «{owner}» است."))
            continue
        for a in row.aliases:
            alias_owner.setdefault(normalize_location(a), row.code)
        loc = existing.get(row.code)
        if loc is None:
            result.append(PreviewRow(row, PreviewStatus.NEW))
            continue
        changes = []
        if loc.name_fa != row.name:
            changes.append(f"نام: {loc.name_fa} ← {row.name}")
        if str(loc.type) != row.type.upper() and loc.type != LocationType(row.type):
            changes.append(f"نوع: {TYPE_NAMES[loc.type]} ← {TYPE_NAMES[LocationType(row.type)]}")
        old_floor = loc.floor.code if loc.floor else ""
        if old_floor != row.floor_code:
            changes.append(f"طبقه: {old_floor or '—'} ← {row.floor_code or '—'}")
        old_aliases = sorted(normalize_location(a.alias) for a in loc.aliases)
        if old_aliases != sorted({normalize_location(a) for a in row.aliases}):
            changes.append("نام‌های دیگر")
        if (loc.merged_group or "") != row.merged_with or loc.note != row.note:
            changes.append("یادداشت / ادغام")
        if not loc.is_active:
            changes.append("فعال‌سازی دوباره")
        status = PreviewStatus.CHANGED if changes else PreviewStatus.UNCHANGED
        result.append(PreviewRow(row, status, changes))
    return result


async def apply_csv(db: Database, actor: Actor, text: str) -> tuple[int, int]:
    """Apply NEW and CHANGED rows of a preview in one transaction. Returns (added, changed)."""
    actor.require(Perm.LOCATIONS_MANAGE)
    actor.require_human("ورود محل‌های مصرف از فایل")
    preview = await preview_csv(db, actor, text)
    errors = [p for p in preview if p.status == PreviewStatus.ERROR]
    if errors:
        first = errors[0]
        raise ValidationError(f"ردیف {to_persian_digits(first.row.line)}: {first.message} "
                              "ابتدا خطاهای فایل را برطرف کنید.")
    added = changed = 0
    async with db.session(actor.user_id) as s:
        existing = {loc.code: loc for loc in await _all_locations(s)}
        floor_ids = {f.code: f.id for f in await _all_floors(s)}
        for p in preview:
            if p.status not in (PreviewStatus.NEW, PreviewStatus.CHANGED):
                continue
            loc = existing.get(p.row.code)
            if loc is None:
                loc = Location(code="", name_fa="", type=LocationType(p.row.type), is_active=True,
                               note="")
                loc.aliases = []
                s.add(loc)
                added += 1
            else:
                changed += 1
                loc.is_active = True
            await _apply_input(s, loc, _row_input(p.row, floor_ids))
            await s.flush()
        if added or changed:
            version = _bump_version(await _settings_in(s))
            audit.record(s, actor, "locations.imported", details={
                "added": added, "changed": changed, "version": version,
                "at": dt.datetime.now().isoformat(timespec="seconds")})
    return added, changed


# ----- export (spec §2.7: Excel) -----


async def locations_table(db: Database, actor: Actor):
    from caspian.services.reports import Column, ReportTable

    actor.require(Perm.ITEMS_VIEW)
    rows = []
    for loc in await list_locations(db):
        rows.append((loc.code, loc.name_fa, TYPE_NAMES[loc.type],
                     loc.floor.name_fa if loc.floor else "انتخاب کاربر", "؛ ".join(loc.aliases),
                     loc.merged_group or "", "فعال" if loc.is_active else "غیرفعال", loc.note))
    return ReportTable("محل‌های مصرف", [Column("کد", "code"), Column("نام"), Column("نوع"), Column("طبقه"),
                                        Column("نام‌های دیگر"), Column("ادغام با"), Column("وضعیت"),
                                        Column("یادداشت")], rows)
