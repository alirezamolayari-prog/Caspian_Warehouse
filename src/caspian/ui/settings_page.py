"""Settings: appearance (per PC) and AI providers (shared definitions, per-PC keys)."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
from caspian.core.text import to_persian_digits
from caspian.services.ai import config
from caspian.services.ai.config import PRESETS, ProviderConfig
from caspian.services.errors import ServiceError
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.dialogs import FormDialog, ltr_field, password_field
from caspian.ui.messages import show_error, show_info
from caspian.ui.theme import ThemeManager
from caspian.ui.widgets import Card, DataTable


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
        self.model = self.add_row("مدل:", ltr_field(provider.model if provider else ""))
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
        else:
            self._apply_preset()
        self.kind.currentIndexChanged.connect(lambda _: self._apply_preset())

    def _apply_preset(self) -> None:
        preset = PRESETS[self.kind.currentData()]
        self.base_url.setText(preset.base_url)
        self.model.setText(preset.model)
        self.stt_model.setText(preset.stt_model)
        if not self.name.text().strip() or self._provider is None:
            self.name.setText(preset.label)

    async def submit(self) -> None:
        # "" removes the stored key; None leaves it unchanged.
        key = "" if self.remove_key.isChecked() else (self.key.text().strip() or None)
        await config.save_provider(
            self._ctx.db, self._ctx.actor, self.name.text(), self.kind.currentData(),
            self.base_url.text(), self.model.text(), self.timeout.value(), self.stt_model.text(),
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
    async def refresh(self) -> None:
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
        self.status.setText(f"اینترنت: {'متصل' if online else 'قطع'} — "
                            f"{to_persian_digits(usable)} سرویس آماده روی این رایانه")
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
        text = f"«{p.name}»: {message} ({to_persian_digits(f'{seconds:.1f}')} ثانیه)"
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
        self.tabs.addTab(self.appearance, "ظاهر")
        self.ai_index = self.tabs.addTab(self.ai, "هوش مصنوعی")
        layout.addWidget(self.tabs)
        ctx.user_changed.connect(self._apply_permissions)
        self._apply_permissions()

    def _apply_permissions(self, *_args) -> None:
        self.tabs.setTabVisible(self.ai_index, self._ctx.actor.can(Perm.AI_CONFIGURE))
