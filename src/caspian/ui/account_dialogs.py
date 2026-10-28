"""Signing in with a personal Caspian Server account: one large RTL window with simple steps.

ورود (mobile + password) · ثبت‌نام (name + mobile → SMS code → password → waiting for approval) ·
رمز را فراموش کرده‌ام (mobile → code → new password) · تغییر اجباری رمز. Every server answer is one
Persian sentence (services.server_link.accounts); the services do the work, this only shows steps.
"""

import re

import httpx
from PySide6.QtCore import QLocale, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian import APP_DISPLAY_NAME
from caspian.core.persian import (
    clean_person_name,
    name_problem,
    normalize_mobile,
    password_problem,
    password_rules,
)
from caspian.core.text import to_ascii_digits, to_persian_digits
from caspian.db.database import Database
from caspian.services.errors import ServiceError
from caspian.services.server_link import session
from caspian.services.server_link.accounts import REGISTER, RESET, AuthApi, AuthError
from caspian.services.server_link.session import MustChange, PersonLogin
from caspian.ui.logo import LogoLabel

PENDING_TEXT = "ثبت‌نام انجام شد. حساب شما پس از تأیید مدیر فعال می‌شود."
REJECTED_TEXT = "مدیر سرور این حساب را تأیید نکرده است. اگر فکر می‌کنید اشتباه شده، با مدیر تماس بگیرید."
MOBILE_HINT = "مثلاً ۰۹۱۲۳۴۵۶۷۸۹"
_CODE_DIGITS = re.compile(r"\D")


def _ltr(edit: QLineEdit) -> QLineEdit:
    edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    edit.setLocale(QLocale.c())
    return edit


def _password(placeholder: str = "") -> QLineEdit:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.setPlaceholderText(placeholder)
    return edit


def _show_toggle(*fields: QLineEdit) -> QCheckBox:
    box = QCheckBox("نمایش رمز")
    box.toggled.connect(lambda on: [f.setEchoMode(QLineEdit.EchoMode.Normal if on
                                                  else QLineEdit.EchoMode.Password) for f in fields])
    return box


def _clock(seconds: int) -> str:
    return to_persian_digits(f"{seconds // 60}:{seconds % 60:02d}")


class _Page(QWidget):
    """Title, a short explanation, a form, an error line (next to the fields) and buttons."""

    def __init__(self, title: str, subtitle: str = "") -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(QLabel(title, objectName="PageTitle"))
        self.subtitle = QLabel(subtitle, objectName="Muted")
        self.subtitle.setWordWrap(True)
        self.subtitle.setVisible(bool(subtitle))
        layout.addWidget(self.subtitle)
        self.form = QFormLayout()
        self.form.setSpacing(10)
        layout.addLayout(self.form)
        self.extra = QVBoxLayout()
        layout.addLayout(self.extra)
        self.status = QLabel(objectName="StatusText")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.hide()
        layout.addWidget(self.status)
        layout.addStretch(1)
        self.buttons = QHBoxLayout()
        layout.addLayout(self.buttons)
        self.links = QHBoxLayout()
        layout.addLayout(self.links)

    def say(self, text: str, is_error: bool = True) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.status.setProperty("error", is_error)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def primary(self, text: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("variant", "primary")
        button.setDefault(True)
        button.clicked.connect(slot)
        self.buttons.addStretch(1)
        self.buttons.addWidget(button)
        return button

    def secondary(self, text: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(slot)
        self.buttons.insertWidget(0, button)
        return button

    def link(self, text: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("variant", "link")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(slot)
        self.links.addWidget(button)
        return button


class _Rules(QWidget):
    """The password rules, ticked live while typing (✓ green / • grey, never colour alone)."""

    def __init__(self, field: QLineEdit) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._labels = [QLabel() for _ in password_rules("")]
        for label in self._labels:
            layout.addWidget(label)
        field.textChanged.connect(self.update_rules)
        self.update_rules("")

    def update_rules(self, text: str) -> None:
        for label, (rule, ok) in zip(self._labels, password_rules(text), strict=True):
            label.setText(f"{'✓' if ok else '•'} {rule}")
            label.setObjectName("RuleOk" if ok else "RuleBad")
            label.style().unpolish(label)
            label.style().polish(label)


class AccountWindow(QDialog):
    def __init__(self, db: Database, message: str = "", cancel_text: str = "خروج",
                 transport: httpx.AsyncBaseTransport | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AccountWindow")
        self.setWindowTitle(f"‏ورود به {APP_DISPLAY_NAME}")
        self.setMinimumSize(480, 520)
        self._db, self._transport = db, transport
        self.result_login: PersonLogin | None = None
        self._purpose = REGISTER
        self._mobile = ""
        self._first = self._last = ""
        self._verified = ""
        self._pending: MustChange | None = None
        self._expires = self._resend = 0
        self._timer = QTimer(self, interval=1000)
        self._timer.timeout.connect(self._tick)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 20)
        logo = LogoLabel(64)
        if logo.has_logo:
            outer.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self._build_login(message, cancel_text)
        self._build_name()
        self._build_code()
        self._build_password()
        self._build_forgot()
        self._build_change()
        self._build_done()
        for page in (self.login_page, self.name_page, self.code_page, self.password_page,
                     self.forgot_page, self.change_page, self.done_page):
            self.stack.addWidget(page)
        self.go(self.login_page)

    # ----- pages -----

    def _build_login(self, message: str, cancel_text: str) -> None:
        page = self.login_page = _Page("ورود", "با شماره موبایل و رمز عبور خودتان وارد شوید.")
        self.login_mobile = _ltr(QLineEdit(placeholderText=MOBILE_HINT))
        self.login_password = _password()
        page.form.addRow("شماره موبایل:", self.login_mobile)
        page.form.addRow("رمز عبور:", self.login_password)
        page.form.addRow("", _show_toggle(self.login_password))
        self.remember = QCheckBox("مرا روی این کامپیوتر به خاطر بسپار")
        page.form.addRow("", self.remember)
        self.cancel_button = page.secondary(cancel_text, self.reject)
        self.login_button = page.primary("ورود", self.on_login)
        page.link("ثبت‌نام", lambda: self.go(self.name_page))
        page.link("رمز را فراموش کرده‌ام", lambda: self.go(self.forgot_page))
        if message:
            page.say(message)

    def _build_name(self) -> None:
        page = self.name_page = _Page(
            "ثبت‌نام — مرحلهٔ ۱ از ۳",
            "نام و نام خانوادگی را فارسی بنویسید. یک کد ۶ رقمی برایتان پیامک می‌شود.")
        self.first_name = QLineEdit()
        self.last_name = QLineEdit()
        self.reg_mobile = _ltr(QLineEdit(placeholderText=MOBILE_HINT))
        page.form.addRow("نام:", self.first_name)
        page.form.addRow("نام خانوادگی:", self.last_name)
        page.form.addRow("شماره موبایل:", self.reg_mobile)
        page.secondary("بازگشت", lambda: self.go(self.login_page))
        self.send_button = page.primary("دریافت کد", self.on_send_code)

    def _build_code(self) -> None:
        page = self.code_page = _Page("ثبت‌نام — مرحلهٔ ۲ از ۳: کد پیامک", "")
        self.code = _ltr(QLineEdit(placeholderText="۶ رقم"))
        self.code.setMaxLength(12)  # paste with spaces or Persian digits is fine
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        page.form.addRow("کد:", self.code)
        self.countdown = QLabel(objectName="Muted")
        page.extra.addWidget(self.countdown)
        page.secondary("ویرایش شماره", self.on_edit_mobile)
        self.verify_button = page.primary("تأیید کد", self.on_verify)
        self.resend_button = page.link("ارسال دوباره", self.on_resend)

    def _build_password(self) -> None:
        page = self.password_page = _Page("انتخاب رمز عبور", "رمزی انتخاب کنید که فقط خودتان می‌دانید.")
        self.new_password = _password()
        self.repeat_password = _password()
        page.form.addRow("رمز عبور:", self.new_password)
        page.form.addRow("تکرار رمز:", self.repeat_password)
        page.form.addRow("", _show_toggle(self.new_password, self.repeat_password))
        page.extra.addWidget(_Rules(self.new_password))
        page.secondary("انصراف", lambda: self.go(self.login_page))
        self.password_button = page.primary("ثبت‌نام", self.on_password)

    def _build_forgot(self) -> None:
        page = self.forgot_page = _Page("رمز را فراموش کرده‌ام",
                                        "شمارهٔ موبایل حساب را بنویسید تا کد پیامک شود.")
        self.forgot_mobile = _ltr(QLineEdit(placeholderText=MOBILE_HINT))
        page.form.addRow("شماره موبایل:", self.forgot_mobile)
        page.secondary("بازگشت", lambda: self.go(self.login_page))
        self.forgot_button = page.primary("دریافت کد", self.on_forgot)

    def _build_change(self) -> None:
        page = self.change_page = _Page("تغییر رمز", "پیش از ادامه باید رمز عبور را عوض کنید.")
        self.change_current = _password()
        self.change_new = _password()
        self.change_repeat = _password()
        page.form.addRow("رمز فعلی:", self.change_current)
        page.form.addRow("رمز تازه:", self.change_new)
        page.form.addRow("تکرار رمز تازه:", self.change_repeat)
        page.form.addRow("", _show_toggle(self.change_current, self.change_new, self.change_repeat))
        page.extra.addWidget(_Rules(self.change_new))
        page.secondary("انصراف", lambda: self.go(self.login_page))
        self.change_button = page.primary("ذخیره و ورود", self.on_change)

    def _build_done(self) -> None:
        page = self.done_page = _Page("ثبت شد", PENDING_TEXT)
        page.primary("بازگشت به ورود", lambda: self.go(self.login_page))

    def go(self, page: _Page) -> None:
        page.say("")
        self.stack.setCurrentWidget(page)
        first = page.findChild(QLineEdit)
        if first is not None:
            QTimer.singleShot(0, first.setFocus)

    def _busy(self, page: _Page, busy: bool) -> None:
        for button in page.findChildren(QPushButton):
            button.setEnabled(not busy)
        if not busy and page is self.code_page:
            self._tick(update_only=True)

    async def _run(self, page: _Page, work) -> bool:
        """Run one server step; an error stays on the page next to the fields."""
        self._busy(page, True)
        try:
            await work()
            return True
        except AuthError as exc:
            page.say(exc.message)
            if exc.code in ("account_pending", "account_rejected"):
                self.done_page.subtitle.setText(PENDING_TEXT if exc.code == "account_pending"
                                                else REJECTED_TEXT)
                self.go(self.done_page)
            return False
        except ServiceError as exc:
            page.say(exc.message)
            return False
        finally:
            self._busy(page, False)

    # ----- login -----

    @asyncSlot()
    async def on_login(self) -> None:
        page = self.login_page
        login, password = self.login_mobile.text().strip(), self.login_password.text()
        if not login or not password:
            page.say("شماره موبایل و رمز عبور را وارد کنید.")
            return

        async def work() -> None:
            result = await session.sign_in(self._db, login, password, self.remember.isChecked(),
                                           self._transport)
            if isinstance(result, MustChange):
                self._pending = result
                self.change_current.setText(password)
                self.go(self.change_page)
                return
            self.result_login = result
            self.accept()

        if not await self._run(page, work):
            self.login_password.clear()
            self.login_password.setFocus()

    # ----- register / forgot: mobile → code -----

    @asyncSlot()
    async def on_send_code(self) -> None:
        page = self.name_page
        first, last = clean_person_name(self.first_name.text()), clean_person_name(self.last_name.text())
        mobile = normalize_mobile(self.reg_mobile.text())
        if problem := name_problem(first) or name_problem(last):
            page.say(problem)
            return
        if mobile is None:
            page.say("شماره موبایل درست نیست؛ " + MOBILE_HINT + " بنویسید.")
            return
        self._first, self._last = first, last
        self.first_name.setText(first)
        self.last_name.setText(last)
        await self._request_code(page, mobile, REGISTER)

    @asyncSlot()
    async def on_forgot(self) -> None:
        mobile = normalize_mobile(self.forgot_mobile.text())
        if mobile is None:
            self.forgot_page.say("شماره موبایل درست نیست؛ " + MOBILE_HINT + " بنویسید.")
            return
        await self._request_code(self.forgot_page, mobile, RESET)

    async def _request_code(self, page: _Page, mobile: str, purpose: str) -> None:
        api = AuthApi(await session.server_url(self._db), self._transport)

        async def work() -> None:
            sent = await api.request_code(mobile, purpose)
            self._mobile, self._purpose = mobile, purpose
            self._expires, self._resend = sent.expires_in, sent.resend_after
            self.code_page.findChild(QLabel, "PageTitle").setText(
                "ثبت‌نام — مرحلهٔ ۲ از ۳: کد پیامک" if purpose == REGISTER else "کد پیامک")
            self.code_page.subtitle.setText(
                f"کد ۶ رقمی به شمارهٔ {to_persian_digits(mobile)} پیامک شد. آن را اینجا بنویسید.")
            self.code_page.subtitle.show()
            self.code.clear()
            self._timer.start()
            self._tick(update_only=True)
            self.go(self.code_page)

        await self._run(page, work)

    def _tick(self, update_only: bool = False) -> None:
        if not update_only:
            self._expires = max(0, self._expires - 1)
            self._resend = max(0, self._resend - 1)
        if self._expires:
            self.countdown.setText(f"اعتبار کد: {_clock(self._expires)}")
        else:
            self.countdown.setText("زمان کد تمام شد؛ «ارسال دوباره» را بزنید.")
        self.resend_button.setEnabled(self._resend == 0)
        self.resend_button.setText("ارسال دوباره" if self._resend == 0
                                   else f"ارسال دوباره ({_clock(self._resend)})")

    @asyncSlot()
    async def on_resend(self) -> None:
        if self._resend == 0 and self._mobile:
            await self._request_code(self.code_page, self._mobile, self._purpose)

    def on_edit_mobile(self) -> None:
        self._timer.stop()
        self.go(self.name_page if self._purpose == REGISTER else self.forgot_page)

    @asyncSlot()
    async def on_verify(self) -> None:
        page = self.code_page
        code = _CODE_DIGITS.sub("", to_ascii_digits(self.code.text()))
        if len(code) != 6:
            page.say("کد ۶ رقمی پیامک را کامل وارد کنید.")
            return
        api = AuthApi(await session.server_url(self._db), self._transport)

        async def work() -> None:
            self._verified = await api.verify_code(self._mobile, code, self._purpose)
            self._timer.stop()
            register = self._purpose == REGISTER
            self.password_page.findChild(QLabel, "PageTitle").setText(
                "ثبت‌نام — مرحلهٔ ۳ از ۳: رمز عبور" if register else "رمز تازه")
            self.password_button.setText("ثبت‌نام" if register else "ذخیره رمز تازه")
            self.new_password.clear()
            self.repeat_password.clear()
            self.go(self.password_page)

        await self._run(page, work)

    @asyncSlot()
    async def on_password(self) -> None:
        page = self.password_page
        password = self.new_password.text()
        if problem := password_problem(password):
            page.say(problem)
            return
        if password != self.repeat_password.text():
            page.say("رمز و تکرار آن یکی نیستند.")
            return
        api = AuthApi(await session.server_url(self._db), self._transport)

        async def work() -> None:
            if self._purpose == REGISTER:
                await api.register(self._verified, self._first, self._last, password)
                self.done_page.subtitle.setText(PENDING_TEXT)
                self.go(self.done_page)
            else:
                await api.reset_password(self._verified, password)
                self.login_mobile.setText(self._mobile)
                self.login_password.clear()
                self.go(self.login_page)
                self.login_page.say("رمز تازه ذخیره شد. حالا با رمز تازه وارد شوید.", is_error=False)

        if not await self._run(page, work) and page.status.text().startswith("زمان این مرحله"):
            self.go(self.name_page if self._purpose == REGISTER else self.forgot_page)

    # ----- forced change -----

    @asyncSlot()
    async def on_change(self) -> None:
        page = self.change_page
        new = self.change_new.text()
        if problem := password_problem(new):
            page.say(problem)
            return
        if new != self.change_repeat.text():
            page.say("رمز تازه و تکرار آن یکی نیستند.")
            return
        if new == self.change_current.text():
            page.say("رمز تازه باید با رمز فعلی فرق داشته باشد.")
            return

        async def work() -> None:
            self.result_login = await session.change_and_sign_in(
                self._db, self._pending, self.change_current.text(), new, self.remember.isChecked(),
                self._transport)
            self.accept()

        await self._run(page, work)


class ServerPasswordDialog(QDialog):
    """«تغییر رمز» from the user menu for someone with a server account."""

    def __init__(self, db: Database, actor, transport: httpx.AsyncBaseTransport | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AccountWindow")
        self.setWindowTitle("‏تغییر رمز")
        self._db, self._actor, self._transport = db, actor, transport
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        self.page = page = _Page("تغییر رمز", "رمز حساب سرور شما (برای همهٔ دستگاه‌ها) عوض می‌شود.")
        self.current, self.new, self.repeat = _password(), _password(), _password()
        page.form.addRow("رمز فعلی:", self.current)
        page.form.addRow("رمز تازه:", self.new)
        page.form.addRow("تکرار رمز تازه:", self.repeat)
        page.form.addRow("", _show_toggle(self.current, self.new, self.repeat))
        page.extra.addWidget(_Rules(self.new))
        page.secondary("انصراف", self.reject)
        page.primary("ذخیره", self.on_save)
        layout.addWidget(page)

    @asyncSlot()
    async def on_save(self) -> None:
        new = self.new.text()
        if problem := password_problem(new):
            self.page.say(problem)
            return
        if new != self.repeat.text():
            self.page.say("رمز تازه و تکرار آن یکی نیستند.")
            return
        try:
            await session.change_password(self._db, self._actor, self.current.text(), new, self._transport)
        except ServiceError as exc:
            self.page.say(exc.message)
            return
        self.accept()
