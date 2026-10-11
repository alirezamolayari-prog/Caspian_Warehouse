"""Consumption location on issue vouchers (spec Part 2 §2.4, §2.9)."""

import datetime as dt
import json
from decimal import Decimal

import pytest

from caspian.db.models import DocType, LocationType, PersonKind
from caspian.services import documents as docs
from caspian.services import items, locations, master
from caspian.services.ai.tools import ToolContext, run_tool
from caspian.services.documents import DocumentInput, LineInput
from caspian.services.errors import ValidationError
from caspian.services.items import ItemInput
from caspian.services.locations import LocationInput

TODAY = dt.date(2026, 9, 27)


@pytest.fixture
async def env(locations_required, admin):
    db = locations_required
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    lamp = await items.create_item(db, admin, ItemInput("3001", "لامپ", u["عدد"]))
    guard = await master.save_person(db, admin, "حسن نگهبان", PersonKind.EMPLOYEE)
    await docs.create_and_post(db, admin, DocumentInput(
        DocType.RECEIPT, TODAY, wh, [LineInput(lamp, u["عدد"], Decimal(50))]))
    index = await locations.load_index(db)
    return {"db": db, "admin": admin, "u": u, "wh": wh, "lamp": lamp, "guard": guard, "index": index,
            "floor": {f.code: f.id for f in index.floors}}


def issue(e, qty=1, **kw) -> DocumentInput:
    return DocumentInput(DocType.ISSUE, TODAY, e["wh"], [LineInput(e["lamp"], e["u"]["عدد"], Decimal(qty))],
                         person_id=e["guard"], **kw)


async def _place(e, doc_id) -> tuple[int | None, int | None, str]:
    from caspian.db.models import Document

    async with e["db"].session() as s:
        d = await s.get(Document, doc_id)
        return d.location_id, d.consumption_floor_id, d.usage_note


async def test_issue_needs_a_location_to_be_posted(env):
    doc_id = await docs.create_document(env["db"], env["admin"], issue(env))  # a draft may lack it
    with pytest.raises(ValidationError, match="محل مصرف"):
        await docs.post_document(env["db"], env["admin"], doc_id)


async def test_plate_stores_its_floor_on_the_voucher(env):
    plate = env["index"].find("68")
    doc_id = await docs.create_and_post(env["db"], env["admin"], issue(
        env, location_id=plate.id, floor_id=env["floor"]["F3"], usage_note=" تعویض لامپ راهرو "))
    assert await _place(env, doc_id) == (plate.id, env["floor"]["G"], "تعویض لامپ راهرو")
    sheet = await docs.print_sheet(env["db"], env["admin"], doc_id)
    assert sheet.place == "پلاک ۶۸ · همکف" and sheet.usage_note == "تعویض لامپ راهرو"
    detail = await docs.get_document(env["db"], env["admin"], doc_id)
    assert detail.input.location_id == plate.id and detail.input.floor_id == env["floor"]["G"]


async def test_vertical_and_general_cannot_be_saved_without_a_floor(env):
    for code in ("AB-ASD", "GEN"):
        loc = env["index"].find(code)
        with pytest.raises(ValidationError, match="طبقه"):
            await docs.create_document(env["db"], env["admin"], issue(env, location_id=loc.id))
        doc_id = await docs.create_and_post(env["db"], env["admin"], issue(
            env, location_id=loc.id, floor_id=env["floor"]["F2"]))
        assert (await _place(env, doc_id))[1] == env["floor"]["F2"]
    sheet = await docs.print_sheet(env["db"], env["admin"], doc_id)
    assert sheet.place == "مشاعات عمومی طبقه · طبقه دوم"


async def test_location_only_on_issues(env):
    plate = env["index"].find("68")
    with pytest.raises(ValidationError, match="فقط برای حواله خروج"):
        await docs.create_document(env["db"], env["admin"], DocumentInput(
            DocType.RECEIPT, TODAY, env["wh"], [LineInput(env["lamp"], env["u"]["عدد"], Decimal(1))],
            location_id=plate.id))


async def test_changing_a_locations_floor_keeps_old_vouchers(env):
    cafe = env["index"].find("CAFE")
    old = await docs.create_and_post(env["db"], env["admin"], issue(env, location_id=cafe.id))
    assert (await _place(env, old))[1] == env["floor"]["B"]
    await locations.save_location(env["db"], env["admin"], LocationInput(
        "CAFE", "کافه کاسپین", LocationType.NAMED, env["floor"]["G"], ["کافه"]), cafe.id)
    assert (await _place(env, old))[1] == env["floor"]["B"]  # history untouched
    new = await docs.create_and_post(env["db"], env["admin"], issue(env, location_id=cafe.id))
    assert (await _place(env, new))[1] == env["floor"]["G"]


async def test_deactivated_location_refused_for_new_vouchers_but_old_ones_keep_it(env):
    cafe = env["index"].find("CAFE")
    old = await docs.create_and_post(env["db"], env["admin"], issue(env, location_id=cafe.id))
    await locations.set_location_active(env["db"], env["admin"], cafe.id, False)
    with pytest.raises(ValidationError, match="غیرفعال"):
        await docs.create_document(env["db"], env["admin"], issue(env, location_id=cafe.id))
    assert (await docs.print_sheet(env["db"], env["admin"], old)).place == "کافه کاسپین · زیرهمکف"


async def test_setting_off_allows_issues_without_location(env):
    await locations.save_settings(env["db"], env["admin"], "1-6,46-57,83-92", required_on_issue=False)
    await docs.create_and_post(env["db"], env["admin"], issue(env))


# ----- the AI assistant creates issues with a place like a user -----


async def _ai(env, **args):
    ctx = ToolContext(env["db"], env["admin"].as_ai(), [], allow_post=False)
    base = {"doc_type": "ISSUE", "person": "حسن نگهبان", "lines": [{"code": "3001", "qty": 2}]}
    return json.loads(await run_tool(ctx, "create_document", {**base, **args}))


async def test_ai_asks_for_the_location_and_floor(env):
    assert (await _ai(env))["needs_choice"] == "location"
    assert "فاز ۱" in (await _ai(env, location="50"))["error"]
    asked = await _ai(env, location="باری اسد نژاد")
    assert asked["needs_choice"] == "floor" and "طبقه دوم" in asked["candidates"]
    made = await _ai(env, location="باری اسد نژاد", floor="دوم", usage_note="جابجایی")
    loc, floor, note = await _place(env, made["document_id"])
    assert loc == env["index"].find("AB-ASD").id and floor == env["floor"]["F2"] and note == "جابجایی"
    made = await _ai(env, location="۶۸")
    assert (await _place(env, made["document_id"]))[1] == env["floor"]["G"]


# ----- the editor -----


async def test_editor_collects_the_place(qtbot, themes, env):
    from caspian.core.settings import Settings
    from caspian.db.database import DbConfig
    from caspian.ui.app_context import AppContext
    from caspian.ui.documents_page import DocumentDialog

    ctx = AppContext(env["db"], DbConfig(), Settings(), themes, env["admin"])
    warehouses = await master.list_warehouses(env["db"])
    persons = await master.search_persons(env["db"])
    dialog = DocumentDialog(ctx, DocType.ISSUE, None, warehouses, persons,
                            await master.list_units(env["db"]), [])
    qtbot.addWidget(dialog)
    await dialog.load_lines()
    assert dialog.collect().location_id is None
    gen = env["index"].find("GEN")
    dialog.place.set_value(gen.id)
    with pytest.raises(ValidationError, match="طبقه"):
        dialog.collect()
    dialog.place.set_value(gen.id, env["floor"]["P2"])
    dialog.usage_note.setText("نظافت")
    data = dialog.collect()
    assert (data.location_id, data.floor_id, data.usage_note) == (gen.id, env["floor"]["P2"], "نظافت")
    dialog.place.edit.setText("zzz")
    dialog.place.location = None
    with pytest.raises(ValidationError, match="از فهرست"):
        dialog.collect()
