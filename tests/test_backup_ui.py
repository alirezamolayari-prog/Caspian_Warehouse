from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.services import backup
from caspian.ui.app_context import AppContext
from caspian.ui.backup_settings import BackupTab


async def test_backup_now_lists_file(qtbot, themes, db, admin, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "backup_password", lambda: "Backup#Pass2026")
    settings = Settings(backup_dir=str(tmp_path / "bk"), backup_keep=5)
    ctx = AppContext(db, DbConfig(), settings, themes, admin)
    tab = BackupTab(ctx)
    qtbot.addWidget(tab)
    assert tab.folder.text() == str(tmp_path / "bk")
    assert "تعیین شده" in tab.password_status.text()
    await tab.on_backup()
    tab.refresh()
    assert tab.table.rowCount() == 1
    assert tab.table.item(0, 2).text() == "دستی"
    tab.table.selectRow(0)
    assert tab.verify_button.isEnabled() and not tab.restore_button.isHidden()
