"""Top-level pages shown in the main window's stacked area."""

import datetime as dt
from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.services import documents, imports, items
from caspian.ui.widgets import Card, DataTable, EmptyState, StatCard


@dataclass(frozen=True)
class PageSpec:
    key: str
    title: str
    icon: str
    coming_in: str = ""  # milestone that implements the page, while it's a placeholder
    perm: Perm | None = None  # hidden from users without this permission
    bottom: bool = False  # pinned to the bottom of the sidebar


PAGES: tuple[PageSpec, ...] = (
    PageSpec("dashboard", "داشبورد", "layout-dashboard"),
    PageSpec("items", "کالاها", "package", perm=Perm.ITEMS_VIEW),
    PageSpec("master", "اطلاعات پایه", "database", perm=Perm.ITEMS_VIEW),
    PageSpec("documents", "اسناد انبار", "arrow-left-right", perm=Perm.DOCUMENTS_VIEW),
    PageSpec("imports", "ورود اطلاعات", "file-input", perm=Perm.IMPORT_RUN),
    PageSpec("stocktake", "انبارگردانی", "clipboard-check", perm=Perm.STOCKTAKE_RUN),
    PageSpec("reports", "گزارش‌ها", "chart-column", perm=Perm.REPORTS_VIEW),
    PageSpec("assistant", "دستیار هوشمند", "sparkles", perm=Perm.AI_USE),
    PageSpec("users", "کاربران", "users", perm=Perm.USERS_MANAGE, bottom=True),
    PageSpec("settings", "تنظیمات", "settings", bottom=True),
)


class DashboardPage(QWidget):
    open_page = Signal(str, str)  # page key, option (e.g. "low_stock")

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
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
            "drafts": StatCard("پیش‌نویس‌های در انتظار", hint="اسناد ثبت‌نشده"),
        }
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, 0, i)
        targets = {"items": ("items", ""), "low_stock": ("items", "low_stock"),
                   "loans": ("documents", "loans"), "drafts": ("imports", "")}
        for key, (page, option) in targets.items():
            self.cards[key].clicked.connect(lambda p=page, o=option: self.open_page.emit(p, o))
        layout.addLayout(grid)

        activity = Card()
        activity.body.addWidget(QLabel("آخرین اسناد انبار", objectName="CardTitle"))
        self.recent = DataTable(("شماره", "نوع سند", "تاریخ", "طرف حساب", "وضعیت"))
        self.recent.hide()
        activity.body.addWidget(self.recent)
        self.empty = EmptyState("هنوز سندی ثبت نشده",
                                "آخرین اسناد انبار اینجا نمایش داده می‌شوند.")
        activity.body.addWidget(self.empty)
        layout.addWidget(activity, 1)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    @asyncSlot()
    async def refresh(self) -> None:
        total, low = await items.count_summary(self._ctx.db)
        self.cards["items"].set_value(to_persian_digits(total))
        self.cards["low_stock"].set_value(to_persian_digits(low))
        drafts, loans = await documents.pending_counts(self._ctx.db)
        batches = await imports.open_batch_count(self._ctx.db)
        hint = f"{to_persian_digits(drafts)} سند، {to_persian_digits(batches)} ورود اطلاعات"
        self.cards["drafts"].set_value(to_persian_digits(drafts + batches), hint)
        self.cards["loans"].set_value(to_persian_digits(loans))
        if not self._ctx.actor.can(Perm.DOCUMENTS_VIEW):
            return
        rows = await documents.list_documents(self._ctx.db, self._ctx.actor, limit=10)
        self.recent.set_rows([(r.id, (to_persian_digits(r.number), r.type_name,
                                      jalali.format_date(r.doc_date), r.person or "—",
                                      r.status_name)) for r in rows])
        self.recent.setVisible(bool(rows))
        self.empty.setVisible(not rows)


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

