"""Dialog behaviour, driving the real services against a test database."""


import pytest

from caspian.services import auth, users
from caspian.services.protected import ProtectedAction, consume
from caspian.ui.auth_dialogs import (
    ApprovalDialog,
    ChangePasswordDialog,
    LoginDialog,
    SetPinDialog,
)
from helpers import settle

ADMIN_PW = "Str0ngPass"



@pytest.fixture
async def admin(db):
    actor = (await auth.login(db, "admin", "admin")).actor
    await auth.change_password(db, actor, "admin", ADMIN_PW)
    await auth.set_pin(db, actor, ADMIN_PW, "4826")
    return actor


async def test_login_dialog_success(db, qtbot):
    dlg = LoginDialog(db)
    qtbot.addWidget(dlg)
    dlg.username.setText("admin")
    dlg.password.setText("admin")
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.result() == dlg.DialogCode.Accepted
    assert dlg.result_login.must_change_password


async def test_login_dialog_error_stays_open(db, qtbot):
    dlg = LoginDialog(db)
    qtbot.addWidget(dlg)
    dlg.open()
    dlg.username.setText("admin")
    dlg.password.setText("wrong")
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.isVisible()
    assert "نادرست" in dlg.status.text()
    assert dlg.status.property("error") is True
    assert dlg.password.text() == ""


async def test_forced_password_change_dialog(db, qtbot):
    actor = (await auth.login(db, "admin", "admin")).actor
    dlg = ChangePasswordDialog(db, actor, forced=True)
    qtbot.addWidget(dlg)
    assert dlg.cancel_button.text() == "خروج"
    dlg.current.setText("admin")
    dlg.new.setText(ADMIN_PW)
    dlg.repeat.setText("different")
    dlg.submit_button.click()
    await settle(dlg)
    assert "یکسان نیستند" in dlg.status.text()
    dlg.repeat.setText(ADMIN_PW)
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.result() == dlg.DialogCode.Accepted
    assert not (await auth.login(db, "admin", ADMIN_PW)).must_change_password


async def test_set_pin_dialog(db, qtbot):
    actor = (await auth.login(db, "admin", "admin")).actor
    await auth.change_password(db, actor, "admin", ADMIN_PW)
    dlg = SetPinDialog(db, actor, forced=True)
    qtbot.addWidget(dlg)
    dlg.password.setText(ADMIN_PW)
    dlg.pin.setText("1234")
    dlg.repeat.setText("1234")
    dlg.submit_button.click()
    await settle(dlg)
    assert "ساده" in dlg.status.text()
    dlg.pin.setText("۴۸۲۶")
    dlg.repeat.setText("۴۸۲۶")
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.result() == dlg.DialogCode.Accepted
    assert not (await auth.login(db, "admin", ADMIN_PW)).needs_pin


async def test_approval_dialog(db, qtbot, admin):
    row = await users.create_user(db, admin, "ali", "", "Ali#2026x", "viewer")
    approvers = [("admin", "مدیر سیستم")]
    dlg = ApprovalDialog(db, admin, ProtectedAction.CHANGE_ROLE, "تغییر نقش علی", approvers)
    qtbot.addWidget(dlg)
    assert dlg.approver.currentData() == "admin"
    dlg.pin.setText("0000")
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.approval is None and "PIN" in dlg.status.text()
    dlg.pin.setText("4826")
    dlg.submit_button.click()
    await settle(dlg)
    assert dlg.approval is not None
    await users.change_role(db, admin, row.id, "manager", dlg.approval)
    with pytest.raises(Exception):  # noqa: B017 - approval already consumed
        consume(dlg.approval, ProtectedAction.CHANGE_ROLE, admin)
