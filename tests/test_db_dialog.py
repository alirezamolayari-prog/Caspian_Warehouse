from caspian.db.database import DbConfig
from caspian.ui.db_setup_dialog import DbSetupDialog


def test_local_mode_locks_host(qtbot):
    dlg = DbSetupDialog(DbConfig())
    qtbot.addWidget(dlg)
    assert dlg.local_radio.isChecked()
    assert not dlg.host_edit.isEnabled()
    assert dlg.current_config().host == "localhost"


def test_network_config(qtbot):
    dlg = DbSetupDialog(DbConfig(host="192.168.1.20", port=3307, name="anbar", user="u1"))
    qtbot.addWidget(dlg)
    assert dlg.network_radio.isChecked()
    assert dlg.host_edit.isEnabled()
    assert dlg.current_config() == DbConfig(host="192.168.1.20", port=3307, name="anbar", user="u1")
    dlg.local_radio.setChecked(True)
    assert dlg.current_config().host == "localhost"


def test_initial_error_is_flagged(qtbot):
    dlg = DbSetupDialog(DbConfig(), error="خطا")
    qtbot.addWidget(dlg)
    assert dlg.status.text() == "خطا"
    assert dlg.status.property("error") is True
