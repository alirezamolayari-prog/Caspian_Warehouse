from decimal import Decimal

import pytest

from caspian.db.models import PersonKind
from caspian.services import items, master
from caspian.services.errors import ConcurrencyError, ValidationError
from caspian.services.items import ItemInput


async def test_categories(db, admin):
    tools = await master.save_category(db, admin, "ابزار")
    power = await master.save_category(db, admin, "ابزار برقی", parent_id=tools)
    with pytest.raises(ValidationError):
        await master.save_category(db, admin, "ابزار")
    with pytest.raises(ValidationError):
        await master.save_category(db, admin, "x", category_id=tools, parent_id=tools)
    with pytest.raises(ValidationError, match="زیرگروه"):
        await master.delete_category(db, admin, tools)
    unit = (await master.list_units(db))[0].id
    await items.create_item(db, admin, ItemInput("1", "دریل", unit, category_id=power))
    rows = {c.name: c for c in await master.list_categories(db)}
    assert rows["ابزار برقی"].item_count == 1 and rows["ابزار برقی"].parent_id == tools
    with pytest.raises(ValidationError, match="کالا دارد"):
        await master.delete_category(db, admin, power)
    await master.save_category(db, admin, "ابزار دستی", category_id=tools)
    assert "ابزار دستی" in {c.name for c in await master.list_categories(db)}


async def test_units(db, admin):
    unit_id = await master.save_unit(db, admin, "شاخه")
    with pytest.raises(ValidationError):
        await master.save_unit(db, admin, "عدد")
    await master.set_unit_active(db, admin, unit_id, False)
    assert "شاخه" not in {u.name for u in await master.list_units(db)}
    assert "شاخه" in {u.name for u in await master.list_units(db, include_inactive=True)}

    piece = next(u.id for u in await master.list_units(db) if u.name == "عدد")
    box = next(u.id for u in await master.list_units(db) if u.name == "جعبه")
    await items.create_item(db, admin, ItemInput("1", "x", piece, units=[(box, Decimal(6))]))
    for used in (piece, box):
        with pytest.raises(ValidationError):
            await master.set_unit_active(db, admin, used, False)


async def test_warehouses(db, admin):
    main = (await master.list_warehouses(db))[0]
    with pytest.raises(ValidationError, match="حداقل یک انبار"):
        await master.set_warehouse_active(db, admin, main.id, False)
    second = await master.save_warehouse(db, admin, "۰۲", "انبار شماره ۲")
    with pytest.raises(ValidationError):
        await master.save_warehouse(db, admin, "02", "duplicate code")
    await master.set_warehouse_active(db, admin, main.id, False)
    assert [w.id for w in await master.list_warehouses(db)] == [second]


async def test_persons(db, admin):
    pid = await master.save_person(db, admin, "شركت  آلفا", PersonKind.SUPPLIER,
                                   phone="۰۹۱۲۱۲۳۴۵۶۷")
    await master.save_person(db, admin, "علی رضایی", PersonKind.EMPLOYEE)
    rows = await master.search_persons(db, "شرکت")  # Persian kaf finds Arabic kaf
    assert [r.name for r in rows] == ["شركت آلفا"]
    assert rows[0].code == "101" and rows[0].phone == "09121234567"
    assert [r.name for r in await master.search_persons(db, "0912")] == ["شركت آلفا"]
    assert len(await master.search_persons(db, kind=PersonKind.EMPLOYEE)) == 1

    version = rows[0].version_id
    await master.save_person(db, admin, "شرکت آلفا", PersonKind.SUPPLIER, person_id=pid,
                             expected_version=version)
    with pytest.raises(ConcurrencyError):
        await master.save_person(db, admin, "x", PersonKind.SUPPLIER, person_id=pid,
                                 expected_version=version)
    await master.set_person_active(db, admin, pid, False)
    assert await master.search_persons(db, "آلفا") == []


async def test_phone_is_validated_and_normalized(db, admin):
    """«abc-12» was accepted (#14)."""
    import pytest

    from caspian.db.models import PersonKind
    from caspian.services.errors import ValidationError

    pid = await master.save_person(db, admin, "علی", PersonKind.EMPLOYEE, phone="۰۹۱۲-۳۴۵ ۶۷۸۹")
    assert [p.phone for p in await master.search_persons(db) if p.id == pid] == ["0912-345 6789"]
    await master.save_person(db, admin, "شرکت", PersonKind.SUPPLIER, phone="+98 (21) 8888-0000")
    for bad in ("abc-12", "12", "0912+3333", "09-12a"):
        with pytest.raises(ValidationError, match="تلفن"):
            await master.save_person(db, admin, "بد", PersonKind.OTHER, phone=bad)
