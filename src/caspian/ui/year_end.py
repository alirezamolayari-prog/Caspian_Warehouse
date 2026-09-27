"""Fiscal year tab and the step-by-step year-end wizard."""

import datetime as dt

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.settings import default_backup_dir
from caspian.core.text import to_persian_digits
from caspian.services import backup, fiscal
from caspian.services.errors import ServiceError
from caspian.services.fiscal import CloseOptions, CloseResult, PreCheck
from caspian.services.fiscal_state import load_state
from caspian.services.protected import ProtectedAction
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.auth_dialogs import request_approval
from caspian.ui.dialogs import ltr_field, password_field
from caspian.ui.messages import show_info
from caspian.ui.widgets import Card, DataTable

STEPS = ("بررسی", "گزینه‌ها", "پشتیبان و بایگانی", "تأیید و اجرا")


class YearEndWizard(QDialog):
    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self.setWindowTitle("بستن سال مالی")
        self.setMinimumSize(640, 520)
        self.result_info: CloseResult | None = None
        self._check: PreCheck | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        layout.addWidget(QLabel("بستن سال مالی", objectName="PageTitle"))
        self.step_label = QLabel(objectName="Muted")
        layout.addWidget(self.step_label)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.status = QLabel(objectName="StatusText")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("انصراف")
        self.cancel_button.clicked.connect(self.reject)
        self.back_button = QPushButton("قبلی")
        self.back_button.clicked.connect(lambda: self._go(self.stack.currentIndex() - 1))
        self.next_button = QPushButton("بعدی")
        self.next_button.setProperty("variant", "primary")
        self.next_button.clicked.connect(self.on_next)
        for b in (self.cancel_button, self.back_button, self.next_button):
            buttons.addWidget(b)
        layout.addLayout(buttons)

        # 1. checks
        page = QWidget()
        col = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("سالی که بسته می‌شود:"))
        self.year = QSpinBox()
        self.year.setRange(1390, 1500)
        self.year.setValue(jalali.fiscal_year_of(dt.date.today()) - 1)
        self.year.valueChanged.connect(lambda _: self.run_check())
        row.addWidget(self.year)
        row.addStretch(1)
        col.addLayout(row)
        self.check_label = QLabel()
        self.check_label.setWordWrap(True)
        col.addWidget(self.check_label)
        col.addStretch(1)
        self.stack.addWidget(page)

        # 2. options
        page = QWidget()
        col = QVBoxLayout(page)
        self.carry_mode = QRadioButton("انتقال اطلاعات به سال جدید")
        self.fresh_mode = QRadioButton("شروع یک سال کاملاً خالی (Fresh Start)")
        group = QButtonGroup(self)
        group.addButton(self.carry_mode)
        group.addButton(self.fresh_mode)
        self.carry_mode.setChecked(True)
        col.addWidget(self.carry_mode)
        self.carry_persons = QCheckBox("اشخاص (تأمین‌کنندگان، مشتریان، کارمندان)")
        self.carry_items = QCheckBox("تعریف کالاها")
        self.carry_stock = QCheckBox("موجودی کالا (به‌صورت سند «موجودی اول دوره»)")
        self.purge_stale = QCheckBox(
            f"حذف کالاهای راکد (بدون موجودی و بدون گردش در {fiscal.STALE_MONTHS} ماه اخیر)")
        for box in (self.carry_persons, self.carry_items, self.carry_stock):
            box.setChecked(True)
            col.addWidget(box)
        col.addWidget(self.purge_stale)
        col.addSpacing(12)
        col.addWidget(self.fresh_mode)
        note = QLabel("امانی‌های باز همیشه به سال جدید منتقل می‌شوند. همه اطلاعات سال بسته‌شده در بایگانی "
                      "فقط‌خواندنی باقی می‌ماند و در گزارش‌ها قابل مشاهده است.", objectName="Muted")
        note.setWordWrap(True)
        col.addWidget(note)
        col.addStretch(1)
        self.fresh_mode.toggled.connect(self._on_mode)
        self.stack.addWidget(page)

        # 3. backup & archive
        page = QWidget()
        form = QFormLayout(page)
        self.backup_dir = QLineEdit(ctx.settings.backup_dir or str(default_backup_dir()))
        form.addRow("پوشه پشتیبان:", self.backup_dir)
        self.backup_state = QLabel()
        form.addRow("رمز پشتیبان:", self.backup_state)
        self.admin_user = ltr_field("root")
        self.admin_password = password_field()
        self._needs_admin = not ctx.db.is_sqlite
        if self._needs_admin:
            form.addRow(QLabel("برای ساخت پایگاه داده بایگانی، حساب مدیر MariaDB لازم است (ذخیره نمی‌شود).",
                               objectName="Muted"))
            form.addRow("کاربر مدیر MariaDB:", self.admin_user)
            form.addRow("رمز:", self.admin_password)
        self.stack.addWidget(page)

        # 4. confirm
        page = QWidget()
        col = QVBoxLayout(page)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        col.addWidget(self.summary)
        col.addStretch(1)
        self.stack.addWidget(page)
        self._go(0)
        self.run_check()

    def _on_mode(self, fresh: bool) -> None:
        for box in (self.carry_persons, self.carry_items, self.carry_stock, self.purge_stale):
            box.setEnabled(not fresh)

    def options(self) -> CloseOptions:
        if self.fresh_mode.isChecked():
            return CloseOptions.fresh_start()
        return CloseOptions(self.carry_persons.isChecked(), self.carry_items.isChecked(),
                            self.carry_stock.isChecked(), self.purge_stale.isChecked())

    def _go(self, index: int) -> None:
        index = max(0, min(index, self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        self.step_label.setText(f"مرحله {to_persian_digits(index + 1)} از {to_persian_digits(len(STEPS))}: "
                                f"{STEPS[index]}")
        self.back_button.setEnabled(index > 0)
        last = index == self.stack.count() - 1
        self.next_button.setText("بستن سال مالی" if last else "بعدی")
        self.next_button.setProperty("variant", "danger" if last else "primary")
        self.next_button.style().unpolish(self.next_button)
        self.next_button.style().polish(self.next_button)
        self.status.setText("")
        if index == 0:
            self.next_button.setEnabled(bool(self._check and not self._check.blocking))
        if index == 2:
            has_pw = bool(backup.backup_password())
            self.backup_state.setText("تعیین شده" if has_pw
                                      else "تعیین نشده — ابتدا در تنظیمات ← پشتیبان‌گیری تعیین کنید")
            self.next_button.setEnabled(has_pw)
        if last:
            o = self.options()
            carry = "شروع خالی" if self.fresh_mode.isChecked() else "، ".join(
                n for n, on in (("اشخاص", o.carry_persons), ("کالاها", o.carry_items),
                                ("موجودی", o.carry_stock)) if on) or "هیچ‌کدام"
            y = to_persian_digits(self.year.value())
            self.summary.setText(
                f"سال مالی {y} بسته می‌شود:\n"
                f"• ابتدا یک نسخه پشتیبان رمزنگاری‌شده در «{self.backup_dir.text()}» ساخته می‌شود.\n"
                f"• همه اطلاعات سال {y} در بایگانی فقط‌خواندنی نگه داشته می‌شود.\n"
                f"• منتقل‌شونده‌ها: {carry}"
                + ("؛ کالاهای راکد حذف می‌شوند." if o.purge_stale_items else ".") + "\n"
                f"• رویدادهای سیستم تا پایان سال {y} به فایل اکسل منتقل و از پایگاه فعال پاک می‌شوند.\n"
                f"• پس از بستن، سندی با تاریخ سال {y} قابل ثبت یا تغییر نیست.\n\n"
                "این عملیات نیاز به PIN مدیر دارد.")

    @asyncSlot()
    async def run_check(self) -> None:
        try:
            self._check = await fiscal.pre_check(self._ctx.db, self._ctx.actor, self.year.value())
        except ServiceError as exc:
            self.check_label.setText(exc.message)
            return
        c = self._check
        lines = [f"اسناد سال {to_persian_digits(c.year)}: {to_persian_digits(c.documents)}",
                 f"امانی‌های باز (منتقل می‌شوند): {to_persian_digits(c.open_loans)}"]
        lines += [f"✖ {p}" for p in c.blocking] or ["✔ مانعی برای بستن سال وجود ندارد."]
        self.check_label.setText("\n".join(lines))
        if self.stack.currentIndex() == 0:
            self.next_button.setEnabled(not c.blocking)

    @asyncSlot()
    async def on_next(self) -> None:
        index = self.stack.currentIndex()
        if index < self.stack.count() - 1:
            self._go(index + 1)
            return
        approval = await request_approval(
            self._ctx.db, self._ctx.actor, ProtectedAction.CLOSE_FISCAL_YEAR,
            f"بستن سال مالی {to_persian_digits(self.year.value())} و انتقال اطلاعات آن به بایگانی.", self)
        if approval is None:
            return
        for b in (self.next_button, self.back_button, self.cancel_button):
            b.setEnabled(False)
        steps = {"backup": "در حال تهیه نسخه پشتیبان…", "archive": "در حال ساخت بایگانی…",
                 "close": "در حال بستن سال و انتقال مانده‌ها…"}
        try:
            self.result_info = await fiscal.run_year_end(
                self._ctx.db, self._ctx.actor, self.year.value(), self.options(), approval,
                backup.make_dumper(self._ctx.db, self._ctx.db_config, self._ctx.db_password,
                                   self._ctx.settings.mariadb_tools_dir),
                backup.backup_password() or "", self.backup_dir.text(), self._ctx.db_config,
                self.admin_user.text().strip(), self.admin_password.text(),
                progress=lambda step: self.status.setText(steps[step]))
        except ServiceError as exc:
            self.status.setProperty("error", True)
            self.status.setText(exc.message)
            self.cancel_button.setEnabled(True)
            return
        self.accept()


class FiscalTab(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        card = Card()
        card.body.addWidget(QLabel("سال مالی", objectName="CardTitle"))
        self.state_label = QLabel(objectName="Muted")
        self.state_label.setWordWrap(True)
        card.body.addWidget(self.state_label)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_button = QPushButton("بستن سال مالی…")
        self.close_button.setProperty("variant", "danger")
        self.close_button.clicked.connect(self.on_close_year)
        row.addWidget(self.close_button)
        card.body.addLayout(row)
        layout.addWidget(card)
        archives = Card()
        archives.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(("سال", "پایگاه داده بایگانی", "تاریخ بستن", "بسته‌شده توسط", "نسخه پشتیبان"))
        archives.body.addWidget(self.table)
        layout.addWidget(archives, 1)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    @asyncSlot()
    async def refresh(self) -> None:
        state = await load_state(self._ctx.db)
        current = jalali.fiscal_year_of(dt.date.today())
        closed = to_persian_digits(state.closed_through) if state.closed_through else "هیچ سالی"
        self.state_label.setText(f"سال مالی جاری: {to_persian_digits(current)} — بسته‌شده تا: {closed}. "
                                 "اطلاعات سال‌های بسته‌شده فقط‌خواندنی است و از بخش گزارش‌ها قابل مشاهده است.")
        self.table.set_rows([(a.year, (to_persian_digits(a.year), a.database,
                                       jalali.format_date(dt.datetime.fromisoformat(a.closed_at)),
                                       a.closed_by, a.backup)) for a in state.archive_list()])

    @asyncSlot()
    async def on_close_year(self) -> None:
        wizard = YearEndWizard(self._ctx, self)
        if await exec_dialog(wizard) and wizard.result_info:
            r = wizard.result_info
            show_info(self, f"سال مالی {to_persian_digits(r.year)} بسته شد.\n"
                            f"سند موجودی اول دوره: {to_persian_digits(r.opening_documents)} — "
                            f"امانی منتقل‌شده: {to_persian_digits(r.carried_loans)}\n"
                            f"رویدادهای بایگانی‌شده: {to_persian_digits(r.audit_rows_exported)}"
                            f" ({r.audit_file})")
        await self.refresh()
