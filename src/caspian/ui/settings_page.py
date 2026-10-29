"""Settings: appearance (per PC) and AI providers (shared definitions, per-PC keys)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core.permissions import Perm
from caspian.core.text import ltr, to_persian_digits
from caspian.db.models import ProviderKind
from caspian.services.ai import config, server_gateway
from caspian.services.ai import history as chat_history
from caspian.services.ai.config import PRESETS, ProviderConfig
from caspian.services.errors import ServiceError
from caspian.services.server_link import connection as server_connection
from caspian.services.server_link.accounts import DEFAULT_URL as DEFAULT_SERVER_URL
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.automation_settings import McpSection, MessagingTab, TasksTab
from caspian.ui.backup_settings import BackupTab
from caspian.ui.dialogs import FormDialog, ltr_field, password_field, track_edits
from caspian.ui.messages import confirm, show_error, show_info
from caspian.ui.tasks import spawn
from caspian.ui.theme import ThemeManager
from caspian.ui.widgets import Card, DataTable, PersianSpinBox
from caspian.ui.year_end import FiscalTab


class AppearanceTab(QWidget):
    THEME_OPTIONS = (("system", "هماهنگ با ویندوز"), ("light", "روشن"), ("dark", "تیره"))

    def __init__(self, themes: ThemeManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._themes = themes
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card()
        card.body.addWidget(QLabel("ظاهر برنامه (فقط روی این رایانه)", objectName="CardTitle"))
        form = QFormLayout()
        form.setSpacing(12)
        self.theme_combo = QComboBox()
        for value, label in self.THEME_OPTIONS:
            self.theme_combo.addItem(label, value)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(themes.mode))
        self.theme_combo.currentIndexChanged.connect(self._on_theme_selected)
        themes.theme_changed.connect(self._sync_combo)
        form.addRow("پوسته:", self.theme_combo)
        card.body.addLayout(form)
        layout.addWidget(card)
        layout.addStretch(1)

    def _on_theme_selected(self) -> None:
        mode = self.theme_combo.currentData()
        if mode != self._themes.mode:
            self._themes.set_mode(mode)

    def _sync_combo(self, _theme) -> None:
        index = self.theme_combo.findData(self._themes.mode)
        if index != self.theme_combo.currentIndex():
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentIndex(index)
            self.theme_combo.blockSignals(False)


class ProviderDialog(FormDialog):
    def __init__(self, ctx: AppContext, provider: ProviderConfig | None = None, parent=None) -> None:
        super().__init__("ویرایش سرویس هوش مصنوعی" if provider else "افزودن سرویس هوش مصنوعی",
                         "تنظیمات سرویس برای همه رایانه‌های شبکه مشترک است؛ کلید API فقط روی همین "
                         "رایانه (در بخش امن رمزهای ویندوز) ذخیره می‌شود.",
                         submit_text="ذخیره", parent=parent)
        self.setMinimumWidth(520)
        self._ctx, self._provider = ctx, provider
        self.kind = QComboBox()
        for kind, preset in PRESETS.items():
            self.kind.addItem(preset.label, kind)
        self.add_row("نوع سرویس:", self.kind)
        self.name = self.add_row("نام نمایشی:", QLineEdit(provider.name if provider else ""))
        self.base_url = self.add_row("آدرس API:", ltr_field(provider.base_url if provider else ""))
        # Typed freely, or picked from the provider's own list («دریافت لیست مدل‌ها»).
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.lineEdit().setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.model.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.model.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.model.setEditText(provider.model if provider else "")
        model_row = QHBoxLayout()
        model_row.addWidget(self.model, 1)
        self.free_only = QCheckBox("فقط رایگان")
        self.free_only.toggled.connect(self._fill_models)
        model_row.addWidget(self.free_only)
        self.fetch_models = QPushButton("دریافت لیست مدل‌ها")
        self.fetch_models.clicked.connect(self.on_fetch_models)
        model_row.addWidget(self.fetch_models)
        self.form.addRow("مدل:", model_row)
        self._inputs += [self.model, self.fetch_models, self.free_only]
        self._models: list = []
        self.stt_model = self.add_row("مدل گفتار به متن:", ltr_field(provider.stt_model if provider else ""))
        self.stt_model.setPlaceholderText("خالی = بدون پشتیبانی صدا")
        self.timeout = PersianSpinBox()
        self.timeout.setRange(3, 300)
        self.timeout.setValue(provider.timeout_seconds if provider else 30)
        self.add_row("زمان انتظار (ثانیه):", self.timeout)
        self.key = self.add_row("کلید API:", password_field(
            "ذخیره شده — برای تغییر، کلید جدید را وارد کنید" if provider and provider.has_key
            else "برای مدل محلی لازم نیست"))
        self.remove_key = QCheckBox("حذف کلید از این رایانه")
        self.remove_key.setVisible(bool(provider and provider.has_key))
        self.add_row("", self.remove_key)
        self.enabled = QCheckBox("فعال")
        self.enabled.setChecked(provider.enabled if provider else True)
        self.add_row("", self.enabled)
        if provider:
            self.kind.setCurrentIndex(self.kind.findData(provider.kind))
            self.free_only.setVisible(provider.kind == ProviderKind.OPENROUTER)
        else:
            self._apply_preset()
        self.kind.currentIndexChanged.connect(lambda _: self._apply_preset())

    def _apply_preset(self) -> None:
        preset = PRESETS[self.kind.currentData()]
        self.base_url.setText(preset.base_url)
        self.model.clear()
        self._models = []
        self.model.setEditText(preset.model)
        is_openrouter = self.kind.currentData() == ProviderKind.OPENROUTER
        self.free_only.setVisible(is_openrouter)
        self.free_only.setChecked(is_openrouter)
        self.stt_model.setText(preset.stt_model)
        if not self.name.text().strip() or self._provider is None:
            self.name.setText(preset.label)

    @asyncSlot()
    async def on_fetch_models(self) -> None:
        key = self.key.text().strip() or (config.get_key(self._provider.id) if self._provider else None)
        self.fetch_models.setEnabled(False)
        self.show_status("در حال دریافت فهرست مدل‌ها…", is_error=False)
        try:
            self._models = await self._ctx.ai.list_models(self.kind.currentData(), self.base_url.text(), key)
        except ServiceError as exc:
            self.show_status(exc.message)
            return
        finally:
            self.fetch_models.setEnabled(True)
        wanted = self.model.currentText().strip()
        self._fill_models()
        free = sum(m.free for m in self._models)
        text = f"{to_persian_digits(len(self._models))} مدل دریافت شد" + (
            f" ({to_persian_digits(free)} رایگان)." if free else ".")
        if wanted and wanted not in {m.id for m in self._models}:
            # Keep the user's model; just say the provider doesn't list it (#7).
            text += f" مدل فعلی «{ltr(wanted)}» در فهرست این سرویس نیست؛ آن را بررسی کنید."
        self.show_status(text, is_error=bool(wanted) and wanted not in {m.id for m in self._models})

    def _fill_models(self, *_args) -> None:
        """Offer the fetched models without ever changing the selected one (#7)."""
        current = self.model.currentText()
        self.model.blockSignals(True)
        self.model.clear()
        only_free = not self.free_only.isHidden() and self.free_only.isChecked()
        for m in self._models:
            if m.free or not only_free:
                self.model.addItem(f"{m.id}", m.id)
        index = self.model.findText(current)
        self.model.setCurrentIndex(index)
        if index < 0:
            self.model.setEditText(current)
        self.model.blockSignals(False)

    async def submit(self) -> None:
        # "" removes the stored key; None leaves it unchanged.
        key = "" if self.remove_key.isChecked() else (self.key.text().strip() or None)
        await config.save_provider(
            self._ctx.db, self._ctx.actor, self.name.text(), self.kind.currentData(),
            self.base_url.text(), self.model.currentText(), self.timeout.value(), self.stt_model.text(),
            self.enabled.isChecked(), key, self._provider.id if self._provider else None)


class AITab(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._rows: dict[int, ProviderConfig] = {}
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        server = QLabel(
            "«دستیار کاسپین» از طریق سرور کاسپین و با حساب هر کاربر کار می‌کند و روی این رایانه کلید یا "
            "تنظیمی لازم ندارد. سرویس‌های زیر فقط پشتیبان اختیاری و پیشرفته‌اند.", objectName="Muted")
        server.setWordWrap(True)
        layout.addWidget(server)
        self.local_fallback = QCheckBox("استفاده از سرویس محلی (پیشرفته): اگر سرور کاسپین پاسخ نداد، "
                                        "از سرویس‌های زیر استفاده شود")
        self.local_fallback.toggled.connect(self.on_local_fallback)
        layout.addWidget(self.local_fallback)
        intro = QLabel(
            "سرویس‌ها به ترتیب اولویت امتحان می‌شوند؛ اگر یکی محدود (۴۲۹)، کند یا در دسترس نبود، "
            "بی‌صدا سراغ بعدی می‌رود. بدون اینترنت فقط مدل‌های محلی استفاده می‌شوند و بقیه "
            "برنامه کاملاً آفلاین کار می‌کند.", objectName="Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        rule = QLabel("دستیار فقط پیش‌نویس می‌سازد؛ هر ثبت نهایی (تغییر موجودی) را خودتان در گفتگو با "
                      "دکمه «بله، ثبت کن» تأیید می‌کنید. حذف، ابطال و عملیات حساس همیشه با شماست.",
                      objectName="Muted")
        rule.setWordWrap(True)
        layout.addWidget(rule)
        retention = QHBoxLayout()
        retention.addWidget(QLabel("نگهداری سوابق گفتگو با دستیار:"))
        self.history_days = PersianSpinBox()
        self.history_days.setRange(1, 365)
        self.history_days.setSuffix(" روز")
        self.history_days.editingFinished.connect(self.on_history_days)
        retention.addWidget(self.history_days)
        retention.addStretch(1)
        layout.addLayout(retention)
        toolbar = QHBoxLayout()
        self.status = QLabel(objectName="Muted")
        toolbar.addWidget(self.status, 1)
        self.up_button = QPushButton("↑ اولویت بالاتر")
        self.down_button = QPushButton("↓ اولویت پایین‌تر")
        self.test_button = QPushButton("تست اتصال")
        self.edit_button = QPushButton("ویرایش")
        self.delete_button = QPushButton("حذف")
        self.add_button = QPushButton("افزودن سرویس")
        self.add_button.setProperty("variant", "primary")
        for b, slot in ((self.up_button, lambda: self.on_move(-1)),
                        (self.down_button, lambda: self.on_move(1)),
                        (self.test_button, self.on_test), (self.edit_button, self.on_edit),
                        (self.delete_button, self.on_delete), (self.add_button, self.on_add)):
            b.clicked.connect(slot)
            toolbar.addWidget(b)
        layout.addLayout(toolbar)
        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(("اولویت", "نام", "نوع", "مدل", "آدرس", "کلید روی این رایانه",
                                "گفتار", "وضعیت"))
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self.on_edit())
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        layout.addWidget(McpSection(ctx))
        self._update_buttons()

    def _update_buttons(self) -> None:
        selected = self.table.selected_id() is not None
        for b in (self.up_button, self.down_button, self.test_button, self.edit_button,
                  self.delete_button):
            b.setEnabled(selected)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    @asyncSlot()
    async def on_history_days(self) -> None:
        try:
            await chat_history.set_retention_days(self._ctx.db, self._ctx.actor, self.history_days.value())
        except ServiceError as exc:
            show_error(self, exc.message)

    @asyncSlot()
    async def on_local_fallback(self, enabled: bool) -> None:
        if self._loading:
            return
        try:
            await server_gateway.set_local_fallback(self._ctx.db, self._ctx.actor, enabled)
        except ServiceError as exc:
            show_error(self, exc.message)

    @asyncSlot()
    async def refresh(self) -> None:
        self._loading = True
        self.local_fallback.setChecked(await server_gateway.local_fallback(self._ctx.db))
        self._loading = False
        self.history_days.setValue(await chat_history.retention_days(self._ctx.db))
        rows = await config.list_providers(self._ctx.db)
        self._rows = {p.id: p for p in rows}
        theme = self._ctx.themes.current
        self.table.set_rows(
            [(p.id, (to_persian_digits(i + 1), p.name, PRESETS[p.kind].label, p.model, p.base_url,
                     "دارد" if p.has_key else ("لازم نیست" if not p.needs_key else "ندارد"),
                     "بله" if p.stt_model else "—", "فعال" if p.enabled else "غیرفعال"))
             for i, p in enumerate(rows)],
            muted=[not p.enabled for p in rows],
            highlight={(i, 5): theme.danger for i, p in enumerate(rows)
                       if p.needs_key and not p.has_key})
        online = await self._ctx.ai.is_online()
        usable = sum(p.usable for p in rows)
        text = (f"اینترنت: {'متصل' if online else 'قطع'} — "
                f"{to_persian_digits(usable)} سرویس آماده روی این رایانه")
        if not rows:
            text += (" — هنوز سرویسی اضافه نشده؛ «افزودن سرویس» را بزنید "
                     "(Gemini و OpenRouter سطح رایگان دارند).")
        self.status.setText(text)
        self._update_buttons()

    def selected(self) -> ProviderConfig | None:
        pid = self.table.selected_id()
        return self._rows.get(pid) if pid is not None else None

    @asyncSlot()
    async def on_add(self) -> None:
        if await exec_dialog(ProviderDialog(self._ctx, parent=self)):
            await self.refresh()

    @asyncSlot()
    async def on_edit(self) -> None:
        if (p := self.selected()) and await exec_dialog(ProviderDialog(self._ctx, p, self)):
            await self.refresh()

    @asyncSlot()
    async def on_delete(self) -> None:
        if (p := self.selected()) is None:
            return
        await config.delete_provider(self._ctx.db, self._ctx.actor, p.id)
        await self.refresh()

    @asyncSlot()
    async def on_move(self, direction: int) -> None:
        if (p := self.selected()) is None:
            return
        await config.move_provider(self._ctx.db, self._ctx.actor, p.id, direction)
        await self.refresh()
        self.table.select_id(p.id)

    @asyncSlot()
    async def on_test(self) -> None:
        if (p := self.selected()) is None:
            return
        self.test_button.setEnabled(False)
        try:
            ok, message, seconds = await self._ctx.ai.test_provider(p)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        finally:
            self.test_button.setEnabled(True)
        text = f"«{ltr(p.name)}»: {message} ({to_persian_digits(f'{seconds:.1f}')} ثانیه)"
        (show_info if ok else show_error)(self, text)


class ServerTab(QWidget):
    """Caspian Server (admin only). Everyone signs in with their own account (mobile + password);
    the old shared account below keeps working until the admin switches it off on the server.
    Passwords are kept only in this PC's Windows Credential Manager."""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card()
        card.body.addWidget(QLabel("اتصال به سرور کاسپین (درخواست کالا)", objectName="CardTitle"))
        intro = QLabel("هر کارمند با شماره موبایل و رمز خودش وارد برنامه می‌شود (ثبت‌نام با کد پیامک، "
                       "سپس تأیید مدیر در پنل سرور) و درخواست‌هایش با نام خودش فرستاده می‌شود. "
                       "رمزها فقط روی همین رایانه، در بخش امن رمزهای ویندوز، ذخیره می‌شوند.\n"
                       "اگر هنوز سرور ندارید، این بخش را خالی بگذارید: برنامه بدون سرور کامل کار می‌کند "
                       "و درخواست‌های کالا روی همین رایانه می‌مانند تا بعداً ارسال شوند.",
                       objectName="Muted")
        intro.setWordWrap(True)
        card.body.addWidget(intro)

        card.body.addWidget(QLabel("حساب مشترک (قدیمی)", objectName="CardTitle"))
        legacy = QLabel("حساب «انبار» که همه با آن کار می‌کردند. درخواست‌های کسانی که هنوز حساب شخصی "
                        "ندارند با این حساب فرستاده می‌شود. وقتی همه حساب شخصی گرفتند می‌توانید آن را "
                        "خالی بگذارید.", objectName="Muted")
        legacy.setWordWrap(True)
        card.body.addWidget(legacy)
        form = QFormLayout()
        form.setSpacing(10)
        self.username = ltr_field("warehouse")
        form.addRow("نام کاربری:", self.username)
        self.password = password_field()
        form.addRow("رمز عبور:", self.password)
        card.body.addLayout(form)

        self.advanced_button = QPushButton("تنظیمات پیشرفته ▾")
        self.advanced_button.setProperty("variant", "link")
        self.advanced_button.setCheckable(True)
        card.body.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignRight)
        self.advanced = QWidget()
        advanced = QFormLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.url = ltr_field(DEFAULT_SERVER_URL)
        self.url.setPlaceholderText(DEFAULT_SERVER_URL)
        self.url.textChanged.connect(self._update_warning)
        advanced.addRow("نشانی سرور:", self.url)
        self.advanced.hide()
        self.advanced_button.toggled.connect(self.advanced.setVisible)
        card.body.addWidget(self.advanced)
        self.warning = QLabel("هشدار: با http رمز بدون رمزنگاری از شبکه عبور می‌کند؛ برای سرور اینترنتی "
                              "از https استفاده کنید.", objectName="StatusText")
        self.warning.setProperty("error", True)
        self.warning.setWordWrap(True)
        self.warning.hide()
        card.body.addWidget(self.warning)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.test_button = QPushButton("آزمایش اتصال")
        self.test_button.clicked.connect(self.on_test)
        self.save_button = QPushButton("ذخیره")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.on_save)
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.save_button)
        card.body.addLayout(buttons)
        layout.addWidget(card)
        layout.addStretch(1)
        self._saved: server_connection.ServerConnection | None = None

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.load()

    @asyncSlot()
    async def load(self) -> None:
        self._saved = await server_connection.get_connection(self._ctx.db)
        self.url.setText(await server_connection.get_url(self._ctx.db) or DEFAULT_SERVER_URL)
        if self._saved is not None:
            self.url.setText(self._saved.url)
            self.username.setText(self._saved.username)
            stored = bool(server_connection.get_password(self._saved))
            self.password.setPlaceholderText(
                "ذخیره شده — برای تغییر، رمز جدید را وارد کنید" if stored else "")

    def _update_warning(self, *_args) -> None:
        try:
            url = server_connection.clean_url(self.url.text())
        except ServiceError:
            self.warning.hide()
            return
        self.warning.setVisible(server_connection.ServerConnection(url, "").insecure)

    def _password(self) -> str:
        typed = self.password.text()
        if typed:
            return typed
        same = self._saved is not None and self._saved.url == self.url.text().strip().rstrip("/") \
            and self._saved.username == self.username.text().strip()
        return (server_connection.get_password(self._saved) or "") if same else ""

    @asyncSlot()
    async def on_test(self) -> None:
        self.test_button.setEnabled(False)
        try:
            text = await server_connection.test_connection(self.url.text(), self.username.text(),
                                                           self._password())
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        finally:
            self.test_button.setEnabled(True)
        show_info(self, text)

    @asyncSlot()
    async def on_save(self) -> None:
        try:
            await server_connection.save_connection(self._ctx.db, self._ctx.actor, self.url.text(),
                                                    self.username.text(), self.password.text() or None)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        self.password.clear()
        await self.load()
        if self._ctx.server_link is not None:
            self._ctx.server_link.retry()
        show_info(self, "اتصال به سرور ذخیره شد.")


UNSAVED_QUESTION = "تغییرات ذخیره نشده‌اند. اگر ادامه دهید از بین می‌روند. بدون ذخیره ادامه می‌دهید؟"


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.appearance = AppearanceTab(ctx.themes)
        self.theme_combo = self.appearance.theme_combo
        self.ai = AITab(ctx)
        self.messaging = MessagingTab(ctx)
        self.tasks = TasksTab(ctx)
        self.backup = BackupTab(ctx)
        self.fiscal = FiscalTab(ctx)
        self.tabs.addTab(self.appearance, "ظاهر")
        self.ai_index = self.tabs.addTab(self.ai, "هوش مصنوعی (پیشرفته)")
        self.messaging_index = self.tabs.addTab(self.messaging, "پیام‌رسان‌ها")
        self.tasks_index = self.tabs.addTab(self.tasks, "کارهای زمان‌بندی‌شده")
        self.backup_index = self.tabs.addTab(self.backup, "پشتیبان‌گیری")
        self.fiscal_index = self.tabs.addTab(self.fiscal, "سال مالی")
        self.server = ServerTab(ctx)
        self.server_index = self.tabs.addTab(self.server, "اتصال به سرور")
        layout.addWidget(self.tabs)
        ctx.user_changed.connect(self._apply_permissions)
        self._apply_permissions()
        # 4.3: leaving a tab (or the page) with unsaved edits asks first, like the item form.
        self._dirty: set[QWidget] = set()
        self._tracking = False
        self._current = self.tabs.currentIndex()
        self._asking = False
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._tracking:
            return
        self._tracking = True  # after the tabs filled themselves: only the user's edits count
        for tab in (self.ai, self.messaging, self.tasks, self.backup, self.fiscal, self.server):
            track_edits(tab, lambda *_a, t=tab: self._dirty.add(t))
            for button in tab.findChildren(QPushButton):
                if button.text().startswith("ذخیره"):  # «ذخیره…»: saved
                    button.clicked.connect(lambda *_a, t=tab: self._dirty.discard(t))

    def has_unsaved(self) -> bool:
        return bool(self._dirty)

    async def confirm_leave(self) -> bool:
        """True when there is nothing unsaved, or the person chooses to leave without saving."""
        if not self._dirty:
            return True
        if not await confirm(self, UNSAVED_QUESTION, "ادامه بدون ذخیره", danger=True):
            return False
        self._dirty.clear()
        return True

    def _on_tab_changed(self, index: int) -> None:
        previous = self.tabs.widget(self._current)
        self._current = index
        if previous in self._dirty and not self._asking:
            spawn(self._ask_after_switch(previous))

    async def _ask_after_switch(self, previous: QWidget) -> None:
        self._asking = True
        try:
            if not await confirm(self, UNSAVED_QUESTION, "ادامه بدون ذخیره", danger=True):
                self.tabs.setCurrentWidget(previous)  # back to the tab with the edits
                return
            self._dirty.discard(previous)
            reload = getattr(previous, "load", None) or getattr(previous, "refresh", None)
            if callable(reload):
                reload()  # show what is really saved again
        finally:
            self._asking = False

    def _apply_permissions(self, *_args) -> None:
        self.tabs.setTabVisible(self.ai_index, self._ctx.actor.can(Perm.AI_CONFIGURE))
        admin = self._ctx.actor.can(Perm.SETTINGS_EDIT)
        self.tabs.setTabVisible(self.messaging_index, admin)
        self.tabs.setTabVisible(self.tasks_index, admin)
        self.tabs.setTabVisible(self.backup_index, self._ctx.actor.can(Perm.BACKUP_CREATE))
        self.tabs.setTabVisible(self.fiscal_index, self._ctx.actor.can(Perm.YEAR_CLOSE))
        self.tabs.setTabVisible(self.server_index, self._ctx.actor.is_admin)
