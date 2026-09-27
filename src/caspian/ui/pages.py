"""Top-level pages shown in the main window's stacked area."""

import datetime as dt
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from caspian.core import jalali
from caspian.ui.theme import ThemeManager
from caspian.ui.widgets import Card, EmptyState, StatCard


@dataclass(frozen=True)
class PageSpec:
    key: str
    title: str
    icon: str
    coming_in: str = ""  # milestone that implements the page, while it's a placeholder


PAGES: tuple[PageSpec, ...] = (
    PageSpec("dashboard", "داشبورد", "layout-dashboard"),
    PageSpec("items", "کالاها", "package", "M3"),
    PageSpec("documents", "اسناد انبار", "arrow-left-right", "M4"),
    PageSpec("imports", "ورود اطلاعات", "file-input", "M5"),
    PageSpec("stocktake", "انبارگردانی", "clipboard-check", "M6"),
    PageSpec("reports", "گزارش‌ها", "chart-column", "M7"),
    PageSpec("assistant", "دستیار هوشمند", "sparkles", "M9"),
    PageSpec("settings", "تنظیمات", "settings"),
)


class DashboardPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        greeting = QLabel(f"امروز {jalali.format_long(dt.date.today())}", objectName="Muted")
        layout.addWidget(greeting)

        grid = QGridLayout()
        grid.setSpacing(16)
        self.cards = {
            "items": StatCard("تعداد کالاها", hint="کالاهای فعال"),
            "low_stock": StatCard("زیر نقطه سفارش", hint="نیازمند خرید"),
            "loans": StatCard("امانی‌های باز", hint="در انتظار بازگشت"),
            "drafts": StatCard("پیش‌نویس‌های در انتظار", hint="نیازمند تأیید"),
        }
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, 0, i)
        layout.addLayout(grid)

        activity = Card()
        activity.body.addWidget(QLabel("فعالیت‌های اخیر", objectName="CardTitle"))
        activity.body.addWidget(
            EmptyState(
                "هنوز فعالیتی ثبت نشده",
                "آخرین اسناد و تغییرات اینجا نمایش داده می‌شوند.",
            )
        )
        layout.addWidget(activity, 1)


class PlaceholderPage(QWidget):
    def __init__(self, spec: PageSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = Card()
        card.body.addWidget(
            EmptyState(spec.title, f"این بخش در مرحله {spec.coming_in} نقشه راه ساخته می‌شود.")
        )
        layout.addWidget(card)


class SettingsPage(QWidget):
    THEME_OPTIONS = (("system", "هماهنگ با ویندوز"), ("light", "روشن"), ("dark", "تیره"))

    def __init__(self, themes: ThemeManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._themes = themes
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        card = Card()
        card.body.addWidget(QLabel("ظاهر برنامه", objectName="CardTitle"))
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
