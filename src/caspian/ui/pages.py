"""Top-level pages shown in the main window's stacked area."""

import datetime as dt
from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.services import documents, health, items
from caspian.ui.health_dialog import show_health
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
        # Old data that today's validation would refuse (QA round 1 #10); hidden when clean.
        self.health_warning = QPushButton(objectName="HealthWarning")
        self.health_warning.setProperty("variant", "danger")
        self.health_warning.hide()
        self.health_warning.clicked.connect(self.on_health)
        layout.addWidget(self.health_warning)

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
                   "loans": ("documents", "loans")}
        for key, (page, option) in targets.items():
            self.cards[key].clicked.connect(lambda p=page, o=option: self.open_page.emit(p, o))
        self.cards["drafts"].clicked.connect(self._open_pending)
        self._seq = 0
        self.pending = None
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

    @asyncSlot()
    async def on_health(self) -> None:
        await show_health(self._ctx, self)
        await self.refresh()

    def _open_pending(self) -> None:
        """Draft documents first (they hold stock back); otherwise the open imports."""
        if self.pending is None or self.pending.draft_documents or not self.pending.open_imports:
            self.open_page.emit("documents", "drafts")
        else:
            self.open_page.emit("imports", "")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    @asyncSlot()
    async def refresh(self) -> None:
        total, low = await items.count_summary(self._ctx.db)
        self.cards["items"].set_value(to_persian_digits(total))
        self.cards["low_stock"].set_value(to_persian_digits(low))
        self._seq += 1
        seq = self._seq
        pending = await documents.pending_summary(self._ctx.db)
        _drafts, loans = await documents.pending_counts(self._ctx.db)
        if seq != self._seq:  # a newer refresh is running: don't mix numbers from two moments (#25)
            return
        self.pending = pending
        self.cards["drafts"].set_value(to_persian_digits(pending.total), pending.hint)
        self.cards["loans"].set_value(to_persian_digits(loans))
        if not self._ctx.actor.can(Perm.DOCUMENTS_VIEW):
            return
        findings = await health.check(self._ctx.db, self._ctx.actor)
        self.health_warning.setText(f"⚠ {to_persian_digits(len(findings))} مورد داده نیازمند بررسی "
                                    "(تاریخ نامعتبر، موجودی اعشاری یا کالای تکراری) — برای دیدن بزنید")
        self.health_warning.setVisible(bool(findings))
        rows = await documents.list_documents(self._ctx.db, self._ctx.actor, limit=10)
        self.recent.set_rows([(r.id, (r.number_text, r.type_name,
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

