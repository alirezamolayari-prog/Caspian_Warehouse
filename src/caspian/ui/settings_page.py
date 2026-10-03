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
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core.permissions import Perm
from caspian.core.text import ltr, to_persian_digits
from caspian.db.models import ProviderKind
from caspian.services.ai import config
from caspian.services.ai import history as chat_history
from caspian.services.ai.config import PRESETS, ProviderConfig
from caspian.services.errors import ServiceError
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.automation_settings import McpSection, MessagingTab, TasksTab
from caspian.ui.backup_settings import BackupTab
from caspian.ui.dialogs import FormDialog, ltr_field, password_field
from caspian.ui.messages import show_error, show_info
from caspian.ui.theme import ThemeManager
from caspian.ui.widgets import Card, DataTable
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
                         "رایانه (در Windows Credential Manager) ذخیره می‌شود.",
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
        self.timeout = QSpinBox()
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        intro = QLabel(
            "سرویس‌ها به ترتیب اولویت امتحان می‌شوند؛ اگر یکی محدود (429)، کند یا در دسترس نبود، "
            "بی‌صدا سراغ بعدی می‌رود. بدون اینترنت فقط مدل‌های محلی (Ollama) استفاده می‌شوند و بقیه "
            "برنامه کاملاً آفلاین کار می‌کند.", objectName="Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.allow_post = QCheckBox("اجازه ثبت نهایی سند توسط دستیار (با همه کنترل‌های موجودی و تاریخ؛ "
                                    "حذف و ابطال هرگز)")
        self.allow_post.setToolTip("خاموش: دستیار فقط پیش‌نویس می‌سازد و ثبت نهایی با کاربر است.")
        self.allow_post.toggled.connect(self.on_allow_post)
        layout.addWidget(self.allow_post)
        retention = QHBoxLayout()
        retention.addWidget(QLabel("نگهداری سوابق گفتگو با دستیار:"))
        self.history_days = QSpinBox()
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

    @asyncSlot(bool)
    async def on_allow_post(self, allowed: bool) -> None:
        try:
            await config.set_ai_may_post(self._ctx.db, self._ctx.actor, allowed)
        except ServiceError as exc:
            show_error(self, exc.message)

    @asyncSlot()
    async def refresh(self) -> None:
        self.allow_post.blockSignals(True)
        self.allow_post.setChecked(await config.ai_may_post(self._ctx.db))
        self.allow_post.blockSignals(False)
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
        self.ai_index = self.tabs.addTab(self.ai, "هوش مصنوعی")
        self.messaging_index = self.tabs.addTab(self.messaging, "پیام‌رسان‌ها")
        self.tasks_index = self.tabs.addTab(self.tasks, "کارهای زمان‌بندی‌شده")
        self.backup_index = self.tabs.addTab(self.backup, "پشتیبان‌گیری")
        self.fiscal_index = self.tabs.addTab(self.fiscal, "سال مالی")
        layout.addWidget(self.tabs)
        ctx.user_changed.connect(self._apply_permissions)
        self._apply_permissions()

    def _apply_permissions(self, *_args) -> None:
        self.tabs.setTabVisible(self.ai_index, self._ctx.actor.can(Perm.AI_CONFIGURE))
        admin = self._ctx.actor.can(Perm.SETTINGS_EDIT)
        self.tabs.setTabVisible(self.messaging_index, admin)
        self.tabs.setTabVisible(self.tasks_index, admin)
        self.tabs.setTabVisible(self.backup_index, self._ctx.actor.can(Perm.BACKUP_CREATE))
        self.tabs.setTabVisible(self.fiscal_index, self._ctx.actor.can(Perm.YEAR_CLOSE))
