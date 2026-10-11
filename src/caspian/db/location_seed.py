"""Floors and consumption locations from `data/seed/caspian_locations.csv` (spec Part 2).

Used by the Alembic data migration (first install / upgrade) and parsed again by the admin's
«ورود دوباره از CSV» preview. Seeding inserts only what is missing (matched by `code`), so it
can run any number of times and never overwrites changes the admin made in the app.
Plain SQLAlchemy Core on a sync connection: independent of later ORM model changes.
"""

import csv
import io
import sys
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

from caspian.core.text import normalize_location

CSV_NAME = "caspian_locations.csv"
COLUMNS = ("code", "name", "type", "floor", "floor_code", "needs_floor", "aliases", "merged_with", "note")
TYPES = ("plate", "named", "vertical", "general", "floor_place")

# Spec §2.2 — stable codes shared with the server and the Caspian Owner app (bottom to top).
FLOORS: tuple[tuple[str, str, int], ...] = (
    ("P4", "پارکینگ P4", 1),
    ("P3", "پارکینگ P3", 2),
    ("P2", "پارکینگ P2", 3),
    ("P1", "پارکینگ P1", 4),
    ("B", "زیرهمکف", 5),
    ("G", "همکف", 6),
    ("F1", "اول", 7),
    ("F2", "دوم", 8),
    ("F3", "سوم", 9),
    ("ROOF", "بام", 10),
)

SETTINGS_KEY = "locations"
# Phase 1 plates are out of scope and refused with a clear message (spec §1, §2.6). Kept as data
# in app_settings so a correction needs no code change.
DEFAULT_SETTINGS = {"version": 1, "phase1_plates": "1-6,46-57,83-92"}


def seed_path() -> Path:
    """The bundled CSV: next to the frozen app (PyInstaller `seed/`) or in the repo's data/seed."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "seed" / CSV_NAME
    return Path(__file__).resolve().parents[3] / "data" / "seed" / CSV_NAME


@dataclass
class SeedRow:
    line: int
    code: str
    name: str
    type: str
    floor_code: str
    needs_floor: bool
    aliases: list[str] = field(default_factory=list)
    merged_with: str = ""
    note: str = ""
    error: str = ""


def parse_csv(text: str) -> list[SeedRow]:
    """Rows of the spec §2.3 format. Problems are reported per row (`error`), never raised, so the
    preview can show them."""
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        return [SeedRow(1, "", "", "", "", False, error=f"ستون‌های لازم در فایل نیست: {', '.join(missing)}")]
    floor_codes = {code for code, _name, _sort in FLOORS}
    rows: list[SeedRow] = []
    for n, raw in enumerate(reader, start=2):
        cell = {key: (raw.get(key) or "").strip() for key in COLUMNS}
        row = SeedRow(
            n, cell["code"], cell["name"], cell["type"].lower(), cell["floor_code"].upper(),
            cell["needs_floor"].lower() in ("yes", "true", "1", "بله"),
            [a.strip() for a in cell["aliases"].split(";") if a.strip()],
            cell["merged_with"], cell["note"])
        if not row.code or not row.name:
            row.error = "کد یا نام خالی است."
        elif row.type not in TYPES:
            row.error = f"نوع «{row.type}» نامعتبر است."
        elif row.floor_code and row.floor_code not in floor_codes:
            row.error = f"کد طبقه «{row.floor_code}» نامعتبر است."
        elif row.needs_floor and row.floor_code:
            row.error = "محلی که طبقه را کاربر انتخاب می‌کند نباید طبقه ثابت داشته باشد."
        elif not row.needs_floor and not row.floor_code:
            row.error = "طبقه این محل مشخص نیست."
        rows.append(row)
    return rows


def read_seed() -> list[SeedRow]:
    return parse_csv(seed_path().read_text(encoding="utf-8-sig"))


_floors = sa.table("floors", sa.column("id", sa.Integer), sa.column("code", sa.String),
                   sa.column("name_fa", sa.String), sa.column("sort_order", sa.Integer),
                   sa.column("is_active", sa.Boolean))
_locations = sa.table("locations", sa.column("id", sa.Integer), sa.column("code", sa.String),
                      sa.column("name_fa", sa.String), sa.column("type", sa.String),
                      sa.column("floor_id", sa.Integer), sa.column("needs_floor", sa.Boolean),
                      sa.column("merged_group", sa.String), sa.column("is_active", sa.Boolean),
                      sa.column("note", sa.Text))
_aliases = sa.table("location_aliases", sa.column("location_id", sa.Integer),
                    sa.column("alias", sa.String), sa.column("alias_normalized", sa.String))
_settings = sa.table("app_settings", sa.column("key", sa.String), sa.column("value", sa.JSON))


def seed(conn: sa.Connection, rows: list[SeedRow]) -> tuple[int, int]:
    """Insert missing floors and locations (by code). Returns (floors added, locations added)."""
    have_floors = dict(conn.execute(sa.select(_floors.c.code, _floors.c.id)).all())
    added_floors = 0
    for code, name, sort in FLOORS:
        if code not in have_floors:
            conn.execute(_floors.insert().values(code=code, name_fa=name, sort_order=sort, is_active=True))
            added_floors += 1
    floor_ids = dict(conn.execute(sa.select(_floors.c.code, _floors.c.id)).all())
    have = set(conn.execute(sa.select(_locations.c.code)).scalars())
    added = 0
    for row in rows:
        if row.error or row.code in have:
            continue
        conn.execute(_locations.insert().values(
            code=row.code, name_fa=row.name, type=row.type.upper(),
            floor_id=floor_ids.get(row.floor_code) if row.floor_code else None,
            needs_floor=row.needs_floor, merged_group=row.merged_with or None, is_active=True,
            note=row.note))
        location_id = conn.execute(
            sa.select(_locations.c.id).where(_locations.c.code == row.code)).scalar_one()
        seen = set()
        for alias in row.aliases:
            key = normalize_location(alias)
            if key and key not in seen:
                seen.add(key)
                conn.execute(_aliases.insert().values(location_id=location_id, alias=alias,
                                                      alias_normalized=key))
        have.add(row.code)
        added += 1
    if conn.execute(sa.select(_settings.c.key).where(_settings.c.key == SETTINGS_KEY)).first() is None:
        conn.execute(_settings.insert().values(key=SETTINGS_KEY, value=DEFAULT_SETTINGS))
    return added_floors, added
