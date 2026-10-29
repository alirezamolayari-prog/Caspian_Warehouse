"""4.9: the backup tab warns (system drive, low space, daily backup off, last backup too old) with a
button to the setting concerned, and never changes a setting by itself."""

import datetime as dt

from caspian.services.backup import backup_warnings

NOW = dt.datetime(2026, 10, 29, 10, 0)
GB = 1024 ** 3


def _keys(**kwargs) -> list[str]:
    args = {"folder": r"D:\Backups", "daily_enabled": True, "newest": NOW - dt.timedelta(days=1),
            "now": NOW, "system_drive": "C:", "free_bytes": 50 * GB, **kwargs}
    return [w.key for w in backup_warnings(**args)]


def test_all_good_means_no_warning():
    assert _keys() == []


def test_each_problem_is_named():
    assert _keys(folder=r"C:\Users\x\Documents\Caspian Backups") == ["system_drive"]
    assert _keys(free_bytes=2 * GB) == ["low_space"]
    assert _keys(daily_enabled=False) == ["daily_off"]
    assert _keys(newest=NOW - dt.timedelta(days=8)) == ["old"]
    assert _keys(newest=None) == ["old"]


def test_texts_are_persian():
    texts = [w.text for w in backup_warnings(r"C:\b", False, None, NOW, "C:", 1 * GB)]
    assert len(texts) == 4
    assert all(not any("a" <= ch.lower() <= "z" for ch in t.replace("C:", "")) for t in texts)


async def test_tab_shows_warnings_with_buttons_and_changes_nothing(qtbot, themes, db, admin, tmp_path):
    from PySide6.QtWidgets import QPushButton

    from caspian.core.settings import Settings
    from caspian.db.database import DbConfig
    from caspian.services import scheduler
    from caspian.ui.app_context import AppContext
    from caspian.ui.backup_settings import BackupTab
    from helpers import wait_until

    settings = Settings()
    settings.backup_dir = str(tmp_path)
    tab = BackupTab(AppContext(db, DbConfig(), settings, themes, admin))
    qtbot.addWidget(tab)
    tab.show()
    assert await wait_until(lambda: tab.warnings.isVisible())
    buttons = [b.text() for b in tab.warnings.findChildren(QPushButton)]
    assert "رفتن به تنظیم روزانه" in buttons and "رفتن به «پشتیبان‌گیری اکنون»" in buttons
    assert (await scheduler.daily_backup(db))[0] is False  # warned, not switched on
