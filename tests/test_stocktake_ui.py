import datetime as dt
from decimal import Decimal

import pytest

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocType, StocktakeStatus
from caspian.services import auth, items, master, users
from caspian.services import documents as docs
from caspian.services import stocktake as st
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext
from caspian.ui.printing import save_pdf
from caspian.ui.stocktake_page import CountDialog, ReportDialog, StocktakePage, sheet_html
from helpers import settle


@pytest.fixture
async def env(themes, db, admin):
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    a = await items.create_item(db, admin, ItemInput("1001", "دریل", u["عدد"],
                                                     barcodes=[("111", None)]))
    await items.create_item(db, admin, ItemInput("1002", "فرز", u["عدد"]))
    await docs.create_and_post(db, admin, docs.DocumentInput(
        DocType.RECEIPT, dt.date.today(), wh, [docs.LineInput(a, u["عدد"], Decimal(7))]))
    await users.create_user(db, admin, "shomar", "", "Count#2026", "counter")
    counter = (await auth.login(db, "shomar", "Count#2026")).actor
    sid = await st.create_stocktake(db, counter, wh, title="آزمایشی")
    return {"themes": themes, "db": db, "admin": admin, "counter": counter, "sid": sid}


def ctx_for(env, actor):
    return AppContext(env["db"], DbConfig(), Settings(), env["themes"], actor)


async def test_count_dialog_scan_and_submit(qtbot, env):
    db, counter = env["db"], env["counter"]
    sheet = await st.count_sheet(db, counter, env["sid"])
    dlg = CountDialog(ctx_for(env, counter), sheet)
    qtbot.addWidget(dlg)
    for code in ("111", "111", "1001", "999"):
        dlg.scan.setText(code)
        await dlg.on_scan()
    first = sheet.lines[0].id
    assert dlg.qty_edits[first].value() == Decimal(3)
    assert "نیست" in dlg.status.text()
    dlg.submit_button.click()
    await settle(dlg)
    assert "شمارش نشده" in dlg.status.text()  # فرز not counted yet
    dlg.missing_zero.setChecked(True)
    dlg.submit_button.click()
    await settle(dlg)
    rows = await st.list_stocktakes(db, counter)
    assert rows[0].status is StocktakeStatus.COUNTED


async def test_report_and_approve(qtbot, env):
    db, admin, counter, sid = env["db"], env["admin"], env["counter"], env["sid"]
    sheet = await st.count_sheet(db, counter, sid)
    await st.record_counts(db, counter, sid, {sheet.lines[0].id: (Decimal(5), "دو عدد شکسته")})
    await st.submit_counts(db, counter, sid, missing_as_zero=True)
    report = await st.discrepancy_report(db, admin, sid)
    dlg = ReportDialog(ctx_for(env, admin), report)
    qtbot.addWidget(dlg)
    assert dlg.table.rowCount() == 1
    assert dlg.table.item(0, 5).text() == "-۲"
    dlg.only_diff.setChecked(False)
    assert dlg.table.rowCount() == 2
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.document_id is not None


async def test_page_hides_report_from_counter(qtbot, env):
    page = StocktakePage(ctx_for(env, env["counter"]))
    qtbot.addWidget(page)
    await page.refresh()
    assert page.table.rowCount() == 1
    assert page.report_button.isHidden() and page.cancel_button.isHidden()
    page.table.selectRow(0)
    assert page.count_button.isEnabled()


async def test_sheet_is_blind_and_pdf_renders(env, tmp_path):
    sheet = await st.count_sheet(env["db"], env["counter"], env["sid"])
    html_text = sheet_html(sheet)
    assert "دریل" in html_text
    assert "<td>۷</td>" not in html_text and "<td>7</td>" not in html_text  # no system qty
    path = tmp_path / "sheet.pdf"
    save_pdf(html_text, str(path))
    data = path.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 2000
