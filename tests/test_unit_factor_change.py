"""Test report issue 3: a draft saved as «۲ کارتن» while 1 کارتن = 24, then the factor changes.
Posting must not silently use either factor: it is refused with a Persian explanation until a
person opens the draft and saves it again (which recomputes with today's factor)."""

import datetime as dt
from decimal import Decimal

import pytest

import approval_helpers as slips
from caspian.db.models import DocType
from caspian.services import documents, items, master
from caspian.services.errors import ValidationError
from caspian.services.items import ItemInput


@pytest.fixture
async def screws(db, admin):
    units = {u.name: u.id for u in await master.list_units(db)}
    piece, box = units["عدد"], units["کارتن"]
    item = await items.create_item(db, admin, ItemInput("9", "پیچ", piece, units=[(box, Decimal(24))]))
    wh = (await master.list_warehouses(db))[0].id
    await slips.create_and_post(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(item, piece, Decimal(100))]))
    draft = await documents.create_document(db, admin, documents.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [documents.LineInput(item, box, Decimal(2))]))
    return {"item": item, "piece": piece, "box": box, "wh": wh, "draft": draft}


async def _set_units(db, admin, item_id, units):
    detail = await items.get_item(db, admin, item_id)
    data = detail.input
    data.units = units
    await items.update_item(db, admin, item_id, detail.version_id, data)


async def _stock(db, item_id) -> Decimal:
    return sum((q for _w, q in await documents.stock_by_warehouse(db, item_id)), Decimal(0))


async def test_changed_factor_blocks_posting_with_a_clear_message(db, admin, screws):
    await _set_units(db, admin, screws["item"], [(screws["box"], Decimal(12))])
    with pytest.raises(ValidationError) as exc:
        await slips.post_document(db, admin, screws["draft"])
    assert "ضریب" in exc.value.message and "کارتن" in exc.value.message and "پیچ" in exc.value.message
    assert "۲۴" in exc.value.message and "۱۲" in exc.value.message
    assert await _stock(db, screws["item"]) == 100  # nothing moved
    # The editor saves the draft again before posting (recomputing with today's factor), so its own
    # «ذخیره و ثبت نهایی» is the "person looks at it" step and is not blocked.


async def test_saving_the_draft_again_uses_todays_factor(db, admin, screws):
    await _set_units(db, admin, screws["item"], [(screws["box"], Decimal(12))])
    detail = await documents.get_document(db, admin, screws["draft"])
    await documents.update_document(db, admin, screws["draft"], detail.version_id, detail.input)
    await slips.post_document(db, admin, screws["draft"])
    assert await _stock(db, screws["item"]) == 124


async def test_unchanged_factor_posts_normally(db, admin, screws):
    await slips.post_document(db, admin, screws["draft"])
    assert await _stock(db, screws["item"]) == 148


async def test_removed_unit_blocks_posting(db, admin, screws):
    await _set_units(db, admin, screws["item"], [])
    with pytest.raises(ValidationError) as exc:
        await slips.post_document(db, admin, screws["draft"])
    assert "واحد" in exc.value.message
    assert await _stock(db, screws["item"]) == 100


async def test_update_item_changes_adds_and_removes_units(db, admin, screws):
    """Report gap: changing an item's unit list had no test."""
    units = {u.name: u.id for u in await master.list_units(db)}
    pack = units.get("بسته") or await master.save_unit(db, admin, "بسته")
    await _set_units(db, admin, screws["item"], [(screws["box"], Decimal(10)), (pack, Decimal(5))])
    assert sorted((await items.get_item(db, admin, screws["item"])).input.units) == sorted(
        [(screws["box"], Decimal(10)), (pack, Decimal(5))])
    await _set_units(db, admin, screws["item"], [(pack, Decimal(6))])
    assert (await items.get_item(db, admin, screws["item"])).input.units == [(pack, Decimal(6))]
