"""«محل‌های مصرف» admin tab (spec §2.7)."""

import openpyxl
import pytest

from caspian.core.settings import Settings
from caspian.db import location_seed
from caspian.db.database import DbConfig
from caspian.db.models import LocationType
from caspian.services import auth, locations, users
from caspian.ui import locations_tab
from caspian.ui.app_context import AppContext
from caspian.ui.locations_tab import CsvPreviewDialog, LocationDialog, LocationsTab
from helpers import wait_until


@pytest.fixture
def ctx(themes, db, admin):
    return AppContext(db, DbConfig(), Settings(), themes, admin)


async def test_list_filters_and_search(qtbot, ctx):
    tab = LocationsTab(ctx)
    qtbot.addWidget(tab)
    await tab.refresh()
    assert tab.table.rowCount() == 141
    tab.type_filter.setCurrentIndex(tab.type_filter.findData(LocationType.VERTICAL))
    await tab.refresh()
    assert tab.table.rowCount() == 9
    tab.type_filter.setCurrentIndex(0)
    tab.floor_filter.setCurrentIndex(tab.floor_filter.findText("طبقه همکف"))
    await tab.refresh()
    assert tab.table.rowCount() == 25  # plates 58–82
    tab.floor_filter.setCurrentIndex(0)
    tab.search.setText("AB-ASD")
    await tab.refresh()
    assert tab.table.rowCount() == 1


async def test_add_edit_and_deactivate(qtbot, ctx):
    tab = LocationsTab(ctx)
    qtbot.addWidget(tab)
    await tab.refresh()
    floors = await locations.list_floors(ctx.db)
    dialog = LocationDialog(ctx, floors, parent=tab)
    qtbot.addWidget(dialog)
    dialog.code.setText("LOBBY")
    dialog.name.setText("Lobby")
    dialog.floor.setCurrentIndex(dialog.floor.findText("طبقه همکف"))
    dialog.aliases.setText("lobby main; front")
    await dialog.submit()
    index = await locations.load_index(ctx.db)
    assert index.exact("front").code == "LOBBY" and index.find("LOBBY").floor.code == "G"

    dialog = LocationDialog(ctx, floors, index.find("LOBBY"), tab)
    qtbot.addWidget(dialog)
    dialog.type.setCurrentIndex(dialog.type.findData(LocationType.GENERAL))
    assert not dialog.floor.isEnabled()
    await dialog.submit()
    assert (await locations.load_index(ctx.db)).find("LOBBY").needs_floor

    await tab.refresh()
    tab.table.select_id(index.find("LOBBY").id)
    await tab.set_active(tab.selected(), False)
    await tab.refresh()
    assert not (await locations.load_index(ctx.db)).find("LOBBY").is_active
    assert tab.table.rowCount() == 142  # deactivated rows stay listed (muted)


async def test_csv_reimport_preview_and_apply(qtbot, ctx, tmp_path, monkeypatch):
    text = location_seed.seed_path().read_text(encoding="utf-8-sig")
    path = tmp_path / "locations.csv"
    path.write_text("﻿" + text + "LOBBY,Lobby,named,همکف,G,no,lobby,,\r\n", encoding="utf-8")
    shown = []

    async def fake_exec(dialog):
        if isinstance(dialog, CsvPreviewDialog):
            shown.append(dialog)
            assert dialog.table.rowCount() == 1 and dialog.submit_button.isEnabled()
            await dialog.submit()
            return 1
        return 0

    monkeypatch.setattr(locations_tab, "exec_dialog", fake_exec)
    monkeypatch.setattr(locations_tab, "show_info", lambda *a: None)
    tab = LocationsTab(ctx)
    qtbot.addWidget(tab)
    await tab.import_file(str(path))
    assert shown and shown[0].result_counts == (1, 0)
    assert (await locations.load_index(ctx.db)).exact("lobby").code == "LOBBY"

    bad = tmp_path / "bad.csv"
    bad.write_text(text + "X,x,named,هیچ,F99,no,,,\r\n", encoding="utf-8")

    async def check_errors(dialog):
        assert not dialog.submit_button.isEnabled() and "خطا" in dialog.status.text()
        return 0

    monkeypatch.setattr(locations_tab, "exec_dialog", check_errors)
    await tab.import_file(str(bad))


async def test_excel_export(qtbot, ctx, tmp_path, monkeypatch):
    target = tmp_path / "loc.xlsx"

    async def fake_save(*args, **kwargs):
        return str(target)

    monkeypatch.setattr(locations_tab, "ask_save_path", fake_save)
    monkeypatch.setattr(locations_tab, "show_info", lambda *a: None)
    tab = LocationsTab(ctx)
    qtbot.addWidget(tab)
    tab.on_excel()
    assert await wait_until(lambda: target.exists())
    sheet = openpyxl.load_workbook(target).active
    values = [c.value for row in sheet.iter_rows() for c in row]
    assert "AB-ASD" in values and "پلاک 68" in values


async def test_manager_sees_but_cannot_change(qtbot, themes, db, admin):
    await users.create_user(db, admin, "mgr", "مدیر انبار", "Mgr#2026pass", "manager")
    manager = (await auth.login(db, "mgr", "Mgr#2026pass")).actor
    tab = LocationsTab(AppContext(db, DbConfig(), Settings(), themes, manager))
    qtbot.addWidget(tab)
    await tab.refresh()
    tab.update_buttons()
    assert tab.table.rowCount() == 141
    hidden = (tab.new_button, tab.edit_button, tab.import_button, tab.settings_button)
    assert all(b.isHidden() for b in hidden)
    assert not tab.excel_button.isHidden()
