"""Login, first-login setup, and admin approval (PIN) dialogs."""

from PySide6.QtWidgets import QComboBox, QFrame, QLabel, QVBoxLayout, QWidget

from caspian import APP_DISPLAY_NAME
from caspian.db.database import Database
from caspian.services import auth, protected
from caspian.services.actor import Actor
from caspian.services.auth import LoginResult
from caspian.services.errors import ValidationError
from caspian.services.protected import ACTION_INFO, Approval, ProtectedAction
from caspian.ui.app_context import exec_dialog
from caspian.ui.dialogs import FormDialog, ltr_field, password_field
from caspian.ui.messages import show_error


class LoginDialog(FormDialog):
    def __init__(self, db: Database, username: str = "", cancel_text: str = "خروج",
                 parent: QWidget | None = None) -> None:
        super().__init__(f"ورود به {APP_DISPLAY_NAME}", "نام کاربری و رمز عبور خود را وارد کنید.",
                         submit_text="ورود", cancel_text=cancel_text, parent=parent)
        self._db = db
        self.result_login: LoginResult | None = None
        self.username = self.add_row("نام کاربری:", ltr_field(username))
        self.password = self.add_row("رمز عبور:", password_field())
        (self.password if username else self.username).setFocus()

    async def submit(self) -> None:
        if not self.username.text().strip():
            raise ValidationError("نام کاربری را وارد کنید.")
        self.result_login = await auth.login(self._db, self.username.text(), self.password.text())

    def show_status(self, text: str, is_error: bool = True) -> None:
        super().show_status(text, is_error)
        if is_error:
            self.password.clear()
            self.password.setFocus()


class ChangePasswordDialog(FormDialog):
    def __init__(self, db: Database, actor: Actor, forced: bool = False,
                 parent: QWidget | None = None) -> None:
        subtitle = (
            "برای امنیت حساب، پیش از ادامه باید رمز عبور خود را تغییر دهید."
            if forced else "رمز عبور فعلی و رمز جدید را وارد کنید."
        )
        super().__init__("تغییر رمز عبور", subtitle, submit_text="ذخیره",
                         cancel_text="خروج" if forced else "انصراف", parent=parent)
        self._db, self._actor = db, actor
        self.current = self.add_row("رمز فعلی:", password_field())
        self.new = self.add_row("رمز جدید:", password_field("حداقل ۶ کاراکتر"))
        self.repeat = self.add_row("تکرار رمز جدید:", password_field())

    async def submit(self) -> None:
        if self.new.text() != self.repeat.text():
            raise ValidationError("رمز جدید و تکرار آن یکسان نیستند.")
        await auth.change_password(self._db, self._actor, self.current.text(), self.new.text())


class SetPinDialog(FormDialog):
    def __init__(self, db: Database, actor: Actor, forced: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(
            "تنظیم PIN مدیر",
            "عملیات حساس (حذف، ادغام، بازیابی پشتیبان، تغییر نقش، بستن سال مالی) "
            "فقط با PIN مدیر انجام می‌شود. یک PIN ۴ تا ۸ رقمی انتخاب کنید.",
            submit_text="ذخیره", cancel_text="خروج" if forced else "انصراف", parent=parent,
        )
        self._db, self._actor = db, actor
        self.password = self.add_row("رمز عبور شما:", password_field())
        self.pin = self.add_row("کد PIN جدید:", ltr_field())
        self.pin.setEchoMode(self.pin.EchoMode.Password)
        self.pin.setMaxLength(8)
        self.repeat = self.add_row("تکرار کد PIN:", ltr_field())
        self.repeat.setEchoMode(self.repeat.EchoMode.Password)
        self.repeat.setMaxLength(8)

    async def submit(self) -> None:
        if self.pin.text() != self.repeat.text():
            raise ValidationError("کد PIN و تکرار آن یکسان نیستند.")
        await auth.set_pin(self._db, self._actor, self.password.text(), self.pin.text())


class ApprovalDialog(FormDialog):
    """Confirmation + admin PIN for a protected action."""

    def __init__(self, db: Database, actor: Actor, action: ProtectedAction, description: str,
                 approvers: list[tuple[str, str]], parent: QWidget | None = None) -> None:
        title, _perm = ACTION_INFO[action]
        super().__init__(f"تأیید عملیات حساس: {title}", submit_text="تأیید و انجام",
                         parent=parent)
        self.submit_button.setProperty("variant", "danger")
        self._db, self._actor, self._action = db, actor, action
        self.approval: Approval | None = None

        warning = QFrame(objectName="WarningBox")
        box = QVBoxLayout(warning)
        box.setContentsMargins(14, 12, 14, 12)
        text = QLabel(description)
        text.setWordWrap(True)
        box.addWidget(text)
        box.addWidget(QLabel("این عملیات قابل بازگشت نیست. برای ادامه، یک مدیر باید PIN خود را "
                             "وارد کند.", objectName="Muted", wordWrap=True))
        self.body.insertWidget(0, warning)

        self.approver = QComboBox()
        for username, name in approvers:
            self.approver.addItem(f"{name} ({username})", username)
        if actor.is_admin:
            index = self.approver.findData(actor.username)
            if index >= 0:
                self.approver.setCurrentIndex(index)
        self.add_row("مدیر تأییدکننده:", self.approver)
        self.pin = self.add_row("کد PIN:", ltr_field())
        self.pin.setEchoMode(self.pin.EchoMode.Password)
        self.pin.setMaxLength(8)
        self.pin.setFocus()

    async def submit(self) -> None:
        self.approval = await protected.approve(
            self._db, self._actor, self._action, self.approver.currentData(), self.pin.text()
        )

    def show_status(self, text: str, is_error: bool = True) -> None:
        super().show_status(text, is_error)
        self.pin.clear()


async def request_approval(db: Database, actor: Actor, action: ProtectedAction,
                           description: str, parent: QWidget | None = None) -> Approval | None:
    """Ask for confirmation + admin PIN. None if cancelled or impossible."""
    approvers = await protected.list_approvers(db)
    if not approvers:
        show_error(parent, "هیچ مدیری PIN تنظیم نکرده است. "
                           "ابتدا از منوی کاربر، PIN مدیر را تنظیم کنید.")
        return None
    dialog = ApprovalDialog(db, actor, action, description, approvers, parent)
    await exec_dialog(dialog)
    return dialog.approval


async def run_login(db: Database, username: str = "", cancel_text: str = "خروج",
                    parent: QWidget | None = None) -> Actor | None:
    """Full login: credentials, then forced password change and admin PIN setup if needed.

    Returns None if the user cancels at any step.
    """
    dialog = LoginDialog(db, username, cancel_text, parent)
    if not await exec_dialog(dialog) or dialog.result_login is None:
        return None
    result = dialog.result_login
    if result.must_change_password and not await exec_dialog(
        ChangePasswordDialog(db, result.actor, forced=True, parent=parent)
    ):
        return None
    if result.needs_pin and not await exec_dialog(
        SetPinDialog(db, result.actor, forced=True, parent=parent)
    ):
        return None
    return result.actor
