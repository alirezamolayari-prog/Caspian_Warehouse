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


async def test_without_password_backups_are_plain_with_a_warning(qtbot, themes, db, admin, tmp_path,
                                                                 monkeypatch):
    """#33: no password used to mean no backup at all; now an unencrypted one, clearly flagged."""
    from caspian.ui import backup_settings

    monkeypatch.setattr(backup, "backup_password", lambda: None)
    infos = []
    monkeypatch.setattr(backup_settings, "show_info", lambda parent, text: infos.append(text))
    ctx = AppContext(db, DbConfig(), Settings(backup_dir=str(tmp_path / "bk")), themes, admin)
    tab = BackupTab(ctx)
    qtbot.addWidget(tab)
    assert not tab.plain_warning.isHidden() and "بدون رمز" in tab.password_status.text()
    await tab.on_backup()
    tab.refresh()
    assert tab.table.rowCount() == 1 and tab.table.item(0, 3).text() == "بدون رمز"
    assert "بدون رمز" in infos[-1]
    [info] = backup.list_backups(tmp_path / "bk")
    assert await tab._ask_password(info) == ""  # no password dialog for a plain file


async def test_first_run_prompt_once_and_skip_is_remembered(qtbot, themes, db, admin, monkeypatch):
    import asyncio

    from PySide6.QtWidgets import QApplication

    from caspian.ui import backup_settings
    from caspian.ui.backup_settings import FirstBackupPasswordDialog, first_run_backup_prompt
    from helpers import wait_until

    monkeypatch.setattr(backup, "backup_password", lambda: None)
    settings = Settings()
    ctx = AppContext(db, DbConfig(), settings, themes, admin)
    task = asyncio.ensure_future(first_run_backup_prompt(ctx))

    def dialog():
        return next((w for w in QApplication.topLevelWidgets()
                     if isinstance(w, FirstBackupPasswordDialog) and w.isVisible()), None)

    assert await wait_until(lambda: dialog() is not None)
    assert dialog().cancel_button.text() == "بعداً، بدون رمز"
    dialog().cancel_button.click()  # «بعداً، بدون رمز»
    assert await task is True
    assert Settings.load().backup_password_prompted  # saved to (the isolated) settings.json
    assert await first_run_backup_prompt(ctx) is False  # not asked again

    stored = {}
    monkeypatch.setattr(backup_settings.backup, "set_backup_password", lambda pw: stored.update(pw=pw))
    fresh = AppContext(db, DbConfig(), Settings(), themes, admin)
    task = asyncio.ensure_future(first_run_backup_prompt(fresh))
    assert await wait_until(lambda: dialog() is not None)
    dialog().password.setText("Backup#Pass2026")
    dialog().repeat.setText("Backup#Pass2026")
    dialog().submit_button.click()
    assert await task is True and stored == {"pw": "Backup#Pass2026"}


async def test_restore_closes_the_window_instead_of_quitting_the_loop(qtbot, themes, db, admin, tmp_path,
                                                                       monkeypatch):
    from PySide6.QtWidgets import QApplication, QMainWindow

    from caspian.ui import backup_settings

    monkeypatch.setattr(backup, "backup_password", lambda: "Backup#Pass2026")
    ctx = AppContext(db, DbConfig(), Settings(backup_dir=str(tmp_path / "bk")), themes, admin)
    window = QMainWindow()
    qtbot.addWidget(window)
    tab = BackupTab(ctx)
    window.setCentralWidget(tab)
    window.show()
    await tab.on_backup()
    tab.refresh()
    tab.table.selectRow(0)
    restored, quits = [], []

    async def fake_restore(*args, **kwargs):
        restored.append(args[3])

    async def approve(*args, **kwargs):
        return object()

    async def ok(dialog):
        return 1

    async def ask(info):
        return ""

    monkeypatch.setattr(backup_settings, "request_approval", approve)
    monkeypatch.setattr(backup_settings, "exec_dialog", ok)
    monkeypatch.setattr(backup_settings.backup, "restore_backup", fake_restore)
    monkeypatch.setattr(tab, "_ask_password", ask)
    monkeypatch.setattr(QApplication, "quit", lambda *a: quits.append(1))
    await tab.on_restore()
    assert restored and not window.isVisible() and quits == []
