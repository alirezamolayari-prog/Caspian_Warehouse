"""Bug list round 2 #9: the backup list shows the app version with Persian digits."""

import re

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.services import backup
from caspian.ui.app_context import AppContext
from caspian.ui.backup_settings import BackupTab


async def test_backup_version_column_is_persian(qtbot, themes, db, admin, tmp_path):
    folder = tmp_path / "backups"
    await backup.create_backup(db, admin, backup.make_dumper(db, None, ""), None, folder, label="test")
    settings = Settings()
    settings.backup_dir = str(folder)
    tab = BackupTab(AppContext(db, DbConfig(), settings, themes, admin))
    qtbot.addWidget(tab)
    tab.folder.setText(str(folder))
    tab.refresh()
    column = BackupTab.COLUMNS.index("نسخه برنامه")
    version = tab.table.item(0, column).text()
    assert version and not re.search("[0-9]", version)
