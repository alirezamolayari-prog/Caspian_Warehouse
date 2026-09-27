"""Backup settings: folder, retention, password, backup now, verify, restore (PIN)."""

import os
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.permissions import Perm
from caspian.core.settings import default_backup_dir
from caspian.core.text import to_persian_digits
from caspian.db.bootstrap import prepare
from caspian.services import backup
from caspian.services.backup import BackupInfo, find_tool
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.protected import ProtectedAction
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.auth_dialogs import request_approval
from caspian.ui.dialogs import FormDialog, password_field
from caspian.ui.messages import show_error, show_info
from caspian.ui.widgets import Card, DataTable


class BackupPasswordDialog(FormDialog):
    def __init__(self, parent=None) -> None:
        super().__init__("رمز نسخه‌های پشتیبان",
                         "همه نسخه‌های پشتیبان با این رمز رمزنگاری می‌شوند. بدون آن، بازیابی ممکن نیست؛ "
                         "آن را جایی امن (خارج از این رایانه) یادداشت کنید. رمز روی همین رایانه در "
                         "Windows Credential Manager نگه داشته می‌شود تا پشتیبان‌گیری خودکار کار کند.",
                         submit_text="ذخیره", parent=parent)
        self.password = self.add_row("رمز جدید:", password_field("حداقل ۸ کاراکتر"))
        self.repeat = self.add_row("تکرار:", password_field())

    async def submit(self) -> None:
        if self.password.text() != self.repeat.text():
            raise ValidationError("رمز و تکرار آن یکسان نیستند.")
        backup.set_backup_password(self.password.text())


class RestorePasswordDialog(FormDialog):
    def __init__(self, info: BackupInfo, parent=None) -> None:
        super().__init__("رمز نسخه پشتیبان",
                         f"رمز فایل «{info.name}» را وارد کنید (اگر با رمز فعلی این رایانه ساخته شده، "
                         "خالی بگذارید).", submit_text="ادامه", parent=parent)
        self.password = self.add_row("رمز:", password_field())

    async def submit(self) -> None:
        pass


class BackupTab(QWidget):
    COLUMNS = ("تاریخ", "ساعت", "برچسب", "حجم", "نسخه برنامه", "فایل")

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._rows: dict[str, BackupInfo] = {}
        settings = ctx.settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        card = Card()
        card.body.addWidget(QLabel("تنظیمات پشتیبان‌گیری (این رایانه)", objectName="CardTitle"))
        form = QFormLayout()
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(settings.backup_dir or str(default_backup_dir()))
        browse = QPushButton("انتخاب…")
        browse.clicked.connect(self.on_browse_folder)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        form.addRow("پوشه پشتیبان:", folder_row)
        self.keep = QSpinBox()
        self.keep.setRange(0, 1000)
        self.keep.setValue(settings.backup_keep)
        self.keep.setSuffix(" نسخه آخر (۰ = همه)")
        form.addRow("نگهداری:", self.keep)
        tools_row = QHBoxLayout()
        self.tools = QLineEdit(settings.mariadb_tools_dir)
        self.tools.setPlaceholderText("خودکار")
        tools_browse = QPushButton("انتخاب…")
        tools_browse.clicked.connect(self.on_browse_tools)
        tools_row.addWidget(self.tools, 1)
        tools_row.addWidget(tools_browse)
        form.addRow("پوشه ابزارهای MariaDB:", tools_row)
        self.tools_status = QLabel(objectName="Muted")
        form.addRow("", self.tools_status)
        self.password_status = QLabel(objectName="Muted")
        pw_row = QHBoxLayout()
        pw_row.addWidget(self.password_status, 1)
        self.password_button = QPushButton("تعیین / تغییر رمز پشتیبان…")
        self.password_button.clicked.connect(self.on_password)
        pw_row.addWidget(self.password_button)
        form.addRow("رمز پشتیبان:", pw_row)
        card.body.addLayout(form)
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.save_button = QPushButton("ذخیره تنظیمات")
        self.save_button.clicked.connect(self.on_save_settings)
        save_row.addWidget(self.save_button)
        card.body.addLayout(save_row)
        layout.addWidget(card)

        toolbar = QHBoxLayout()
        self.status = QLabel(objectName="Muted")
        toolbar.addWidget(self.status, 1)
        self.open_button = QPushButton("باز کردن پوشه")
        self.verify_button = QPushButton("بررسی سلامت")
        self.restore_button = QPushButton("بازیابی…")
        self.restore_button.setProperty("variant", "danger")
        self.backup_button = QPushButton("پشتیبان‌گیری اکنون")
        self.backup_button.setProperty("variant", "primary")
        for b, slot in ((self.open_button, self.on_open_folder), (self.verify_button, self.on_verify),
                        (self.restore_button, self.on_restore), (self.backup_button, self.on_backup)):
            b.clicked.connect(slot)
            toolbar.addWidget(b)
        layout.addLayout(toolbar)
        list_card = Card()
        list_card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(self.COLUMNS)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        list_card.body.addWidget(self.table)
        layout.addWidget(list_card, 1)
        ctx.user_changed.connect(self._update_buttons)
        self._refresh_status()

    # ----- helpers -----

    def _dumper(self):
        return backup.make_dumper(self._ctx.db, self._ctx.db_config, self._ctx.db_password,
                                  self.tools.text().strip())

    def _refresh_status(self) -> None:
        tools_dir = self.tools.text().strip()
        dump = find_tool("mariadb-dump", [tools_dir] if tools_dir else None)
        self.tools_status.setText(f"mariadb-dump: {dump}" if dump
                                  else "mariadb-dump پیدا نشد — مسیر را مشخص کنید.")
        has_pw = bool(backup.backup_password())
        self.password_status.setText("تعیین شده" if has_pw else "تعیین نشده — پشتیبان‌گیری ممکن نیست")

    def _update_buttons(self, *_args) -> None:
        selected = self.table.selected_id() is not None
        self.verify_button.setEnabled(selected)
        self.restore_button.setEnabled(selected)
        self.restore_button.setVisible(self._ctx.actor.can(Perm.BACKUP_RESTORE))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        rows = backup.list_backups(self.folder.text())
        self._rows = {str(b.path): b for b in rows}
        self.table.set_rows([(str(b.path), (
            jalali.format_date(b.created_at), to_persian_digits(b.created_at.strftime("%H:%M")),
            b.label or "—", f"{to_persian_digits(f'{b.size / 1_048_576:.1f}')} MB", b.app_version,
            b.name)) for b in rows])
        self.status.setText(f"{to_persian_digits(len(rows))} نسخه پشتیبان در پوشه")
        self._update_buttons()

    def selected(self) -> BackupInfo | None:
        key = self.table.selected_id()
        return self._rows.get(key) if key else None

    # ----- actions -----

    def on_browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "پوشه پشتیبان", self.folder.text())
        if path:
            self.folder.setText(path)
            self.refresh()

    def on_browse_tools(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "پوشه bin ابزارهای MariaDB", self.tools.text())
        if path:
            self.tools.setText(path)
            self._refresh_status()

    def on_save_settings(self) -> None:
        s = self._ctx.settings
        s.backup_dir, s.backup_keep, s.mariadb_tools_dir = (self.folder.text().strip(),
                                                            self.keep.value(), self.tools.text().strip())
        s.save()
        self._refresh_status()
        show_info(self, "تنظیمات پشتیبان‌گیری ذخیره شد.")

    @asyncSlot()
    async def on_password(self) -> None:
        if await exec_dialog(BackupPasswordDialog(self)):
            self._refresh_status()

    def on_open_folder(self) -> None:
        folder = Path(self.folder.text())
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(folder)))

    @asyncSlot()
    async def on_backup(self) -> None:
        password = backup.backup_password()
        if not password:
            show_error(self, "ابتدا رمز پشتیبان را تعیین کنید.")
            return
        self.backup_button.setEnabled(False)
        self.status.setText("در حال تهیه نسخه پشتیبان…")
        try:
            info = await backup.create_backup(self._ctx.db, self._ctx.actor, self._dumper(), password,
                                              self.folder.text(), "دستی", keep=self.keep.value())
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        finally:
            self.backup_button.setEnabled(True)
        self.refresh()
        show_info(self, f"نسخه پشتیبان ذخیره شد:\n{info.path}")

    async def _ask_password(self, info: BackupInfo) -> str | None:
        dialog = RestorePasswordDialog(info, self)
        if not await exec_dialog(dialog):
            return None
        return dialog.password.text() or backup.backup_password() or ""

    @asyncSlot()
    async def on_verify(self) -> None:
        if (info := self.selected()) is None:
            return
        password = await self._ask_password(info)
        if password is None:
            return
        try:
            await backup.verify_backup(info.path, password)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        show_info(self, "نسخه پشتیبان سالم است و با این رمز قابل بازیابی است.")

    @asyncSlot()
    async def on_restore(self) -> None:
        if (info := self.selected()) is None:
            return
        password = await self._ask_password(info)
        if password is None:
            return
        approval = await request_approval(
            self._ctx.db, self._ctx.actor, ProtectedAction.RESTORE_BACKUP,
            f"همه اطلاعات فعلی با نسخه پشتیبان «{info.name}» (تاریخ {jalali.format_date(info.created_at)}) "
            "جایگزین می‌شود. پیش از بازیابی، یک نسخه پشتیبان خودکار از وضعیت فعلی گرفته می‌شود. پس از "
            "بازیابی برنامه بسته می‌شود و باید دوباره وارد شوید.", self)
        if approval is None:
            return
        self.status.setText("در حال بازیابی… لطفاً صبر کنید.")
        self.setEnabled(False)
        try:
            await backup.restore_backup(self._ctx.db, self._ctx.actor, self._dumper(), info.path,
                                        password, approval, self.folder.text(),
                                        after_restore=lambda: prepare(self._ctx.db))
        except ServiceError as exc:
            self.setEnabled(True)
            show_error(self, exc.message)
            return
        box = QMessageBox(QMessageBox.Icon.Information, "بازیابی انجام شد",
                          "بازیابی انجام شد. برنامه بسته می‌شود؛ لطفاً دوباره اجرا و وارد شوید.",
                          QMessageBox.StandardButton.Ok, self)
        await exec_dialog(box)  # wait for the user before quitting
        QApplication.instance().quit()
