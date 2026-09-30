"""Blind stocktake: start, print count sheets, enter counts, discrepancy report, approve."""

from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.db.models import StocktakeStatus
from caspian.services import master
from caspian.services import stocktake as st
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.stocktake import CountSheet, DiscrepancyReport, StocktakeRow
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.dialogs import Cancelled, FormDialog, ltr_field
from caspian.ui.messages import confirm, show_error, show_info
from caspian.ui.printing import output_menu, report_html
from caspian.ui.widgets import Card, DataTable, QtyEdit, SearchableCombo

LIST_COLUMNS = ("شماره", "انبار", "محدوده", "عنوان", "شروع", "وضعیت", "پیشرفت شمارش")


def sheet_html(sheet: CountSheet) -> str:
    """Blank count sheet: no system quantities, a column to write the count."""
    row = sheet.row
    return report_html(
        f"برگه شمارش انبارگردانی شماره {to_persian_digits(row.number)}",
        [f"انبار: {row.warehouse} — محدوده: {row.category}", row.title],
        ("ردیف", "کد", "نام کالا", "واحد", "مقدار شمارش‌شده", "توضیح"),
        [(to_persian_digits(ln.line_no), ln.code, ln.name, ln.unit, "", "") for ln in sheet.lines],
        widths=(6, 12, 40, 10, 16, 16), blank_columns=(4, 5),
        footer="نام و امضای شمارشگر: ........................................ &nbsp;&nbsp;&nbsp; "
               "نام و امضای ناظر: ........................................",
    )


def report_to_html(report: DiscrepancyReport, only_differences: bool) -> str:
    row = report.row
    lines = report.differences if only_differences else report.lines
    return report_html(
        f"گزارش مغایرت انبارگردانی شماره {to_persian_digits(row.number)}",
        [f"انبار: {row.warehouse} — محدوده: {row.category}",
         f"وضعیت: {row.status_name} — تعداد مغایرت: {to_persian_digits(len(report.differences))}"],
        ("کد", "نام کالا", "واحد", "موجودی سیستم", "شمارش", "مغایرت", "توضیح"),
        [(ln.code, ln.name, ln.unit, format_qty(ln.system_qty), format_qty(ln.counted_qty),
          format_qty(ln.difference), ln.note) for ln in lines],
        widths=(10, 34, 8, 12, 12, 12, 12),
    )


class NewStocktakeDialog(FormDialog):
    def __init__(self, ctx: AppContext, warehouses, categories, parent=None) -> None:
        super().__init__("انبارگردانی جدید",
                         "موجودی سیستم در همین لحظه ثبت (فریز) می‌شود. تا تأیید یا لغو انبارگردانی، "
                         "ثبت و ابطال اسناد کالاهای آن در این انبار قفل است (مگر با تأیید مدیر).",
                         submit_text="شروع انبارگردانی", parent=parent)
        self._ctx = ctx
        self.created_id: int | None = None
        self.warehouse = SearchableCombo()
        for w in warehouses:
            self.warehouse.addItem(w.name, w.id)
        self.add_row("انبار:", self.warehouse)
        self.category = SearchableCombo()
        self.category.addItem("همه کالاها", None)
        for c in categories:
            self.category.addItem(c.name, c.id)
        self.add_row("محدوده:", self.category)
        self.title_edit = self.add_row("عنوان:", QLineEdit())

    async def submit(self) -> None:
        self.created_id = await st.create_stocktake(
            self._ctx.db, self._ctx.actor, self.warehouse.currentData(),
            self.category.currentData(), self.title_edit.text())


class CountDialog(FormDialog):
    """Blind count entry. Scanning a barcode adds 1 to that item's count."""

    def __init__(self, ctx: AppContext, sheet: CountSheet, parent=None) -> None:
        row = sheet.row
        super().__init__(f"شمارش انبارگردانی شماره {to_persian_digits(row.number)}",
                         f"انبار {row.warehouse} — {row.category}. موجودی سیستم نمایش داده "
                         "نمی‌شود (شمارش کور).", submit_text="ثبت نهایی شمارش", parent=parent)
        self.setMinimumSize(860, 640)
        self._ctx, self._sheet = ctx, sheet
        self._row_of: dict[int, int] = {}
        self.qty_edits: dict[int, QtyEdit] = {}
        self.note_edits: dict[int, QLineEdit] = {}

        self.scan = ltr_field()
        self.scan.setPlaceholderText("بارکد یا کد کالا را اسکن / تایپ کنید (هر اسکن = یک عدد)")
        self.scan.returnPressed.connect(self.on_scan)
        self.add_row("اسکن:", self.scan)

        self.table = QTableWidget(len(sheet.lines), 6)
        self.table.setHorizontalHeaderLabels(("ردیف", "کد", "نام کالا", "واحد", "شمارش", "توضیح"))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 140)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(5, 180)
        for r, ln in enumerate(sheet.lines):
            self._row_of[ln.id] = r
            for c, value in enumerate((to_persian_digits(ln.line_no), ln.code, ln.name, ln.unit)):
                self.table.setItem(r, c, QTableWidgetItem(value))
            qty = QtyEdit(ln.counted_qty)
            qty.textChanged.connect(lambda _t: self._update_progress())
            self.qty_edits[ln.id] = qty
            self.note_edits[ln.id] = QLineEdit(ln.note)
            self.table.setCellWidget(r, 4, qty)
            self.table.setCellWidget(r, 5, self.note_edits[ln.id])
        self.body.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.progress = QLabel(objectName="Muted")
        bottom.addWidget(self.progress)
        bottom.addStretch(1)
        self.missing_zero = QCheckBox("شمارش‌نشده‌ها صفر در نظر گرفته شوند")
        bottom.addWidget(self.missing_zero)
        self.body.addLayout(bottom)

        self.save_button = QPushButton("ذخیره موقت")
        self.save_button.clicked.connect(self.on_save)
        self.print_button = QPushButton("برگه شمارش")
        self.print_button.setMenu(output_menu(self, self._sheet_html,
                                              lambda: f"count-sheet-{sheet.row.number}.pdf",
                                              ctx.settings))
        self.buttons.insertWidget(1, self.print_button)
        self.buttons.insertWidget(self.buttons.count() - 1, self.save_button)
        self._inputs += [self.scan, self.save_button]
        self._update_progress()
        self.scan.setFocus()

    def _update_progress(self) -> None:
        done = sum(1 for e in self.qty_edits.values() if e.text().strip())
        self.progress.setText(f"{to_persian_digits(done)} از {to_persian_digits(len(self.qty_edits))}"
                              " ردیف شمارش شده")

    def keyPressEvent(self, event) -> None:
        if self.focusWidget() is self.scan and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    @asyncSlot()
    async def on_scan(self) -> None:
        text = self.scan.text()
        self.scan.clear()
        hit = await st.scan(self._ctx.db, self._sheet.row.id, text)
        if hit is None:
            self.show_status(f"«{text.strip()}» در فهرست این انبارگردانی نیست.")
            return
        line_id = hit.line_id
        edit = self.qty_edits[line_id]
        edit.set_value((edit.value() or Decimal(0)) + hit.factor)  # a carton counts all its pieces
        self.show_status(f"{hit.unit_name}: +{format_qty(hit.factor)}" if hit.factor != 1 else "",
                         is_error=False)
        self.table.selectRow(self._row_of[line_id])
        self.table.scrollToItem(self.table.item(self._row_of[line_id], 0))
        self.scan.setFocus()

    def collect(self) -> dict[int, tuple[Decimal | None, str]]:
        counts = {}
        for line_id, edit in self.qty_edits.items():
            if edit.text().strip() and edit.value() is None:
                raise ValidationError("یکی از مقادیر شمارش عدد معتبر نیست.")
            counts[line_id] = (edit.value(), self.note_edits[line_id].text())
        return counts

    async def _save(self) -> None:
        await st.record_counts(self._ctx.db, self._ctx.actor, self._sheet.row.id, self.collect())

    @asyncSlot()
    async def on_save(self) -> None:
        try:
            await self._save()
        except ServiceError as exc:
            self.show_status(exc.message)
            return
        self.show_status("ذخیره شد.", is_error=False)

    async def _sheet_html(self) -> str:
        return sheet_html(self._sheet)

    async def submit(self) -> None:
        await self._save()
        await st.submit_counts(self._ctx.db, self._ctx.actor, self._sheet.row.id,
                               self.missing_zero.isChecked())


class ReportDialog(FormDialog):
    confirm_discard = False  # nothing to lose on closing

    def __init__(self, ctx: AppContext, report: DiscrepancyReport, parent=None) -> None:
        row = report.row
        super().__init__(f"گزارش مغایرت انبارگردانی شماره {to_persian_digits(row.number)}",
                         f"انبار {row.warehouse} — {row.category} — {row.status_name}",
                         submit_text="تأیید و اعمال اصلاحیه", cancel_text="بستن", parent=parent)
        self.setMinimumSize(900, 620)
        self._ctx, self._report = ctx, report
        self.document_id: int | None = None
        if report.movements_since_snapshot:
            warning = QLabel(
                f"هشدار: پس از شروع انبارگردانی {to_persian_digits(report.movements_since_snapshot)} "
                "سند در این انبار ثبت شده است. اگر کالاها قبل از شمارش جابه‌جا شده‌اند، مغایرت‌ها "
                "ممکن است دقیق نباشند.", objectName="StatusText")
            warning.setProperty("error", True)
            warning.setWordWrap(True)
            self.body.addWidget(warning)
        self.only_diff = QCheckBox("فقط ردیف‌های دارای مغایرت")
        self.only_diff.setChecked(True)
        self.only_diff.toggled.connect(lambda _: self._fill())
        self.body.addWidget(self.only_diff)
        self.table = DataTable(("کد", "نام کالا", "واحد", "موجودی سیستم", "شمارش", "مغایرت", "توضیح"))
        self.body.addWidget(self.table, 1)
        self.summary = QLabel(objectName="Muted")
        self.body.addWidget(self.summary)
        self.pdf_button = QPushButton("چاپ / PDF")
        self.pdf_button.setMenu(output_menu(
            self, self._html, lambda: f"stocktake-{report.row.number}.pdf", ctx.settings))
        self.buttons.insertWidget(1, self.pdf_button)
        can_approve = (row.status == StocktakeStatus.COUNTED
                       and ctx.actor.can(Perm.STOCKTAKE_APPROVE) and ctx.actor.can(Perm.DOCUMENTS_POST))
        self.submit_button.setVisible(can_approve)
        self._fill()

    def _fill(self) -> None:
        lines = self._report.differences if self.only_diff.isChecked() else self._report.lines
        theme = self._ctx.themes.current
        self.table.set_rows(
            [(ln.line_no, (ln.code, ln.name, ln.unit, format_qty(ln.system_qty),
                           format_qty(ln.counted_qty), format_qty(ln.difference), ln.note))
             for ln in lines],
            highlight={(i, 5): theme.danger if ln.difference < 0 else theme.success
                       for i, ln in enumerate(lines) if ln.difference})
        shortage = sum((-d.difference for d in self._report.differences if d.difference < 0), Decimal(0))
        surplus = sum((d.difference for d in self._report.differences if d.difference > 0), Decimal(0))
        self.summary.setText(
            f"{to_persian_digits(len(self._report.differences))} ردیف مغایرت — کسری: "
            f"{format_qty(shortage)} — اضافه: {format_qty(surplus)} (به واحد اصلی هر کالا)")

    async def _html(self) -> str:
        return report_to_html(self._report, self.only_diff.isChecked())

    async def submit(self) -> None:
        n = len(self._report.differences)
        text = (f"اصلاحیه موجودی برای {to_persian_digits(n)} مغایرت ثبت نهایی شود؟ موجودی سیستم با شمارش "
                "یکسان می‌شود." if n else "مغایرتی نیست؛ انبارگردانی بدون سند اصلاحی بسته شود؟")
        if not await confirm(self, text, "تأیید و ثبت اصلاحیه"):
            raise Cancelled
        self.document_id = await st.approve(self._ctx.db, self._ctx.actor, self._report.row.id)


class StocktakePage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._rows: dict[int, StocktakeRow] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        toolbar = QHBoxLayout()
        intro = QLabel("انبارگردانی کور: شمارشگر موجودی سیستم را نمی‌بیند؛ مغایرت‌ها فقط پس از "
                       "ثبت نهایی شمارش برای مدیر نمایش داده می‌شود.", objectName="Muted")
        intro.setWordWrap(True)
        toolbar.addWidget(intro, 1)
        self.count_button = QPushButton("شمارش")
        self.print_button = QPushButton("برگه شمارش")
        self.print_button.setMenu(output_menu(self, self._selected_sheet_html, self._sheet_name,
                                              ctx.settings))
        self.report_button = QPushButton("گزارش مغایرت")
        self.cancel_button = QPushButton("لغو")
        self.new_button = QPushButton("انبارگردانی جدید")
        self.new_button.setProperty("variant", "primary")
        toolbar.addWidget(self.print_button)
        for b, slot in ((self.count_button, self.on_count),
                        (self.report_button, self.on_report), (self.cancel_button, self.on_cancel),
                        (self.new_button, self.on_new)):
            b.clicked.connect(slot)
            toolbar.addWidget(b)
        layout.addLayout(toolbar)
        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(LIST_COLUMNS)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self.on_count())
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        ctx.user_changed.connect(self._update_buttons)
        self._update_buttons()

    def selected(self) -> StocktakeRow | None:
        row_id = self.table.selected_id()
        return self._rows.get(row_id) if row_id is not None else None

    def _update_buttons(self, *_args) -> None:
        row, actor = self.selected(), self._ctx.actor
        approver = actor.can(Perm.STOCKTAKE_APPROVE) and actor.can(Perm.STOCK_VIEW)
        self.report_button.setVisible(approver)
        self.cancel_button.setVisible(actor.can(Perm.STOCKTAKE_APPROVE))
        is_open = row is not None and row.status == StocktakeStatus.OPEN
        self.count_button.setEnabled(is_open)
        self.print_button.setEnabled(row is not None)
        self.report_button.setEnabled(row is not None and row.status in (
            StocktakeStatus.COUNTED, StocktakeStatus.APPROVED))
        self.cancel_button.setEnabled(row is not None and row.status in (
            StocktakeStatus.OPEN, StocktakeStatus.COUNTED))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    @asyncSlot()
    async def refresh(self) -> None:
        try:
            rows = await st.list_stocktakes(self._ctx.db, self._ctx.actor)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        self._rows = {r.id: r for r in rows}
        warn = self._ctx.themes.current.warning
        self.table.set_rows(
            [(r.id, (to_persian_digits(r.number), r.warehouse, r.category, r.title or "—",
                     jalali.format_date(r.snapshot_at), r.status_name,
                     f"{to_persian_digits(r.counted)} / {to_persian_digits(r.total)}"))
             for r in rows],
            muted=[r.status == StocktakeStatus.CANCELLED for r in rows],
            highlight={(i, 5): warn for i, r in enumerate(rows) if r.status == StocktakeStatus.COUNTED})
        self._update_buttons()

    @asyncSlot()
    async def on_new(self) -> None:
        dialog = NewStocktakeDialog(self._ctx, await master.list_warehouses(self._ctx.db),
                                    await master.list_categories(self._ctx.db), self)
        if await exec_dialog(dialog):
            await self.refresh()
            self.table.select_id(dialog.created_id)

    @asyncSlot()
    async def on_count(self) -> None:
        row = self.selected()
        if row is None or row.status != StocktakeStatus.OPEN:
            return
        sheet = await st.count_sheet(self._ctx.db, self._ctx.actor, row.id)
        await exec_dialog(CountDialog(self._ctx, sheet, self))
        await self.refresh()

    async def _selected_sheet_html(self) -> str:
        row = self.selected()
        return sheet_html(await st.count_sheet(self._ctx.db, self._ctx.actor, row.id))

    def _sheet_name(self) -> str:
        row = self.selected()
        return f"count-sheet-{row.number if row else ''}.pdf"

    @asyncSlot()
    async def on_report(self) -> None:
        if (row := self.selected()) is None:
            return
        try:
            report = await st.discrepancy_report(self._ctx.db, self._ctx.actor, row.id)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        dialog = ReportDialog(self._ctx, report, self)
        if await exec_dialog(dialog):
            show_info(self, "اصلاحیه موجودی ثبت شد." if dialog.document_id
                      else "مغایرتی نبود؛ انبارگردانی تأیید شد.")
        await self.refresh()

    @asyncSlot()
    async def on_cancel(self) -> None:
        if (row := self.selected()) is None:
            return
        try:
            await st.cancel(self._ctx.db, self._ctx.actor, row.id)
        except ServiceError as exc:
            show_error(self, exc.message)
        await self.refresh()
