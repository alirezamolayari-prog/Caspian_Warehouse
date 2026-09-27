import datetime as dt
from decimal import Decimal

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import DocType
from caspian.services import backup, fiscal, items, master, protected
from caspian.services import documents as docs
from caspian.services.fiscal import CloseOptions
from caspian.services.items import ItemInput
from caspian.ui.app_context import AppContext
from caspian.ui.reports_page import ReportsPage
from caspian.ui.year_end import FiscalTab, YearEndWizard
from helpers import wait_until

PASSWORD = "Backup#Pass2026"


async def test_wizard_steps(qtbot, themes, db, admin, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "backup_password", lambda: PASSWORD)
    ctx = AppContext(db, DbConfig(), Settings(backup_dir=str(tmp_path)), themes, admin)
    wizard = YearEndWizard(ctx)
    qtbot.addWidget(wizard)
    assert await wait_until(lambda: "مانعی" in wizard.check_label.text())
    assert wizard.next_button.isEnabled()
    wizard.year.setValue(1405)  # current year: not finished
    assert await wait_until(lambda: "پایان نرسیده" in wizard.check_label.text())
    assert not wizard.next_button.isEnabled()
    wizard.year.setValue(1404)
    await wait_until(lambda: wizard.next_button.isEnabled())
    await wizard.on_next()
    wizard.fresh_mode.setChecked(True)
    assert not wizard.carry_items.isEnabled()
    assert wizard.options() == CloseOptions.fresh_start()
    await wizard.on_next()
    assert "تعیین شده" in wizard.backup_state.text()
    await wizard.on_next()
    assert "شروع خالی" in wizard.summary.text() and wizard.next_button.text() == "بستن سال مالی"


async def test_reports_can_read_closed_year_archive(qtbot, themes, db, admin, tmp_path):
    unit = (await master.list_units(db))[0].id
    wh = (await master.list_warehouses(db))[0].id
    item = await items.create_item(db, admin, ItemInput("A", "دریل", unit))
    await docs.create_and_post(db, admin, docs.DocumentInput(
        DocType.RECEIPT, dt.date(2025, 9, 1), wh, [docs.LineInput(item, unit, Decimal(5))]))
    approval = await protected.approve(db, admin, protected.ProtectedAction.CLOSE_FISCAL_YEAR,
                                       "admin", "4826")
    await fiscal.run_year_end(db, admin, 1404, CloseOptions(), approval,
                              backup.SqliteDumper(db.url.database), PASSWORD, tmp_path)
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    tab = FiscalTab(ctx)
    qtbot.addWidget(tab)
    await tab.refresh()
    assert tab.table.rowCount() == 1 and "۱۴۰۴" in tab.state_label.text()

    page = ReportsPage(ctx)
    qtbot.addWidget(page)
    await page.load_filters()
    assert page.year.count() == 2
    page.year.setCurrentIndex(1)
    assert not page.archive_note.isHidden()
    page.cardex_search.setText("A")
    await page.on_cardex_search()
    await wait_until(lambda: page.cardex.report is not None)
    # In the archive the item's history is the original 1404 receipt, not the opening document.
    assert any(row[1] == "رسید ورود" for row in page.cardex.report.rows)
