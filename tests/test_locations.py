"""Consumption locations — spec docs/specs/caspian_consumption_location_spec.md §2.9."""

import pytest
from sqlalchemy import func, select

from caspian.core.text import normalize_location
from caspian.db import location_seed
from caspian.db.models import AuditLog, Floor, Location, LocationAlias, LocationType
from caspian.services import locations
from caspian.services.errors import PermissionDenied, ValidationError
from caspian.services.locations import LocationInput, Phase1Plate


async def _counts(db) -> tuple[int, int, int]:
    async with db.session() as s:
        return tuple([await s.scalar(select(func.count()).select_from(m))
                      for m in (Floor, Location, LocationAlias)])


def test_csv_has_the_spec_rows():
    rows = location_seed.read_seed()
    assert len(rows) == 141 and not [r for r in rows if r.error]
    assert {r.type for r in rows} == set(location_seed.TYPES)


async def test_seeded_by_migration_and_seed_is_idempotent(db):
    before = await _counts(db)
    assert before[:2] == (10, 141)

    async with db.engine.begin() as conn:  # running the seed again adds nothing
        added = await conn.run_sync(lambda c: location_seed.seed(c, location_seed.read_seed()))
    assert added == (0, 0) and await _counts(db) == before


@pytest.mark.parametrize("text", ["68", "۶۸", "٦٨", " 68 ", "پلاک 68", "پلاک۶۸"])
async def test_plate_68_in_every_writing(db, text):
    index = await locations.load_index(db)
    loc = index.exact(text)
    assert loc is not None and loc.code == "68" and loc.floor.code == "G"
    assert loc.label() == "پلاک ۶۸ · همکف"


@pytest.mark.parametrize("text", ["126/1", "126-1", "۱۲۶/۱", "١٢٦/١", "۱۲۶-۱", "126 / 1"])
async def test_plate_126_1_in_every_writing(db, text):
    loc = (await locations.load_index(db)).exact(text)
    assert loc is not None and loc.code == "126/1" and loc.floor.code == "F2"


@pytest.mark.parametrize(("text", "code"), [
    ("باری اسد نژاد", "AB-ASD"), ("باری اسدنژاد", "AB-ASD"), ("آسانسور باری اسدنژاد", "AB-ASD"),
    ("دو قلو کافه", "AD-CAF"), ("شیشه‌ای صفرخانی", "AS-SAF"), ("ab-asd", "AB-ASD"),
    ("کافه", "CAFE"), ("پشت بام", "BAM"), ("p1", "P1"), ("راهرو", "GEN"),
    ("باري اسد نژاد", "AB-ASD"),  # Arabic yeh
])
async def test_names_and_aliases(db, text, code):
    loc = (await locations.load_index(db)).exact(text)
    assert loc is not None and loc.code == code


def test_normalisation_rules():
    assert normalize_location("اسد نژاد") == normalize_location("اسدنژاد")
    assert normalize_location("دو قلو") == normalize_location("دوقلو")
    assert normalize_location("شیشه‌ای") == normalize_location("شیشه ای") == normalize_location("شیشهای")
    assert normalize_location("پلـاک ۶۸") == normalize_location("پلاک68")  # tatweel
    assert normalize_location("126-1") == normalize_location("۱۲۶/۱") == "126/1"


@pytest.mark.parametrize("text", ["50", "۵۰", "1", "6", "46", "57", "83", "92", "پلاک 50"])
async def test_phase1_plates_are_refused(db, text):
    index = await locations.load_index(db)
    with pytest.raises(Phase1Plate, match="این پلاک مربوط به فاز ۱ است"):
        index.exact(text)
    with pytest.raises(Phase1Plate):
        index.search(text)


async def test_search_ranks_exact_then_prefix(db):
    index = await locations.load_index(db)
    found = index.search("اسدنژاد")
    assert {loc.code for loc in found} >= {"PL-ASD", "AB-ASD", "AS-ASD", "AD-ASD"}
    assert index.search("12")[0].code == "12"  # the exact plate first, then 120, 121, …
    assert index.search("") and index.search("xyz-nothing") == []


async def test_floor_rules_for_each_type(db):
    index = await locations.load_index(db)
    f2 = next(f for f in index.floors if f.code == "F2")
    async with db.session() as s:
        for code in ("AB-ASD", "GEN", "PL-W"):  # vertical / general: the user must pick a floor
            with pytest.raises(ValidationError, match="طبقه"):
                await locations.resolve_place_in(s, index.find(code).id, None)
            place = await locations.resolve_place_in(s, index.find(code).id, f2.id)
            assert place.floor.code == "F2"
        # Fixed floors always win, even if a different floor is passed.
        assert (await locations.resolve_place_in(s, index.find("68").id, f2.id)).floor.code == "G"
        assert (await locations.resolve_place_in(s, index.find("CAFE").id, None)).floor.code == "B"
        assert (await locations.resolve_place_in(s, index.find("P3").id, None)).floor.code == "P3"
    assert index.find("AB-ASD").label(f2) == "آسانسور باری اسدنژاد · طبقه دوم"
    assert index.find("P1").label() == "پارکینگ P1"


async def test_deactivated_location_is_hidden_from_search(db, admin):
    index = await locations.load_index(db)
    await locations.set_location_active(db, admin, index.find("CAFE").id, False)
    index = await locations.load_index(db)
    assert index.exact("کافه") is None and all(loc.code != "CAFE" for loc in index.search("کافه"))
    assert index.find("CAFE") is not None  # still known for history and reports
    assert any(loc.code == "CAFE" for loc in await locations.list_locations(db, "کافه"))
    async with db.session() as s:
        with pytest.raises(ValidationError, match="غیرفعال"):
            await locations.resolve_place_in(s, index.find("CAFE").id, None)
        await locations.resolve_place_in(s, index.find("CAFE").id, None, allow_inactive=True)


async def test_admin_edit_audits_bumps_version_and_refuses_clashes(db, admin):
    index = await locations.load_index(db)
    floors = {f.code: f.id for f in index.floors}
    version = (await locations.read_settings(db)).version
    new_id = await locations.save_location(db, admin, LocationInput(
        "LOBBY", "لابی اصلی", LocationType.NAMED, floors["G"], ["لابی", "ورودی اصلی"]))
    loc = (await locations.load_index(db)).exact("ورودی اصلی")
    assert loc.id == new_id and loc.floor.code == "G"
    assert (await locations.read_settings(db)).version == version + 1
    with pytest.raises(ValidationError, match="کافه"):  # «کافه» is CAFE's alias already
        await locations.save_location(db, admin, LocationInput(
            "X", "ایکس", LocationType.NAMED, floors["G"], ["کافه"]))
    with pytest.raises(ValidationError, match="استفاده شده"):
        await locations.save_location(db, admin,
                                      LocationInput("68", "تکراری", LocationType.NAMED, floors["G"]))
    with pytest.raises(ValidationError, match="طبقه"):
        await locations.save_location(db, admin, LocationInput("Y", "بدون طبقه", LocationType.NAMED))
    # A vertical location never keeps a fixed floor.
    await locations.save_location(db, admin, LocationInput(
        "PL-N", "پله شمالی", LocationType.VERTICAL, floors["G"], ["پله شمالی"]))
    assert (await locations.load_index(db)).find("PL-N").needs_floor
    async with db.session() as s:
        actions = set((await s.scalars(select(AuditLog.action))).all())
    assert {"location.created"} <= actions


async def test_only_location_managers_and_never_the_ai(db, admin):
    from caspian.services import auth, users

    await users.create_user(db, admin, "mgr", "مدیر انبار", "Mgr#2026pass", "manager")
    manager = (await auth.login(db, "mgr", "Mgr#2026pass")).actor
    data = LocationInput("Z", "زد", LocationType.GENERAL)
    for actor in (manager, admin.as_ai()):
        with pytest.raises(PermissionDenied):
            await locations.save_location(db, actor, data)


async def test_csv_preview_then_apply(db, admin):
    text = location_seed.seed_path().read_text(encoding="utf-8-sig")
    preview = await locations.preview_csv(db, admin, text)
    assert {p.status for p in preview} == {locations.PreviewStatus.UNCHANGED}

    changed = (text.replace("CAFE,کافه کاسپین,named,زیرهمکف,B,no,کافه,,",
                            "CAFE,کافه کاسپین,named,همکف,G,no,کافه;کافی شاپ,,")
               + "LOBBY,لابی,named,همکف,G,no,لابی,,\r\n"
               + "BAD,بد,named,طبقه دهم,F10,no,,,\r\n"
               + "Q,کیو,named,همکف,G,no,کافه,,\r\n")
    preview = await locations.preview_csv(db, admin, changed)
    by_code = {p.row.code: p for p in preview}
    assert by_code["CAFE"].status == "CHANGED" and any("طبقه" in c for c in by_code["CAFE"].changes)
    assert by_code["LOBBY"].status == "NEW"
    assert by_code["BAD"].status == "ERROR" and by_code["Q"].status == "ERROR"  # duplicate alias
    with pytest.raises(ValidationError, match="ردیف"):
        await locations.apply_csv(db, admin, changed)  # nothing is written while errors remain

    good = (changed.replace("BAD,بد,named,طبقه دهم,F10,no,,,\r\n", "")
            .replace("Q,کیو,named,همکف,G,no,کافه,,\r\n", ""))
    assert await locations.apply_csv(db, admin, good) == (1, 1)
    index = await locations.load_index(db)
    assert index.exact("کافی شاپ").code == "CAFE" and index.find("CAFE").floor.code == "G"
    assert index.exact("لابی").code == "LOBBY"
    assert {p.status for p in await locations.preview_csv(db, admin, good)} == {"UNCHANGED"}


async def test_phase1_ranges_are_settings(db, admin):
    expected = set(range(1, 7)) | set(range(46, 58)) | set(range(83, 93))
    assert locations.parse_ranges("1-6, 46-57,۸۳-۹۲") == expected
    await locations.save_settings(db, admin, "1-6", required_on_issue=True)
    index = await locations.load_index(db)
    assert index.exact("50") is None  # no longer a Phase 1 plate, and not a known plate either
    with pytest.raises(Phase1Plate):
        index.exact("3")
