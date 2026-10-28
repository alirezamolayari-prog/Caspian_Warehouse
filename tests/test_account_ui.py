"""Phase 2.1: the sign-in window — login, registration with an SMS code, forgotten password and a
forced password change — large, Persian, and every server answer shown as a Persian sentence."""

import re

from PySide6.QtWidgets import QLabel, QPushButton

from caspian.ui.account_dialogs import PENDING_TEXT, AccountWindow
from helpers import wait_until

MOBILE = "09121234567"


def _window(qtbot, db, fake_server) -> AccountWindow:
    window = AccountWindow(db, transport=fake_server.transport())
    qtbot.addWidget(window)
    window.show()
    return window


async def test_login_with_mobile_and_password(qtbot, db, fake_server):
    fake_server.add_person(MOBILE, "anbar-1403", "رضا انباردار")
    window = _window(qtbot, db, fake_server)
    window.login_mobile.setText("0912 123 4567")
    window.login_password.setText("anbar-1403")
    window.on_login()
    assert await wait_until(lambda: window.result_login is not None)
    assert window.result_login.actor.display_name == "رضا انباردار"


async def test_wrong_password_is_said_in_persian_and_cleared(qtbot, db, fake_server):
    fake_server.add_person(MOBILE, "anbar-1403", "رضا انباردار")
    window = _window(qtbot, db, fake_server)
    window.login_mobile.setText(MOBILE)
    window.login_password.setText("nope-nope")
    window.on_login()
    page = window.login_page
    assert await wait_until(lambda: page.status.isVisible())
    assert page.status.text() == "شماره موبایل یا رمز عبور اشتباه است."
    assert window.login_password.text() == "" and window.result_login is None


async def test_register_three_steps_then_waiting_for_approval(qtbot, db, fake_server):
    window = _window(qtbot, db, fake_server)
    window.go(window.name_page)
    window.first_name.setText("عَلي")
    window.last_name.setText("اسماعیل‌زاده")
    window.reg_mobile.setText("+98 912 123 4567")
    window.on_send_code()
    assert await wait_until(lambda: window.stack.currentWidget() is window.code_page)
    assert window.first_name.text() == "علی"  # cleaned like the server does
    assert not window.resend_button.isEnabled() and "اعتبار کد" in window.countdown.text()
    assert not re.search(r"[0-9]", window.countdown.text())  # Persian digits
    window.code.setText("۴۸۲ ۹۱۳")  # Persian digits and a space (pasted) are fine
    window.on_verify()
    assert await wait_until(lambda: window.stack.currentWidget() is window.password_page)
    window.new_password.setText("anbar-1403")
    window.repeat_password.setText("anbar-1403")
    window.on_password()
    assert await wait_until(lambda: window.stack.currentWidget() is window.done_page)
    assert window.done_page.subtitle.text() == PENDING_TEXT
    account = fake_server.users["m09121234567"]
    assert account["full_name"] == "علی اسماعیل‌زاده" and account["approval"] == "pending"

    window.go(window.login_page)  # login while pending shows the same message
    window.login_mobile.setText(MOBILE)
    window.login_password.setText("anbar-1403")
    window.on_login()
    assert await wait_until(lambda: window.stack.currentWidget() is window.done_page)


async def test_names_are_checked_before_any_sms(qtbot, db, fake_server):
    window = _window(qtbot, db, fake_server)
    window.go(window.name_page)
    window.first_name.setText("Ali")
    window.last_name.setText("رضایی")
    window.reg_mobile.setText(MOBILE)
    window.on_send_code()
    assert await wait_until(lambda: window.name_page.status.isVisible())
    assert "حروف فارسی" in window.name_page.status.text() and fake_server.sms == []
    window.first_name.setText("علی")
    window.reg_mobile.setText("12345")
    window.on_send_code()
    assert await wait_until(lambda: "موبایل" in window.name_page.status.text())
    assert fake_server.sms == []


async def test_password_mismatch_and_weak_password(qtbot, db, fake_server):
    window = _window(qtbot, db, fake_server)
    window.go(window.password_page)
    window.new_password.setText("12345678")
    window.repeat_password.setText("12345678")
    window.on_password()
    assert await wait_until(lambda: window.password_page.status.isVisible())
    assert "رایج" in window.password_page.status.text()
    window.new_password.setText("anbar-1403")
    window.repeat_password.setText("anbar-1404")
    window.on_password()
    assert await wait_until(lambda: "یکی نیستند" in window.password_page.status.text())


async def test_forgot_password_back_to_login(qtbot, db, fake_server):
    fake_server.add_person(MOBILE, "old-pass-99", "رضا انباردار")
    window = _window(qtbot, db, fake_server)
    window.go(window.forgot_page)
    window.forgot_mobile.setText(MOBILE)
    window.on_forgot()
    assert await wait_until(lambda: window.stack.currentWidget() is window.code_page)
    window.code.setText("482913")
    window.on_verify()
    assert await wait_until(lambda: window.stack.currentWidget() is window.password_page)
    window.new_password.setText("new-pass-77")
    window.repeat_password.setText("new-pass-77")
    window.on_password()
    assert await wait_until(lambda: window.stack.currentWidget() is window.login_page)
    assert window.login_mobile.text() == MOBILE and "رمز تازه" in window.login_page.status.text()
    assert fake_server.users["m09121234567"]["password"] == "new-pass-77"


async def test_must_change_password_then_continue(qtbot, db, fake_server):
    name = fake_server.add_person(MOBILE, "temp-pass-11", "رضا انباردار")
    fake_server.users[name]["must_change"] = True
    window = _window(qtbot, db, fake_server)
    window.login_mobile.setText(MOBILE)
    window.login_password.setText("temp-pass-11")
    window.on_login()
    assert await wait_until(lambda: window.stack.currentWidget() is window.change_page)
    window.change_new.setText("mine-pass-55")
    window.change_repeat.setText("mine-pass-55")
    window.on_change()
    assert await wait_until(lambda: window.result_login is not None)
    assert fake_server.users[name]["password"] == "mine-pass-55"


async def test_window_text_is_persian(qtbot, db, fake_server):
    window = _window(qtbot, db, fake_server)
    texts = [w.text() for w in window.findChildren(QLabel) + window.findChildren(QPushButton)]
    assert all(not re.search(r"[A-Za-z]", t) for t in texts), texts
