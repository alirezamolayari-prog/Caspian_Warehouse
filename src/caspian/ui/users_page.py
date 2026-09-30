"""User management (requires users.manage)."""

import dataclasses
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.services import users
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.protected import ProtectedAction
from caspian.services.users import RoleRow, UserRow
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.auth_dialogs import request_approval
from caspian.ui.dialogs import FormDialog, ltr_field, password_field
from caspian.ui.messages import show_error, show_info
from caspian.ui.widgets import Card

log = logging.getLogger(__name__)

COLUMNS = ("نام کاربری", "نام و نام خانوادگی", "نقش", "وضعیت", "کد PIN", "آخرین ورود")


def _role_combo(roles: list[RoleRow], current: str = "") -> QComboBox:
    combo = QComboBox()
    for role in roles:
        combo.addItem(role.name, role.code)
    if current:
        combo.setCurrentIndex(combo.findData(current))
    return combo


class NewUserDialog(FormDialog):
    def __init__(self, ctx: AppContext, roles: list[RoleRow], parent=None) -> None:
        super().__init__("کاربر جدید", "کاربر در اولین ورود باید رمز عبور موقت را تغییر دهد.",
                         submit_text="ایجاد کاربر", parent=parent)
        self._ctx = ctx
        self.created: UserRow | None = None
        self.username = self.add_row("نام کاربری:", ltr_field())
        self.full_name = self.add_row("نام و نام خانوادگی:", ltr_field())
        self.full_name.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.role = self.add_row("نقش:", _role_combo(roles, "storekeeper"))
        self.password = self.add_row("رمز عبور موقت:", password_field("حداقل ۶ کاراکتر"))
        self.username.editingFinished.connect(self.check_username)

    @asyncSlot()
    async def check_username(self) -> None:
        """Say a name is taken as soon as it's typed, not after the password (#15)."""
        if self.username.text().strip() and await users.username_taken(self._ctx.db, self.username.text()):
            self.show_status(users.TAKEN)
        elif self.status.text() == users.TAKEN:
            self.show_status("")

    async def submit(self) -> None:
        role = self.role.currentData()
        approval = None
        if role == "admin":
            approval = await request_approval(
                self._ctx.db, self._ctx.actor, ProtectedAction.CHANGE_ROLE,
                f"ایجاد کاربر «{self.username.text().strip()}» با نقش مدیر سیستم.", self,
            )
            if approval is None:
                raise ValidationError("ایجاد مدیر بدون تأیید PIN امکان‌پذیر نیست.")
        self.created = await users.create_user(
            self._ctx.db, self._ctx.actor, self.username.text(), self.full_name.text(),
            self.password.text(), role, approval,
        )


class EditNameDialog(FormDialog):
    """Login name and full name. A new login name must be unique (audited as user.renamed)."""

    def __init__(self, ctx: AppContext, user: UserRow, parent=None) -> None:
        super().__init__(f"ویرایش کاربر «{user.username}»", submit_text="ذخیره", parent=parent)
        self._ctx, self._user = ctx, user
        self.username = self.add_row("نام کاربری:", ltr_field(user.username))
        self.full_name = self.add_row("نام و نام خانوادگی:", ltr_field(user.full_name))
        self.full_name.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

    async def submit(self) -> None:
        if self.username.text().strip().lower() != self._user.username:
            await users.rename_user(self._ctx.db, self._ctx.actor, self._user.id, self.username.text())
            if self._user.id == self._ctx.actor.user_id:  # keep the header chip in sync
                self._ctx.set_actor(dataclasses.replace(
                    self._ctx.actor, username=self.username.text().strip().lower()))
        await users.update_user(self._ctx.db, self._ctx.actor, self._user.id,
                                self.full_name.text())


class ResetPasswordDialog(FormDialog):
    def __init__(self, ctx: AppContext, user: UserRow, parent=None) -> None:
        super().__init__(f"بازنشانی رمز «{user.username}»",
                         "کاربر در ورود بعدی باید این رمز موقت را تغییر دهد.",
                         submit_text="بازنشانی", parent=parent)
        self._ctx, self._user = ctx, user
        self.password = self.add_row("رمز موقت:", password_field("حداقل ۶ کاراکتر"))

    async def submit(self) -> None:
        await users.reset_password(self._ctx.db, self._ctx.actor, self._user.id,
                                   self.password.text())


class ChangeRoleDialog(FormDialog):
    def __init__(self, ctx: AppContext, user: UserRow, roles: list[RoleRow], parent=None) -> None:
        super().__init__(f"تغییر نقش «{user.username}»", "تغییر نقش نیاز به تأیید PIN مدیر دارد.",
                         submit_text="ادامه", parent=parent)
        self._ctx, self._user, self._roles = ctx, user, roles
        self.role = self.add_row("نقش جدید:", _role_combo(roles, user.role_code))

    async def submit(self) -> None:
        code = self.role.currentData()
        if code == self._user.role_code:
            return
        new_name = self.role.currentText()
        approval = await request_approval(
            self._ctx.db, self._ctx.actor, ProtectedAction.CHANGE_ROLE,
            f"تغییر نقش کاربر «{self._user.username}» از «{self._user.role_name}» "
            f"به «{new_name}».", self,
        )
        if approval is None:
            raise ValidationError("تغییر نقش لغو شد.")
        await users.change_role(self._ctx.db, self._ctx.actor, self._user.id, code, approval)


class UsersPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._rows: list[UserRow] = []
        self._roles: list[RoleRow] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        self.new_button = QPushButton("کاربر جدید")
        self.new_button.setProperty("variant", "primary")
        self.new_button.clicked.connect(self.on_new)
        toolbar.addWidget(self.new_button)
        toolbar.addStretch(1)
        self.edit_button = QPushButton("ویرایش کاربر")
        self.reset_button = QPushButton("بازنشانی رمز")
        self.role_button = QPushButton("تغییر نقش")
        self.active_button = QPushButton("غیرفعال‌سازی")
        self.edit_button.clicked.connect(self.on_edit)
        self.reset_button.clicked.connect(self.on_reset)
        self.role_button.clicked.connect(self.on_role)
        self.active_button.clicked.connect(self.on_toggle_active)
        for button in (self.edit_button, self.reset_button, self.role_button, self.active_button):
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self.on_edit())
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        self._update_buttons()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    @asyncSlot()
    async def refresh(self) -> None:
        try:
            self._roles = await users.list_roles(self._ctx.db)
            self._rows = await users.list_users(self._ctx.db, self._ctx.actor)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        selected = self.selected()
        self.table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            values = (
                row.username,
                row.full_name,
                row.role_name,
                "فعال" if row.is_active else "غیرفعال",
                "تنظیم شده" if row.has_pin else ("—" if row.role_code != "admin" else "ندارد"),
                jalali.format_date(row.last_login_at) if row.last_login_at else "—",
            )
            for c, value in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(value))
        if selected:
            for r, row in enumerate(self._rows):
                if row.id == selected.id:
                    self.table.selectRow(r)
        self._update_buttons()

    def selected(self) -> UserRow | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self._rows[rows[0].row()] if rows and rows[0].row() < len(self._rows) else None

    def _update_buttons(self) -> None:
        user = self.selected()
        for button in (self.edit_button, self.reset_button, self.role_button, self.active_button):
            button.setEnabled(user is not None)
        if user is not None:
            self.active_button.setText("غیرفعال‌سازی" if user.is_active else "فعال‌سازی")
            self.active_button.setEnabled(user.id != self._ctx.actor.user_id or not user.is_active)

    async def _run(self, dialog: FormDialog) -> bool:
        accepted = bool(await exec_dialog(dialog))
        if accepted:
            await self.refresh()
        return accepted

    @asyncSlot()
    async def on_new(self) -> None:
        if not self._roles:
            self._roles = await users.list_roles(self._ctx.db)
        await self._run(NewUserDialog(self._ctx, self._roles, self))

    @asyncSlot()
    async def on_edit(self) -> None:
        if user := self.selected():
            await self._run(EditNameDialog(self._ctx, user, self))

    @asyncSlot()
    async def on_reset(self) -> None:
        user = self.selected()
        if user and await self._run(ResetPasswordDialog(self._ctx, user, self)):
            show_info(self, f"رمز «{user.username}» بازنشانی شد.")

    @asyncSlot()
    async def on_role(self) -> None:
        if user := self.selected():
            await self._run(ChangeRoleDialog(self._ctx, user, self._roles, self))

    @asyncSlot()
    async def on_toggle_active(self) -> None:
        user = self.selected()
        if user is None:
            return
        try:
            await users.set_active(self._ctx.db, self._ctx.actor, user.id, not user.is_active)
        except ServiceError as exc:
            show_error(self, exc.message)
        await self.refresh()
