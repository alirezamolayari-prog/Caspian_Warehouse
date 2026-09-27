"""Settings tabs for messaging (Telegram / email), scheduled tasks and MCP access."""

import json
import shutil
import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.text import to_persian_digits
from caspian.db.database import describe_error
from caspian.db.models import TaskKind, TaskStatus
from caspian.db.readonly import create_readonly_user, readonly_password, readonly_username
from caspian.services import messaging, scheduler
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.messaging import EmailConfig, MessagingConfig, TelegramConfig
from caspian.services.scheduler import KIND_NAMES, REPORT_NAMES, STATUS_NAMES, TaskRow
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.dialogs import FormDialog, ltr_field, password_field
from caspian.ui.messages import show_error, show_info
from caspian.ui.widgets import Card, DataTable

CHANNEL_NAMES = {"telegram": "تلگرام", "email": "ایمیل"}


def _split(text: str) -> list[str]:
    return [p.strip() for p in text.replace("،", ",").split(",") if p.strip()]


class MessagingTab(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(12)
        note = QLabel("گیرندگان را فقط مدیر تعیین می‌کند؛ دستیار هوشمند و کارهای زمان‌بندی‌شده فقط به همین "
                      "گیرندگان پیام می‌فرستند. توکن و رمز فقط روی همین رایانه ذخیره می‌شوند.",
                      objectName="Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        tg = Card()
        tg.body.addWidget(QLabel("تلگرام", objectName="CardTitle"))
        form = QFormLayout()
        self.tg_enabled = QCheckBox("فعال")
        self.tg_token = password_field("توکن ربات (از BotFather)")
        self.tg_chats = ltr_field()
        self.tg_chats.setPlaceholderText("شناسه گفتگو، با کاما جدا کنید")
        form.addRow("", self.tg_enabled)
        form.addRow("توکن ربات:", self.tg_token)
        form.addRow("شناسه‌های گفتگو:", self.tg_chats)
        tg.body.addLayout(form)
        layout.addWidget(tg)

        mail = Card()
        mail.body.addWidget(QLabel("ایمیل (SMTP)", objectName="CardTitle"))
        form = QFormLayout()
        self.mail_enabled = QCheckBox("فعال")
        self.mail_host = ltr_field()
        self.mail_port = QSpinBox()
        self.mail_port.setRange(1, 65535)
        self.mail_security = QComboBox()
        for value, label in (("starttls", "STARTTLS"), ("ssl", "SSL/TLS"), ("none", "بدون رمزنگاری")):
            self.mail_security.addItem(label, value)
        self.mail_user = ltr_field()
        self.mail_password = password_field("رمز SMTP")
        self.mail_sender = ltr_field()
        self.mail_recipients = ltr_field()
        self.mail_recipients.setPlaceholderText("گیرندگان، با کاما جدا کنید")
        for label, w in (("", self.mail_enabled), ("سرور:", self.mail_host), ("پورت:", self.mail_port),
                         ("امنیت:", self.mail_security), ("نام کاربری:", self.mail_user),
                         ("رمز:", self.mail_password), ("فرستنده:", self.mail_sender),
                         ("گیرندگان:", self.mail_recipients)):
            form.addRow(label, w)
        mail.body.addLayout(form)
        layout.addWidget(mail)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.test_button = QPushButton("ارسال پیام آزمایشی")
        self.test_button.clicked.connect(self.on_test)
        self.save_button = QPushButton("ذخیره")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.on_save)
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self._loaded = False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.load()

    @asyncSlot()
    async def load(self) -> None:
        config = await messaging.load_config(self._ctx.db)
        self.tg_enabled.setChecked(config.telegram.enabled)
        self.tg_chats.setText(", ".join(config.telegram.chat_ids))
        e = config.email
        self.mail_enabled.setChecked(e.enabled)
        self.mail_host.setText(e.host)
        self.mail_port.setValue(e.port)
        self.mail_security.setCurrentIndex(max(self.mail_security.findData(e.security), 0))
        self.mail_user.setText(e.username)
        self.mail_sender.setText(e.sender)
        self.mail_recipients.setText(", ".join(e.recipients))

    def collect(self) -> MessagingConfig:
        return MessagingConfig(
            TelegramConfig(self.tg_enabled.isChecked(), _split(self.tg_chats.text())),
            EmailConfig(self.mail_enabled.isChecked(), self.mail_host.text().strip(),
                        self.mail_port.value(), self.mail_security.currentData(),
                        self.mail_user.text().strip(), self.mail_sender.text().strip(),
                        _split(self.mail_recipients.text())))

    @asyncSlot()
    async def on_save(self) -> None:
        try:
            await messaging.save_config(self._ctx.db, self._ctx.actor, self.collect(),
                                        self.tg_token.text() or None,
                                        self.mail_password.text() or None)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        self.tg_token.clear()
        self.mail_password.clear()
        show_info(self, "تنظیمات پیام‌رسان ذخیره شد.")

    @asyncSlot()
    async def on_test(self) -> None:
        self.test_button.setEnabled(False)
        try:
            used = await self._ctx.messenger.send("پیام آزمایشی از انبار کاسپین ✅")
            show_info(self, "پیام آزمایشی ارسال شد: " + "، ".join(CHANNEL_NAMES[c] for c in used))
        except ServiceError as exc:
            show_error(self, exc.message)
        finally:
            self.test_button.setEnabled(True)


class InstructionsDialog(FormDialog):
    """Instructions (typed or .md file) -> proposed tasks (still need approval)."""

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__("افزودن کار از دستورالعمل",
                         "دستورالعمل‌ها را بنویسید یا یک فایل .md بارگذاری کنید، مثلاً «هر روز ساعت ۸ شب "
                         "پشتیبان بگیر» یا «هر شنبه ساعت ۹ گزارش موجودی را به تلگرام بفرست». کارها "
                         "پس از تأیید شما فعال می‌شوند.", submit_text="تحلیل و پیشنهاد", parent=parent)
        self.setMinimumSize(600, 460)
        self._ctx = ctx
        self.created: list[int] = []
        load = QPushButton("بارگذاری فایل .md…")
        load.clicked.connect(self.on_load_file)
        self.body.addWidget(load)
        self.text = QPlainTextEdit()
        self.body.addWidget(self.text, 1)
        self._inputs.append(self.text)

    def on_load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "فایل دستورالعمل", "", "Markdown (*.md *.txt)")
        if path:
            self.text.setPlainText(Path(path).read_text(encoding="utf-8", errors="replace"))

    async def submit(self) -> None:
        text = self.text.toPlainText().strip()
        if not text:
            raise ValidationError("دستورالعملی وارد نشده است.")
        proposals, _used_ai = await scheduler.propose_from_text(text, self._ctx.ai)
        if not proposals:
            raise ValidationError("کار قابل‌فهمی (پشتیبان‌گیری یا ارسال گزارش با زمان مشخص) پیدا نشد.")
        self.created = await scheduler.add_proposals(self._ctx.db, self._ctx.actor, proposals)


class TaskEditDialog(FormDialog):
    def __init__(self, ctx: AppContext, row: TaskRow, parent=None) -> None:
        super().__init__("ویرایش کار زمان‌بندی‌شده",
                         "زمان‌بندی به شکل cron: «دقیقه ساعت روزماه ماه روزهفته» — مثلاً «0 20 * * *» یعنی "
                         "هر روز ساعت ۲۰. روز هفته: ۰=یکشنبه … ۶=شنبه.", submit_text="ذخیره", parent=parent)
        self._ctx, self._row = ctx, row
        self.name = self.add_row("نام:", QLineEdit(row.name))
        self.cron = self.add_row("زمان‌بندی:", ltr_field(row.cron))
        self.report = QComboBox()
        for key, label in REPORT_NAMES.items():
            self.report.addItem(label, key)
        self.report.setCurrentIndex(max(self.report.findData(row.params.get("report")), 0))
        self.telegram = QCheckBox("تلگرام")
        self.email = QCheckBox("ایمیل")
        self.telegram.setChecked("telegram" in row.params.get("channels", []))
        self.email.setChecked("email" in row.params.get("channels", []))
        channels = QHBoxLayout()
        channels.addWidget(self.telegram)
        channels.addWidget(self.email)
        channels.addStretch(1)
        if row.kind == TaskKind.REPORT:
            self.add_row("گزارش:", self.report)
            self.form.addRow("ارسال به:", channels)

    async def submit(self) -> None:
        params = self._row.params
        if self._row.kind == TaskKind.REPORT:
            params = {"report": self.report.currentData(),
                      "channels": [c for c, box in (("telegram", self.telegram), ("email", self.email))
                                   if box.isChecked()]}
        try:
            await scheduler.update_task(self._ctx.db, self._ctx.actor, self._row.id, self.name.text(),
                                        self.cron.text().strip(), params)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc


class TasksTab(QWidget):
    COLUMNS = ("نام", "نوع", "زمان‌بندی", "جزئیات", "وضعیت", "رایانه اجرا", "اجرای بعدی", "آخرین نتیجه")

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._rows: dict[int, TaskRow] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        self.approve_button = QPushButton("تأیید و فعال‌سازی روی این رایانه")
        self.pause_button = QPushButton("توقف")
        self.run_button = QPushButton("اجرا اکنون")
        self.edit_button = QPushButton("ویرایش")
        self.delete_button = QPushButton("حذف")
        self.add_button = QPushButton("افزودن از دستورالعمل…")
        self.add_button.setProperty("variant", "primary")
        for b, slot in ((self.approve_button, self.on_approve), (self.pause_button, self.on_pause),
                        (self.run_button, self.on_run), (self.edit_button, self.on_edit),
                        (self.delete_button, self.on_delete), (self.add_button, self.on_add)):
            b.clicked.connect(slot)
            toolbar.addWidget(b)
        layout.addLayout(toolbar)
        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(self.COLUMNS)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        self._update_buttons()

    def selected(self) -> TaskRow | None:
        tid = self.table.selected_id()
        return self._rows.get(tid) if tid is not None else None

    def _update_buttons(self, *_args) -> None:
        row = self.selected()
        for b in (self.run_button, self.edit_button, self.delete_button):
            b.setEnabled(row is not None)
        self.approve_button.setEnabled(row is not None and row.status == TaskStatus.PROPOSED)
        self.pause_button.setEnabled(row is not None and row.status != TaskStatus.PROPOSED)
        if row is not None:
            self.pause_button.setText("ادامه" if row.status == TaskStatus.PAUSED else "توقف")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    @asyncSlot()
    async def refresh(self) -> None:
        rows = await scheduler.list_tasks(self._ctx.db, self._ctx.actor)
        self._rows = {r.id: r for r in rows}
        theme = self._ctx.themes.current

        def details(r: TaskRow) -> str:
            if r.kind != TaskKind.REPORT:
                return "—"
            names = [CHANNEL_NAMES.get(c, c) for c in r.params.get("channels", [])]
            channels = "، ".join(names) or "بدون کانال"
            return f"{REPORT_NAMES.get(r.params.get('report'), '?')} ← {channels}"

        def last(r: TaskRow) -> str:
            if r.last_run_at is None:
                return "—"
            return f"{'✔' if r.last_ok else '✖'} {jalali.format_date(r.last_run_at)}: {r.last_result}"

        def next_run(r: TaskRow) -> str:
            if r.next_run_at is None or r.status != TaskStatus.ACTIVE:
                return "—"
            return f"{jalali.format_date(r.next_run_at)} {to_persian_digits(r.next_run_at.strftime('%H:%M'))}"

        self.table.set_rows(
            [(r.id, (r.name, KIND_NAMES[r.kind], to_persian_digits(r.schedule_text), details(r),
                     STATUS_NAMES[r.status], r.machine or "—", next_run(r), last(r)))
             for r in rows],
            highlight={(i, 4): theme.warning for i, r in enumerate(rows) if r.status == TaskStatus.PROPOSED},
        )
        self._update_buttons()

    async def _act(self, coro) -> None:
        try:
            await coro
        except (ServiceError, ValueError) as exc:
            show_error(self, getattr(exc, "message", str(exc)))
        await self.refresh()

    @asyncSlot()
    async def on_add(self) -> None:
        if await exec_dialog(InstructionsDialog(self._ctx, self)):
            await self.refresh()

    @asyncSlot()
    async def on_edit(self) -> None:
        if (row := self.selected()) and await exec_dialog(TaskEditDialog(self._ctx, row, self)):
            await self.refresh()

    @asyncSlot()
    async def on_approve(self) -> None:
        if row := self.selected():
            await self._act(scheduler.approve(self._ctx.db, self._ctx.actor, row.id))

    @asyncSlot()
    async def on_pause(self) -> None:
        if row := self.selected():
            await self._act(scheduler.set_paused(self._ctx.db, self._ctx.actor, row.id,
                                                 row.status != TaskStatus.PAUSED))

    @asyncSlot()
    async def on_delete(self) -> None:
        if row := self.selected():
            await self._act(scheduler.delete_task(self._ctx.db, self._ctx.actor, row.id))

    @asyncSlot()
    async def on_run(self) -> None:
        if (row := self.selected()) is None:
            return
        self.run_button.setEnabled(False)
        ok, result = await scheduler.run_task(self._ctx.db, row.id, self._ctx.messenger)
        (show_info if ok else show_error)(self, result)
        await self.refresh()


class ReadOnlyUserDialog(FormDialog):
    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__("ساخت کاربر فقط‌خواندنی پایگاه داده",
                         "برای ساخت کاربر، یک حساب مدیر MariaDB (مثلاً root) لازم است. این رمز ذخیره نمی‌شود. "
                         "کاربر ساخته‌شده فقط اجازه خواندن جدول‌های انبار را دارد (نه جدول کاربران).",
                         submit_text="ساخت / به‌روزرسانی", parent=parent)
        self._ctx = ctx
        self.admin_user = self.add_row("کاربر مدیر MariaDB:", ltr_field("root"))
        self.admin_password = self.add_row("رمز:", password_field())

    async def submit(self) -> None:
        try:
            await create_readonly_user(self._ctx.db_config, self.admin_user.text().strip(),
                                       self.admin_password.text())
        except ServiceError:
            raise
        except Exception as exc:  # connection/privilege errors from MariaDB
            raise ValidationError(describe_error(exc)) from exc


class McpSection(Card):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self.body.addWidget(QLabel("دسترسی فقط‌خواندنی برای ابزارهای هوش مصنوعی بیرونی (MCP)",
                                   objectName="CardTitle"))
        self.status = QLabel(objectName="Muted")
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)
        row = QHBoxLayout()
        self.create_button = QPushButton("ساخت کاربر فقط‌خواندنی…")
        self.create_button.clicked.connect(self.on_create)
        self.copy_button = QPushButton("کپی تنظیمات MCP")
        self.copy_button.clicked.connect(self.on_copy)
        row.addWidget(self.create_button)
        row.addWidget(self.copy_button)
        row.addStretch(1)
        self.body.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        if readonly_password(self._ctx.db_config):
            self.status.setText(f"کاربر فقط‌خواندنی «{readonly_username(self._ctx.db_config)}» روی این رایانه "
                                "تنظیم شده است؛ ابزار MCP علاوه بر ابزارهای آماده، پرس‌وجوی SELECT هم دارد.")
        else:
            self.status.setText("هنوز کاربر فقط‌خواندنی ساخته نشده؛ MCP با کاربر برنامه در حالت «فقط‌خواندنی» "
                                "کار می‌کند و پرس‌وجوی آزاد SQL غیرفعال است.")

    @asyncSlot()
    async def on_create(self) -> None:
        if await exec_dialog(ReadOnlyUserDialog(self._ctx, self)):
            self.refresh()
            show_info(self, "کاربر فقط‌خواندنی ساخته شد.")

    def on_copy(self) -> None:
        command = shutil.which("caspian-mcp") or str(Path(sys.executable).with_name("caspian-mcp.exe"))
        snippet = json.dumps({"mcpServers": {"caspian-warehouse": {"command": command}}}, indent=2)
        QGuiApplication.clipboard().setText(snippet)
        show_info(self, "تنظیمات در حافظه کپی شد:\n" + snippet)

