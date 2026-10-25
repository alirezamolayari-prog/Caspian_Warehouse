"""First run on a new PC: the database dialog explains in Persian what single-PC and network mode
mean and where MariaDB comes from (bundled by the installer, or a download link)."""

from caspian.ui.db_setup_dialog import DbSetupDialog


def test_mode_guidance_follows_the_choice(qtbot):
    dialog = DbSetupDialog()
    qtbot.addWidget(dialog)
    dialog.local_radio.setChecked(True)
    local = dialog.mode_help.text()
    assert "همین رایانه" in local and "نصب جدید" in local
    dialog.network_radio.setChecked(True)
    network = dialog.mode_help.text()
    assert "رایانه‌ی اصلی" in network and "IP" in network and network != local


def test_mariadb_download_link(qtbot):
    dialog = DbSetupDialog()
    qtbot.addWidget(dialog)
    assert "https://mariadb.org/download/" in dialog.mariadb_link.text()
    assert dialog.mariadb_link.openExternalLinks()
