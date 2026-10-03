import datetime as dt
from decimal import Decimal

import pytest

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocType, PersonKind
from caspian.services import auth, items, master, users
from caspian.services import documents as docs
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext
from caspian.ui.reports_page import ReportsPage, format_cell, report_to_html
from helpers import wait_until


@pytest.fixture
async def page(qtbot, themes, db, admin):
    u = {x.name: x.id for x in await master.list_units(db)}
    wh = (await master.list_warehouses(db))[0].id
    drill = await items.create_item(db, admin, ItemInput("1001", "دریل", u["عدد"]))
    await docs.create_and_post(db, admin, docs.DocumentInput(
        DocType.RECEIPT, dt.date.today() - dt.timedelta(10), wh,
        [docs.LineInput(drill, u["عدد"], Decimal(100), Decimal(250000))]))
    recipient = await master.save_person(db, admin, "گیرنده", PersonKind.EMPLOYEE)
    await docs.create_and_post(db, admin, docs.DocumentInput(
        DocType.ISSUE, dt.date.today() - dt.timedelta(5), wh,
        [docs.LineInput(drill, u["عدد"], Decimal(90))], person_id=recipient))
    p = ReportsPage(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(p)
    await p.load_filters()
    return p


def test_format_cell():
    assert format_cell(Decimal("1250.5"), "qty") == "۱٬۲۵۰٫۵"
    assert format_cell(12, "int") == "۱۲"
    assert format_cell(dt.date(2026, 3, 21), "date") == "۱۴۰۵/۰۱/۰۱"
    assert format_cell(None, "qty") == ""


async def test_stock_tab(page):
    await page.stock.refresh()
    assert page.stock.table.rowCount() == 1
    assert page.stock.table.item(0, 4).text() == "۱۰"
    assert "۲٬۵۰۰٬۰۰۰" in page.stock.totals.text()
    assert page.stock.excel_button.isEnabled()
    assert "دریل" in report_to_html(page.stock.report)


async def test_cardex_search_runs_report(qtbot, page):
    """One field for code / name / barcode (#11): typing the code and Enter runs the cardex."""
    from PySide6.QtCore import Qt

    page.cardex_item.lineEdit().setText("1001")
    qtbot.keyClick(page.cardex_item, Qt.Key.Key_Return)
    await wait_until(lambda: page.cardex.report is not None)
    assert page.cardex.table.rowCount() == 3  # opening balance + receipt + issue
    assert page.cardex.table.item(0, 1).text() == "مانده از قبل"
    assert page.cardex.table.item(2, 7).text() == "۱۰"


async def test_cardex_item_by_arabic_name_or_barcode(qtbot, page):
    from PySide6.QtCore import Qt

    db, admin = page._ctx.db, page._ctx.actor
    unit = (await master.list_units(db))[0].id
    await items.create_item(db, admin, ItemInput("2001", "کیف ابزار", unit, barcodes=[("626555", None)]))
    await page.load_filters()
    page.cardex_item.lineEdit().setText("كيف")  # Arabic keyboard
    qtbot.keyClick(page.cardex_item, Qt.Key.Key_Return)
    assert page.cardex_item.currentText() == "2001 — کیف ابزار"
    page.cardex_item.select_value(None)
    page.cardex_item.lineEdit().setText("626555")  # barcode: not in the list text
    qtbot.keyClick(page.cardex_item, Qt.Key.Key_Return)
    assert await wait_until(lambda: page.cardex_item.currentText() == "2001 — کیف ابزار")
    assert await wait_until(lambda: page.cardex.report is not None)


async def test_report_filters_are_searchable(page):
    from caspian.ui.widgets import SearchableCombo

    for combo in (page.stock_wh, page.stock_cat, page.cardex_wh, page.burn_wh, page.activity_user):
        assert isinstance(combo, SearchableCombo)
    assert page.stock_cat.itemText(0) == "همه گروه‌ها"


async def test_burn_rate_and_apply_reorder_point(page):
    page.lookback.setValue(30)
    await page.burn.refresh()
    assert page.burn.table.rowCount() == 1
    page.burn.table.selectAll()
    await page.on_apply_reorder_points()
    detail = await items.get_item(page._ctx.db, page._ctx.actor, page.burn.report.row_ids[0])
    # 90 issued over 30 days = 3/day; (14 lead + 7 safety) * 3 = 63
    assert detail.input.reorder_point == Decimal(63)


async def test_viewer_sees_only_permitted_tabs(qtbot, themes, db, admin):
    await users.create_user(db, admin, "neda", "", "Neda#2026", "viewer")
    viewer = (await auth.login(db, "neda", "Neda#2026")).actor
    p = ReportsPage(AppContext(db, DbConfig(), Settings(), themes, viewer))
    qtbot.addWidget(p)
    assert p.tabs.isTabVisible(p.tabs.indexOf(p.stock))
    assert not p.tabs.isTabVisible(p.activity_index)
    assert p.apply_rp.isHidden()


async def test_excel_export_uses_awaitable_dialog(page, tmp_path, monkeypatch):
    from caspian.ui import reports_page

    target = tmp_path / "stock.xlsx"
    asked = {}

    async def fake_ask(parent, title, settings, default_name, name_filter):
        asked["name"] = default_name
        return str(target)

    monkeypatch.setattr(reports_page, "ask_save_path", fake_ask)
    await page.stock.refresh()
    await page.stock.on_excel()
    assert await wait_until(target.exists)
    assert asked["name"] == f"{page.stock.report.title}.xlsx"


async def test_stock_report_uses_the_width(page):
    """#31: text columns stretch, numbers fit their content."""
    from PySide6.QtWidgets import QHeaderView

    await page.stock.refresh()
    header = page.stock.table.horizontalHeader()
    from caspian.ui.reports_page import SHORT_TEXT_COLUMNS

    mode = QHeaderView.ResizeMode
    for i, c in enumerate(page.stock.report.columns):
        stretch = c.kind == "text" and c.title not in SHORT_TEXT_COLUMNS  # names; codes fit (#17)
        assert header.sectionResizeMode(i) == (mode.Stretch if stretch else mode.ResizeToContents)
    assert page.stock_cat.minimumWidth() >= 180 and page.stock_cat.itemText(0) == "همه گروه‌ها"


async def test_burn_rate_columns_are_not_squeezed(page):
    """«کد»/«نام کالا»/«واحد» were cut to «10…/چه…» and the checkbox label clipped (#17)."""
    page.resize(700, 500)
    await page.burn.refresh()
    header = page.burn.table.horizontalHeader()
    assert header.minimumSectionSize() >= 72
    titles = [c.title for c in page.burn.report.columns]
    assert header.sectionResizeMode(titles.index("کد")) == header.ResizeMode.ResizeToContents
    # The checkbox is on the second filter row, not after the four spin boxes.
    assert page.burn.filters.indexOf(page.only_order) == -1
    second = page.burn.layout().itemAt(page.burn.layout().indexOf(page.burn.filters) + 1).layout()
    assert second.indexOf(page.only_order) >= 0
