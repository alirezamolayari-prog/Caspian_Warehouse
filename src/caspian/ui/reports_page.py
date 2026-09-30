"""Reports: stock balance, cardex, burn-rate/reorder, loans, user activity."""

import datetime as dt
from collections.abc import Awaitable, Callable
from decimal import Decimal

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import to_ascii_digits, to_persian_digits
from caspian.db.database import Database
from caspian.services import items, master, reports, users
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.excel_export import write_xlsx
from caspian.services.fiscal import get_archive, open_archive
from caspian.services.fiscal_state import load_state
from caspian.services.reports import ReorderParams, ReportTable
from caspian.ui.app_context import AppContext
from caspian.ui.file_dialogs import ask_save_path
from caspian.ui.messages import show_error, show_info
from caspian.ui.printing import output_menu, report_html
from caspian.ui.tasks import spawn
from caspian.ui.widgets import Card, DataTable, JalaliDateEdit, SearchableCombo


def format_cell(value, kind: str) -> str:
    if value is None:
        return ""
    if kind in ("qty", "money"):
        return format_qty(Decimal(value))
    if kind == "int":
        return to_persian_digits(value)
    if kind == "date" and isinstance(value, dt.date):
        text = jalali.format_date(value)
        if isinstance(value, dt.datetime):
            text += " " + to_persian_digits(value.strftime("%H:%M"))
        return text
    return str(value)


def report_to_html(report: ReportTable) -> str:
    rows = [[format_cell(v, c.kind) for v, c in zip(row, report.columns, strict=False)]
            for row in report.rows]
    footer = ""
    if report.totals:
        footer = "جمع: " + " — ".join(
            f"{report.columns[i].title}: {format_cell(v, report.columns[i].kind)}"
            for i, v in report.totals.items())
    return report_html(report.title, report.meta, [c.title for c in report.columns], rows,
                       footer=footer)


class ReportView(QWidget):
    """Filters on top (added by the owner), a table, totals, and export buttons."""

    def __init__(self, ctx: AppContext, run: Callable[[], Awaitable[ReportTable]],
                 multi_select: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx, self._run = ctx, run
        self.report: ReportTable | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        self.filters = QHBoxLayout()
        layout.addLayout(self.filters)
        self.meta = QLabel(objectName="Muted")
        self.meta.setWordWrap(True)
        layout.addWidget(self.meta)
        self.extra_actions = QHBoxLayout()
        self.extra_actions.addStretch(1)
        self.show_button = QPushButton("نمایش گزارش")
        self.show_button.setProperty("variant", "primary")
        self.show_button.clicked.connect(self.refresh)
        self.excel_button = QPushButton("خروجی اکسل")
        self.excel_button.clicked.connect(self.on_excel)
        self.print_button = QPushButton("چاپ / PDF")
        self.print_button.setMenu(output_menu(self, self._html, self._file_name, ctx.settings))
        for b in (self.excel_button, self.print_button, self.show_button):
            self.extra_actions.addWidget(b)
        layout.addLayout(self.extra_actions)
        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable([], multi_select=multi_select)
        self.table.set_empty_text("فیلترها را انتخاب کنید و «نمایش گزارش» را بزنید.")
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        self.totals = QLabel(objectName="Muted")
        layout.addWidget(self.totals)
        self._set_export_enabled(False)

    def _set_export_enabled(self, enabled: bool) -> None:
        self.excel_button.setEnabled(enabled)
        self.print_button.setEnabled(enabled)

    def _file_name(self) -> str:
        return f"{self.report.title if self.report else 'report'}.pdf"

    async def _html(self) -> str:
        return report_to_html(self.report)

    @asyncSlot()
    async def refresh(self) -> None:
        try:
            report = await self._run()
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        self.report = report
        cols = report.columns
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels([c.title for c in cols])
        ids = report.row_ids or list(range(len(report.rows)))
        self.table.set_rows([(ids[i], [format_cell(v, c.kind) for v, c in zip(row, cols, strict=False)])
                             for i, row in enumerate(report.rows)])
        self.meta.setText(" | ".join(report.meta))
        # Text columns share the free width; numbers and dates fit their content (#31).
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for i, column in enumerate(cols):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch if column.kind == "text"
                                        else QHeaderView.ResizeMode.ResizeToContents)
        self.table.set_empty_text("موردی برای این گزارش پیدا نشد.")
        self.totals.setText(" — ".join(f"{cols[i].title}: {format_cell(v, cols[i].kind)}"
                                       for i, v in report.totals.items()))
        self._set_export_enabled(True)

    @asyncSlot()
    async def on_excel(self) -> None:
        if self.report is None:
            return
        path = await ask_save_path(self, "ذخیره اکسل", self._ctx.settings, f"{self.report.title}.xlsx",
                                   "Excel (*.xlsx)")
        if path:
            write_xlsx(self.report, path)
            show_info(self, "فایل اکسل ذخیره شد.")


def _warehouse_combo(warehouses, with_all: bool = True) -> QComboBox:
    combo = SearchableCombo()
    if with_all:
        combo.addItem("همه انبارها", None)
    for w in warehouses:
        combo.addItem(w.name, w.id)
    return combo


def _spin(value: int, maximum: int = 730) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(0, maximum)
    spin.setValue(value)
    return spin


class ReportsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._loaded = False
        self._rates: dict[int, reports.BurnRate] = {}
        self._archives: dict[int, Database] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        year_row = QHBoxLayout()
        year_row.addWidget(QLabel("سال مالی:"))
        self.year = QComboBox()
        self.year.addItem("جاری", None)
        self.year.currentIndexChanged.connect(self._on_year_changed)
        year_row.addWidget(self.year)
        self.archive_note = QLabel("نمایش از بایگانی فقط‌خواندنی", objectName="Muted")
        self.archive_note.hide()
        year_row.addWidget(self.archive_note)
        year_row.addStretch(1)
        layout.addLayout(year_row)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # stock balance
        self.stock = ReportView(ctx, self._run_stock)
        self.stock_wh = SearchableCombo()
        self.stock_cat = SearchableCombo()
        self.stock_cat.setMinimumWidth(180)
        self.stock_zero = QCheckBox("نمایش کالاهای بدون موجودی")
        for w in (QLabel("انبار:"), self.stock_wh, QLabel("گروه:"), self.stock_cat, self.stock_zero):
            self.stock.filters.addWidget(w)
        self.stock.filters.addStretch(1)
        self.tabs.addTab(self.stock, "موجودی کالا")

        # cardex
        self.cardex = ReportView(ctx, self._run_cardex)
        # One field: type code, name or scan a barcode (#11).
        self.cardex_item = SearchableCombo("کد، نام یا بارکد کالا…")
        self.cardex_item.setMinimumWidth(300)
        self.cardex_item.activated.connect(self._on_cardex_item_chosen)
        self.cardex_item.unmatched.connect(self.on_cardex_lookup)
        self.cardex_wh = SearchableCombo()
        self.cardex_from = JalaliDateEdit(dt.date.today() - dt.timedelta(days=90))
        self.cardex_to = JalaliDateEdit()
        for w in (QLabel("کالا:"), self.cardex_item, QLabel("انبار:"),
                  self.cardex_wh, QLabel("از:"), self.cardex_from, QLabel("تا:"), self.cardex_to):
            self.cardex.filters.addWidget(w)
        self.cardex.filters.addStretch(1)
        self.tabs.addTab(self.cardex, "کاردکس کالا")

        # burn rate
        self.burn = ReportView(ctx, self._run_burn, multi_select=True)
        self.lookback = _spin(90)
        self.lead = _spin(14)
        self.cover = _spin(60)
        self.safety = _spin(7)
        self.burn_wh = SearchableCombo()
        self.only_order = QCheckBox("فقط کالاهای نیازمند سفارش")
        self.only_order.setChecked(True)
        for w in (QLabel("بازه مصرف (روز):"), self.lookback, QLabel("زمان تحویل:"), self.lead,
                  QLabel("پوشش سفارش:"), self.cover, QLabel("ذخیره اطمینان:"), self.safety,
                  QLabel("انبار:"), self.burn_wh, self.only_order):
            self.burn.filters.addWidget(w)
        self.burn.filters.addStretch(1)
        self.apply_rp = QPushButton("اعمال نقطه سفارش پیشنهادی برای ردیف‌های انتخاب‌شده")
        self.apply_rp.clicked.connect(self.on_apply_reorder_points)
        self.burn.extra_actions.insertWidget(0, self.apply_rp)
        self.tabs.addTab(self.burn, "تحلیل مصرف و سفارش")

        # loans
        self.loans = ReportView(ctx, self._run_loans)
        self.loans.filters.addStretch(1)
        self.tabs.addTab(self.loans, "امانی‌های باز")

        # user activity
        self.activity = ReportView(ctx, self._run_activity)
        self.activity_user = SearchableCombo()
        self.activity_from = JalaliDateEdit(dt.date.today() - dt.timedelta(days=7))
        self.activity_to = JalaliDateEdit()
        for w in (QLabel("کاربر:"), self.activity_user, QLabel("از:"), self.activity_from,
                  QLabel("تا:"), self.activity_to):
            self.activity.filters.addWidget(w)
        self.activity.filters.addStretch(1)
        self.activity_index = self.tabs.addTab(self.activity, "فعالیت کاربران")

        ctx.user_changed.connect(self._apply_permissions)
        self._apply_permissions()

    def _apply_permissions(self, *_args) -> None:
        actor = self._ctx.actor
        stock = actor.can(Perm.STOCK_VIEW)
        for view in (self.stock, self.cardex, self.burn):
            self.tabs.setTabVisible(self.tabs.indexOf(view), stock)
        self.tabs.setTabVisible(self.activity_index, actor.can(Perm.USERS_MANAGE))
        self.apply_rp.setVisible(actor.can(Perm.ITEMS_EDIT))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.load_filters()

    @asyncSlot()
    async def load_filters(self) -> None:
        db = self._ctx.db
        self.year.blockSignals(True)
        self.year.clear()
        self.year.addItem("جاری", None)
        for archive in (await load_state(db)).archive_list():
            self.year.addItem(f"{to_persian_digits(archive.year)} (بایگانی)", archive.year)
        self.year.blockSignals(False)
        warehouses = await master.list_warehouses(db)
        for combo in (self.stock_wh, self.cardex_wh, self.burn_wh):
            combo.clear()
            combo.addItem("همه انبارها", None)
            for w in warehouses:
                combo.addItem(w.name, w.id)
        self.stock_cat.set_items(((c.name, c.id) for c in await master.list_categories(db)),
                                 none_text="همه گروه‌ها")
        await self._load_cardex_items()
        if self._ctx.actor.can(Perm.USERS_MANAGE):
            self.activity_user.clear()
            self.activity_user.addItem("همه کاربران", None)
            for u in await users.list_users(db, self._ctx.actor):
                self.activity_user.addItem(u.full_name or u.username, u.id)

    # ----- fiscal year -----

    async def _db(self) -> Database:
        """The active database, or a read-only archive of a closed year."""
        year = self.year.currentData()
        if year is None:
            return self._ctx.db
        if year not in self._archives:
            archive = await get_archive(self._ctx.db, year)
            self._archives[year] = open_archive(self._ctx.db, self._ctx.db_config,
                                                self._ctx.db_password, archive)
        return self._archives[year]

    def _on_year_changed(self, *_args) -> None:
        year = self.year.currentData()
        self.archive_note.setVisible(year is not None)
        spawn(self._load_cardex_items())  # an archive has its own item list
        # Date filters follow the chosen year so an archive doesn't look empty.
        if year is None:
            start, end = dt.date.today() - dt.timedelta(days=90), dt.date.today()
            act_start = dt.date.today() - dt.timedelta(days=7)
        else:
            start, end = jalali.fiscal_year_bounds(year)
            act_start = start
        self.cardex_from.set_date(start)
        self.cardex_to.set_date(end)
        self.activity_from.set_date(act_start)
        self.activity_to.set_date(end)
        for view in (self.stock, self.cardex, self.burn, self.loans, self.activity):
            view.table.setRowCount(0)
            view.report = None

    # ----- runners -----

    def _params(self) -> ReorderParams:
        return ReorderParams(max(self.lookback.value(), 1), self.lead.value(), self.cover.value(),
                             self.safety.value())

    async def _run_stock(self) -> ReportTable:
        return await reports.stock_balance(await self._db(), self._ctx.actor, self.stock_wh.currentData(),
                                           self.stock_cat.currentData(), self.stock_zero.isChecked())

    async def _run_cardex(self) -> ReportTable:
        item_id = self.cardex_item.currentData()
        if item_id is None:
            raise ValidationError("ابتدا کالا را جستجو و انتخاب کنید.")
        return await reports.cardex(await self._db(), self._ctx.actor, item_id,
                                    self.cardex_wh.currentData(), self.cardex_from.date(),
                                    self.cardex_to.date())

    async def _run_burn(self) -> ReportTable:
        params = self._params()
        rates = await reports.burn_rates(await self._db(), self._ctx.actor, params,
                                         self.burn_wh.currentData(), self.only_order.isChecked())
        self._rates = {r.item_id: r for r in rates}
        return reports.burn_rate_table(rates, params)

    async def _run_loans(self) -> ReportTable:
        return await reports.loans_report(await self._db(), self._ctx.actor)

    async def _run_activity(self) -> ReportTable:
        return await reports.user_activity(await self._db(), self._ctx.actor,
                                           self.activity_user.currentData(),
                                           self.activity_from.date(), self.activity_to.date())

    async def _load_cardex_items(self) -> None:
        rows = await items.search_items(await self._db(), self._ctx.actor, include_inactive=True,
                                        limit=100_000)
        self.cardex_item.set_items(((f"{r.code} — {r.name}", r.id) for r in rows), none_text="",
                                   current=self.cardex_item.currentData())

    def _on_cardex_item_chosen(self, *_args) -> None:
        if self.cardex_item.currentData() is not None:
            self.cardex.refresh()

    @asyncSlot(str)
    async def on_cardex_lookup(self, text: str) -> None:
        """Enter on text that isn't one entry: a barcode, or a code/name with several matches."""
        rows = await items.search_items(await self._db(), self._ctx.actor, text, include_inactive=True,
                                        limit=50)
        raw = to_ascii_digits(text.strip())
        exact = [r for r in rows if r.code == raw]
        if exact or len(rows) == 1:
            self.cardex_item.select_value((exact or rows)[0].id)
            await self.cardex.refresh()
        elif not rows:
            show_error(self, f"کالایی با «{text}» پیدا نشد.")
        else:
            self.cardex_item.lineEdit().setText(text)
            self.cardex_item.completer().setCompletionPrefix(text)
            self.cardex_item.completer().complete()

    @asyncSlot()
    async def on_apply_reorder_points(self) -> None:
        ids = self.burn.table.selected_ids()
        if not ids:
            show_error(self, "ردیف‌هایی را که می‌خواهید نقطه سفارششان به‌روز شود انتخاب کنید.")
            return
        values = {i: self._rates[i].suggested_reorder_point for i in ids if i in self._rates}
        try:
            changed = await items.set_reorder_points(self._ctx.db, self._ctx.actor, values)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        show_info(self, f"نقطه سفارش {to_persian_digits(changed)} کالا به‌روزرسانی شد.")
        await self.burn.refresh()
