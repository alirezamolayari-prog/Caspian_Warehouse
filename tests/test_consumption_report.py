"""Consumption by floor and by location (spec §2.8, §2.9): reports, Excel, AI and MCP."""

import datetime as dt
import json
import shutil
from decimal import Decimal

import openpyxl
import pytest

import approval_helpers as slips
from caspian.db.models import DocType, PersonKind
from caspian.db.readonly import wrap_read_only
from caspian.mcp_server import build_server
from caspian.services import items, locations, master, reports, scheduler
from caspian.services.ai.tools import ToolContext, run_tool
from caspian.services.documents import DocumentInput, LineInput
from caspian.services.excel_export import write_xlsx
from caspian.services.items import ItemInput

DAY = dt.date(2026, 9, 27)


@pytest.fixture
async def env(locations_required, admin):
    db = locations_required
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    lamp = await items.create_item(db, admin, ItemInput("3001", "لامپ", u["عدد"]))
    soap = await items.create_item(db, admin, ItemInput("3002", "مایع شوینده", u["عدد"]))
    guard = await master.save_person(db, admin, "حسن نگهبان", PersonKind.EMPLOYEE)
    cleaner = await master.save_person(db, admin, "مریم نظافت", PersonKind.EMPLOYEE)
    await slips.create_and_post(db, admin, DocumentInput(DocType.RECEIPT, DAY - dt.timedelta(days=5), wh, [
        LineInput(lamp, u["عدد"], Decimal(100), Decimal(50_000)),
        LineInput(soap, u["عدد"], Decimal(100), Decimal(200_000))]))
    index = await locations.load_index(db)
    floor = {f.code: f.id for f in index.floors}

    async def issue(code, lines, person=guard, floor_code=None, day=DAY):
        return await slips.create_and_post(db, admin, DocumentInput(
            DocType.ISSUE, day, wh, [LineInput(i, u["عدد"], Decimal(q)) for i, q in lines], person_id=person,
            location_id=index.find(code).id, floor_id=floor[floor_code] if floor_code else None))

    await issue("68", [(lamp, 2)])
    await issue("70", [(lamp, 1), (soap, 1)], person=cleaner)
    await issue("AB-ASD", [(lamp, 3)], floor_code="F2")
    await issue("GEN", [(soap, 2)], person=cleaner, floor_code="F2")
    await issue("CAFE", [(soap, 1)], day=DAY - dt.timedelta(days=40))
    await locations.set_location_active(db, admin, index.find("CAFE").id, False)
    return {"db": db, "admin": admin, "lamp": lamp, "guard": guard, "cleaner": cleaner}


async def test_by_floor_in_building_order(env):
    rows = await reports.consumption(env["db"], env["admin"], "floor")
    # 4.8: as people read the building — roof down to the basement, then parking P1 … P4.
    assert [r.floor_code for r in rows] == ["ROOF", "F3", "F2", "F1", "G", "B", "P1", "P2", "P3", "P4"]
    by = {r.floor_code: r for r in rows}
    assert (by["G"].documents, by["G"].items) == (2, 2)
    assert by["F2"].documents == 2 and by["B"].documents == 1 and by["P4"].documents == 0
    assert by["G"].value == Decimal(3 * 50_000 + 200_000)


async def test_by_location_keeps_deactivated_places(env):
    rows = await reports.consumption(env["db"], env["admin"], "location")
    labels = {r.location for r in rows}
    assert {"پلاک ۶۸ · طبقه همکف", "پلاک ۷۰ · طبقه همکف", "آسانسور باری اسدنژاد · طبقه دوم",
            "مشاعات عمومی طبقه · طبقه دوم", "کافه کاسپین · طبقه زیرهمکف"} == labels  # CAFE deactivated
    assert {r.location_code for r in rows} == {"68", "70", "AB-ASD", "GEN", "CAFE"}


async def test_filters_date_item_and_requester(env):
    week_ago = DAY - dt.timedelta(days=7)
    recent = await reports.consumption(env["db"], env["admin"], "location", date_from=week_ago)
    assert "CAFE" not in {r.location_code for r in recent}
    lamp = await reports.consumption(env["db"], env["admin"], "floor", item_id=env["lamp"])
    assert {r.floor_code: r.qty for r in lamp if r.documents} == {"G": 3, "F2": 3}
    cleaner = await reports.consumption(env["db"], env["admin"], "location", person_id=env["cleaner"])
    assert {r.location_code for r in cleaner} == {"70", "GEN"}


async def test_table_and_excel(env, tmp_path):
    table = await reports.consumption_table(env["db"], env["admin"], "floor", item_id=env["lamp"])
    assert [c.title for c in table.columns] == ["طبقه", "تعداد حواله", "تعداد اقلام", "مقدار",
                                                "ارزش تقریبی (ریال)"]
    assert table.totals[3] == 6 and table.totals[1] == 3
    write_xlsx(table, tmp_path / "c.xlsx")
    values = [c.value for row in openpyxl.load_workbook(tmp_path / "c.xlsx").active.iter_rows() for c in row]
    assert "طبقه همکف" in values and "طبقه دوم" in values
    scheduled = await scheduler.build_report(env["db"], "consumption_location")
    assert scheduled.title == "مصرف به تفکیک محل"


async def test_issues_without_place_are_listed_apart(env):
    await locations.save_settings(env["db"], env["admin"], "1-6,46-57,83-92", required_on_issue=False)
    u = {x.name: x.id for x in await master.list_units(env["db"])}
    wh = (await master.list_warehouses(env["db"]))[0].id
    await slips.create_and_post(env["db"], env["admin"], DocumentInput(
        DocType.ISSUE, DAY, wh, [LineInput(env["lamp"], u["عدد"], Decimal(1))], person_id=env["guard"]))
    rows = await reports.consumption(env["db"], env["admin"], "floor")
    assert rows[-1].floor == reports.NO_PLACE and rows[-1].documents == 1


async def _ai(env, name, **args):
    ctx = ToolContext(env["db"], env["admin"].as_ai(), [])
    return json.loads(await run_tool(ctx, name, args))


async def test_ai_answers_consumption_on_the_ground_floor(env):
    answer = await _ai(env, "consumption_report", floor="همکف", period="all")
    assert [r["floor_code"] for r in answer["rows"]] == ["G"] and answer["rows"][0]["vouchers"] == 2
    by_place = await _ai(env, "consumption_report", group_by="location", period="all", item="لامپ")
    assert {r["location_code"]: r["qty"] for r in by_place["rows"]} == {"68": 2, "70": 1, "AB-ASD": 3}
    found = await _ai(env, "find_location", query="باری اسد نژاد")
    assert found["locations"][0]["code"] == "AB-ASD" and found["locations"][0]["needs_floor"]
    assert "فاز ۱" in (await _ai(env, "find_location", query="50"))["error"]


async def test_mcp_tools(env, tmp_path):
    await env["db"].dispose()
    copy = tmp_path / "ro.db"
    shutil.copyfile(str(env["db"].url.database), copy)
    ro = wrap_read_only(f"sqlite+aiosqlite:///{copy}")
    try:
        server = build_server(ro, allow_sql=False)
        result = json.loads((await server.call_tool("consumption_report", {"period": "all"})).content[0].text)
        assert {r["floor_code"]: r["vouchers"] for r in result["rows"]}["F2"] == 2
        found = json.loads((await server.call_tool("locations", {"query": "126-1"})).content[0].text)
        assert found["locations"][0]["code"] == "126/1"
        report = json.loads((await server.call_tool("report", {"name": "consumption_floor"})).content[0].text)
        assert report["title"] == "مصرف به تفکیک طبقه"
    finally:
        await ro.dispose()


def test_task_parser_knows_the_consumption_reports():
    [p] = scheduler.parse_instructions("هر شنبه ساعت ۹ گزارش مصرف به تفکیک طبقه را تلگرام کن")
    assert p.params["report"] == "consumption_floor"
    [p] = scheduler.parse_instructions("هر روز گزارش مصرف محل‌ها را ایمیل کن")
    assert p.params["report"] == "consumption_location"


async def test_reports_tab(qtbot, themes, env):
    from caspian.core.settings import Settings
    from caspian.db.database import DbConfig
    from caspian.ui.app_context import AppContext
    from caspian.ui.reports_page import ReportsPage

    page = ReportsPage(AppContext(env["db"], DbConfig(), Settings(), themes, env["admin"]))
    qtbot.addWidget(page)
    await page.load_filters()
    page.places_from.set_date(DAY - dt.timedelta(days=60))
    await page.places.refresh()
    assert page.places.table.rowCount() == 10  # every floor, in building order
    page.places_group.setCurrentIndex(1)
    page.places_person.select_value(env["cleaner"])
    await page.places.refresh()
    assert page.places.table.rowCount() == 2


async def test_value_ignores_a_newer_receipt_without_price(env):
    """4.8: a later receipt line with no price (0) must not turn the value into ۰."""
    db, admin = env["db"], env["admin"]
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    await slips.create_and_post(db, admin, DocumentInput(DocType.RECEIPT, DAY - dt.timedelta(days=1), wh, [
        LineInput(env["lamp"], u["عدد"], Decimal(10), Decimal(0))]))
    async with db.session() as s:
        prices = await reports.last_purchase_prices(s, [env["lamp"]])
    assert prices[env["lamp"]] == Decimal(50_000)
    by = {r.floor_code: r for r in await reports.consumption(db, admin, "floor")}
    assert by["G"].value == Decimal(3 * 50_000 + 200_000)
