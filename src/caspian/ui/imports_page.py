"""Draft-first imports: file/scan sources, column mapping, line-by-line review, apply."""

import asyncio
import datetime as dt
from collections import OrderedDict
from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import to_ascii_digits, to_persian_digits
from caspian.db.models import BatchStatus, DocType, ImportKind, ImportSource, LineStatus, Resolution
from caspian.services import imports, items, master
from caspian.services.ai.assistant import text_to_draft
from caspian.services.documents import DOC_TYPE_NAMES
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.import_files import (
    FIELDS,
    RawRow,
    TableData,
    detect_mapping,
    read_file,
    rows_from_table,
)
from caspian.services.imports import (
    KIND_NAMES,
    RESOLUTION_NAMES,
    SOURCE_NAMES,
    STATUS_NAMES,
    BatchDetail,
    LineRow,
)
from caspian.services.protected import ProtectedAction
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.auth_dialogs import request_approval
from caspian.ui.dialogs import FormDialog, ltr_field
from caspian.ui.file_dialogs import ask_open_path
from caspian.ui.master_page import person_picker
from caspian.ui.messages import ask, show_error, show_info
from caspian.ui.widgets import Card, DataTable, QtyEdit, SearchableCombo

STOCK_DOC_TYPES = (DocType.RECEIPT, DocType.ISSUE, DocType.OPENING, DocType.LOAN_OUT)
PREVIEW_ROWS = 8


def _status_colors(ctx: AppContext) -> dict[LineStatus, str]:
    t = ctx.themes.current
    return {LineStatus.NEW: t.primary, LineStatus.EXISTING_MATCH: t.success,
            LineStatus.CONFLICT: t.warning, LineStatus.ERROR: t.danger,
            LineStatus.IGNORED: t.text_muted}


class _TargetFields:
    """Document type / warehouse / person selectors for stock imports."""

    def __init__(self, dialog: FormDialog, warehouses, persons) -> None:
        self.doc_type = QComboBox()
        for t in STOCK_DOC_TYPES:
            self.doc_type.addItem(DOC_TYPE_NAMES[t], t)
        self.warehouse = SearchableCombo()
        self.warehouse.set_items((w.name, w.id) for w in warehouses)
        self.person = person_picker(dialog._ctx, dialog, persons)
        self.rows = [dialog.add_row("نوع سند:", self.doc_type),
                     dialog.add_row("انبار:", self.warehouse),
                     dialog.add_row("طرف حساب:", self.person)]

    def set_visible(self, visible: bool, form) -> None:
        for widget in self.rows:
            form.setRowVisible(widget, visible)


# ----- sources -----


class FileImportDialog(FormDialog):
    def __init__(self, ctx: AppContext, warehouses, persons, parent=None) -> None:
        super().__init__("ورود اطلاعات از فایل",
                         "فایل اکسل (.xlsx)، CSV یا Word (.docx) را انتخاب کنید. برای فهرست‌های "
                         "دست‌نویس، ابتدا عکس را با یک ابزار هوش مصنوعی به اکسل یا Word تبدیل کنید.",
                         submit_text="ساخت پیش‌نویس", parent=parent)
        self.setMinimumSize(860, 560)
        self._ctx = ctx
        self.table_data: TableData | None = None
        self.batch_id: int | None = None

        file_row = QHBoxLayout()
        self.file_label = QLabel("فایلی انتخاب نشده", objectName="Muted")
        self.browse = QPushButton("انتخاب فایل…")
        self.browse.clicked.connect(self.on_browse)
        file_row.addWidget(self.browse)
        file_row.addWidget(self.file_label, 1)
        self.form.addRow("فایل:", file_row)

        kind_row = QHBoxLayout()
        self.kind_stock = QRadioButton("اقلام یک سند (مثلاً فاکتور خرید)")
        self.kind_items = QRadioButton("تعریف / به‌روزرسانی کالاها")
        group = QButtonGroup(self)
        for b in (self.kind_stock, self.kind_items):
            group.addButton(b)
            kind_row.addWidget(b)
        kind_row.addStretch(1)
        self.kind_stock.setChecked(True)
        self.form.addRow("نوع ورود:", kind_row)
        self.target = _TargetFields(self, warehouses, persons)
        self.kind_stock.toggled.connect(lambda on: self.target.set_visible(on, self.form))

        self.body.addWidget(QLabel("ستون‌ها را با فیلدها تطبیق دهید (پیش‌نمایش چند ردیف اول):",
                                   objectName="CardTitle"))
        self.preview = QTableWidget(0, 0)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.verticalHeader().hide()
        self.preview.setMinimumHeight(240)
        self.body.addWidget(self.preview, 1)
        self.mapping_combos: list[QComboBox] = []

    def load_table(self, table: TableData) -> None:
        self.table_data = table
        self.file_label.setText(f"{table.file_name} — {to_persian_digits(len(table.rows))} ردیف")
        mapping = detect_mapping(table.headers)
        by_column = {col: f for f, col in mapping.items()}
        self.preview.clear()
        self.preview.setColumnCount(len(table.headers))
        self.preview.setRowCount(1 + min(PREVIEW_ROWS, len(table.rows)))
        self.preview.setHorizontalHeaderLabels(table.headers)
        self.preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.mapping_combos = []
        for col in range(len(table.headers)):
            combo = QComboBox()
            combo.addItem("— نادیده —", None)
            for field_name, (label, _syn) in FIELDS.items():
                combo.addItem(label, field_name)
            combo.setCurrentIndex(max(combo.findData(by_column.get(col)), 0))
            self.preview.setCellWidget(0, col, combo)
            self.mapping_combos.append(combo)
            for r, cells in enumerate(table.rows[:PREVIEW_ROWS], start=1):
                self.preview.setItem(r, col, QTableWidgetItem(cells[col]))
        self.preview.setRowHeight(0, 40)

    def mapping(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for col, combo in enumerate(self.mapping_combos):
            field_name = combo.currentData()
            if field_name is None:
                continue
            if field_name in result:
                raise ValidationError(f"فیلد «{FIELDS[field_name][0]}» به دو ستون نسبت داده شده است.")
            result[field_name] = col
        return result

    @asyncSlot()
    async def on_browse(self) -> None:
        path = await ask_open_path(
            self, "انتخاب فایل", "فایل‌های پشتیبانی‌شده (*.xlsx *.csv *.docx);;همه فایل‌ها (*)")
        if not path:
            return
        try:
            table = await asyncio.to_thread(read_file, path)  # big files must not freeze the UI
        except ServiceError as exc:
            self.show_status(exc.message)
            return
        self.show_status("")
        self.load_table(table)

    async def submit(self) -> None:
        if self.table_data is None:
            raise ValidationError("ابتدا فایل را انتخاب کنید.")
        rows = rows_from_table(self.table_data, self.mapping())
        kind = ImportKind.STOCK if self.kind_stock.isChecked() else ImportKind.ITEMS
        stock = kind == ImportKind.STOCK
        self.batch_id = await imports.create_batch(
            self._ctx.db, self._ctx.actor, kind, self.table_data.source, rows,
            self.table_data.file_name,
            self.target.doc_type.currentData() if stock else None,
            self.target.warehouse.currentData() if stock else None,
            self.target.person.currentData() if stock else None,
        )


class ScanDialog(FormDialog):
    """Rapid barcode scanning; each scan adds 1 (repeat scans accumulate)."""

    def __init__(self, ctx: AppContext, warehouses, persons, parent=None) -> None:
        super().__init__("اسکن سریع بارکد",
                         "با اسکنر پشت‌سرهم بخوانید. بارکدهای ناشناخته به‌عنوان کالای جدید "
                         "پیشنهاد می‌شوند و در مرحله بررسی تکمیل می‌کنید.",
                         submit_text="پایان و بررسی", parent=parent)
        self.setMinimumSize(620, 560)
        self._ctx = ctx
        self.batch_id: int | None = None
        self.target = _TargetFields(self, warehouses, persons)
        self.scan = ltr_field()
        self.scan.setPlaceholderText("بارکد را اسکن کنید")
        self.scan.returnPressed.connect(self.on_scan)
        self.add_row("بارکد:", self.scan)
        self.counts: OrderedDict[str, list] = OrderedDict()  # barcode -> [qty, name]
        self.table = DataTable(("بارکد", "کالا", "تعداد"))
        self.body.addWidget(self.table, 1)
        self.scan.setFocus()

    def keyPressEvent(self, event) -> None:
        if self.focusWidget() is self.scan and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    @asyncSlot()
    async def on_scan(self) -> None:
        code = to_ascii_digits(self.scan.text().strip())
        self.scan.clear()
        if not code:
            return
        if code not in self.counts:
            hit = await items.lookup_barcode(self._ctx.db, code)
            name = "— ناشناخته —"
            if hit:
                detail = await items.get_item(self._ctx.db, self._ctx.actor, hit[0])
                name = detail.input.name
            self.counts[code] = [Decimal(0), name]
        self.counts[code][0] += 1
        self.counts.move_to_end(code, last=False)
        self.table.set_rows([(b, (b, name, format_qty(q))) for b, (q, name) in self.counts.items()])

    async def submit(self) -> None:
        if not self.counts:
            raise ValidationError("هنوز بارکدی اسکن نشده است.")
        rows = [RawRow(barcode=b, qty=q, raw={"بارکد": b, "تعداد": str(q)})
                for b, (q, _n) in reversed(self.counts.items())]
        self.batch_id = await imports.create_batch(
            self._ctx.db, self._ctx.actor, ImportKind.STOCK, ImportSource.SCAN, rows,
            f"اسکن {jalali.format_date(dt.date.today())}",
            self.target.doc_type.currentData(), self.target.warehouse.currentData(),
            self.target.person.currentData())


class TextImportDialog(FormDialog):
    """Typed list -> draft. Uses the AI when available, otherwise the offline parser."""

    def __init__(self, ctx: AppContext, warehouses, persons, parent=None) -> None:
        super().__init__("ورود از متن",
                         "هر کالا را در یک خط بنویسید، مثلاً «۵ عدد دریل بوش» یا «پیچ ام‌دی‌اف ۲ کارتن». "
                         "بدون اینترنت هم کار می‌کند.", submit_text="ساخت پیش‌نویس", parent=parent)
        self.setMinimumSize(560, 460)
        self._ctx = ctx
        self.batch_id: int | None = None
        self.used_ai = False
        self.target = _TargetFields(self, warehouses, persons)
        self.target.doc_type.removeItem(self.target.doc_type.findData(DocType.OPENING))
        self.target.doc_type.removeItem(self.target.doc_type.findData(DocType.LOAN_OUT))
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("۵ عدد دریل بوش\n۲ کارتن پیچ ام‌دی‌اف\nچسب چوب ۶ تا")
        self.body.addWidget(self.text, 1)
        self._inputs.append(self.text)

    async def submit(self) -> None:
        if not self.text.toPlainText().strip():
            raise ValidationError("متن را وارد کنید.")
        self.batch_id, self.used_ai = await text_to_draft(
            self._ctx.db, self._ctx.ai, self._ctx.actor, self.text.toPlainText(),
            DocType(self.target.doc_type.currentData()), self.target.warehouse.currentData())


# ----- review -----


class EditLineDialog(FormDialog):
    def __init__(self, ctx: AppContext, kind: ImportKind, line: LineRow, parent=None) -> None:
        super().__init__(f"ویرایش ردیف {to_persian_digits(line.row_no)}", submit_text="ذخیره",
                         parent=parent)
        self._ctx, self._line = ctx, line
        self.code = self.add_row("کد:", ltr_field(line.code))
        self.name = self.add_row("نام کالا:", QLineEdit(line.name))
        self.barcode = self.add_row("بارکد:", ltr_field(line.barcode))
        self.unit = self.add_row("واحد:", QLineEdit(line.unit_name))
        self.qty = self.add_row("مقدار:", QtyEdit(line.qty))
        self.price = self.add_row("فی:", QtyEdit(line.unit_price))
        self.category = self.add_row("گروه:", QLineEdit(line.category_name))
        self.reorder = self.add_row("نقطه سفارش:", QtyEdit(line.reorder_point))
        stock = kind == ImportKind.STOCK
        for w in (self.qty, self.price):
            self.form.setRowVisible(w, stock)
        for w in (self.category, self.reorder):
            self.form.setRowVisible(w, not stock)

    async def submit(self) -> None:
        await imports.update_line(
            self._ctx.db, self._ctx.actor, self._line.id, code=self.code.text(),
            name=self.name.text(), barcode=self.barcode.text(), unit_name=self.unit.text(),
            qty=self.qty.value(), unit_price=self.price.value(),
            category_name=self.category.text(), reorder_point=self.reorder.value())


REVIEW_COLUMNS = ("ردیف", "وضعیت", "کد", "نام", "بارکد", "مقدار", "واحد", "توضیح / کالای منطبق", "اقدام")


class ReviewDialog(FormDialog):
    def __init__(self, ctx: AppContext, batch_id: int, parent=None) -> None:
        super().__init__("بررسی پیش‌نویس", submit_text="اعمال", cancel_text="بستن", parent=parent)
        self.setMinimumSize(1100, 680)
        self._ctx, self._batch_id = ctx, batch_id
        self.detail: BatchDetail | None = None
        self.result_message = ""
        self.result_document_id: int | None = None
        self.summary = QLabel(objectName="Muted")
        self.body.addWidget(self.summary)
        hint = QLabel("برای اصلاح داده‌های یک ردیف، روی آن دوبار کلیک کنید. تا زمانی که «اعمال» "
                      "نزنید، هیچ تغییری در کالاها یا اسناد ایجاد نمی‌شود.", objectName="Muted")
        hint.setWordWrap(True)
        self.body.addWidget(hint)
        self.table = QTableWidget(0, len(REVIEW_COLUMNS))
        self.table.setHorizontalHeaderLabels(REVIEW_COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(8, 260)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.cellDoubleClicked.connect(self.on_edit_row)
        self.body.addWidget(self.table, 1)

    async def reload(self) -> None:
        self.detail = await imports.get_batch(self._ctx.db, self._ctx.actor, self._batch_id)
        row = self.detail.row
        counts = "، ".join(f"{STATUS_NAMES[st]}: {to_persian_digits(n)}"
                           for st, n in row.counts.items() if n)
        target = ""
        if self.detail.doc_type:
            target = f" — مقصد: {DOC_TYPE_NAMES[self.detail.doc_type]} (پیش‌نویس)"
        self.summary.setText(f"{row.title or 'بدون عنوان'} | {KIND_NAMES[row.kind]} از "
                             f"{SOURCE_NAMES[row.source]}{target} | {counts} | "
                             f"نیازمند تصمیم: {to_persian_digits(row.unresolved)}")
        editable = row.status == BatchStatus.OPEN
        self.submit_button.setVisible(editable)
        colors = _status_colors(self._ctx)
        self.table.setRowCount(len(self.detail.lines))
        for r, ln in enumerate(self.detail.lines):
            info = ln.message or (f"← {ln.match_name}" if ln.match_name else "")
            values = (to_persian_digits(ln.row_no), STATUS_NAMES[ln.status], ln.code, ln.name,
                      ln.barcode, format_qty(ln.qty), ln.unit_name, info)
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if c == 1:
                    item.setForeground(QColor(colors[ln.status]))
                self.table.setItem(r, c, item)
            self.table.setCellWidget(r, 8, self._action_combo(ln, editable))

    def _action_combo(self, ln: LineRow, editable: bool) -> QComboBox:
        combo = QComboBox()
        combo.addItem("— تصمیم بگیرید —", None)
        for res in ln.allowed:
            if res in (Resolution.MATCH, Resolution.OVERWRITE) and ln.candidates:
                for item_id, name, score in ln.candidates:
                    verb = "استفاده از" if res == Resolution.MATCH else "بازنویسی"
                    combo.addItem(f"{verb}: {name} ({to_persian_digits(score)}٪)", (res, item_id))
            else:
                combo.addItem(RESOLUTION_NAMES[res], (res, ln.match_item_id))
        current = (ln.resolution, ln.match_item_id) if ln.resolution else None
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data == current or (data and current and data[0] == current[0]
                                   and data[0] in (Resolution.CREATE, Resolution.IGNORE)):
                combo.setCurrentIndex(i)
                break
        combo.setEnabled(editable)
        combo.currentIndexChanged.connect(lambda _i, c=combo, line=ln: self.on_resolution(line, c))
        return combo

    @asyncSlot()
    async def on_resolution(self, line: LineRow, combo: QComboBox) -> None:
        data = combo.currentData()
        if data is None:
            return
        res, item_id = data
        try:
            await imports.set_resolution(self._ctx.db, self._ctx.actor, line.id, res, item_id)
        except ServiceError as exc:
            self.show_status(exc.message)
        await self.reload()

    @asyncSlot()
    async def on_edit_row(self, row: int, _col: int) -> None:
        if self.detail is None or self.detail.row.status != BatchStatus.OPEN:
            return
        line = self.detail.lines[row]
        if await exec_dialog(EditLineDialog(self._ctx, self.detail.row.kind, line, self)):
            await self.reload()

    async def submit(self) -> None:
        approval = None
        if imports.needs_overwrite_approval(self.detail):
            n = sum(ln.resolution == Resolution.OVERWRITE for ln in self.detail.lines)
            approval = await request_approval(
                self._ctx.db, self._ctx.actor, ProtectedAction.IMPORT_OVERWRITE,
                f"بازنویسی اطلاعات {to_persian_digits(n)} کالای موجود با داده‌های فایل.", self)
            if approval is None:
                raise ValidationError("اعمال لغو شد.")
        result = await imports.apply_batch(self._ctx.db, self._ctx.actor, self._batch_id, approval)
        self.result_document_id = result.document_id
        parts = []
        if result.created_items:
            parts.append(f"{to_persian_digits(result.created_items)} کالای جدید ساخته شد")
        if result.updated_items:
            parts.append(f"{to_persian_digits(result.updated_items)} کالا به‌روزرسانی شد")
        if result.document_id:
            parts.append("یک سند پیش‌نویس ساخته شد. تا ثبت نهایی آن، موجودی تغییر نمی‌کند")
        self.result_message = "، ".join(parts) + "." if parts else "تغییری لازم نبود."


# ----- page -----


BATCH_COLUMNS = ("عنوان", "نوع", "منبع", "تاریخ", "ردیف‌ها", "جدید", "منطبق", "تعارض", "خطا", "نیازمند تصمیم")


class ImportsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None, open_document=None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._open_document = open_document  # async (doc_id) -> None, from the main window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem("پیش‌نویس‌های باز", BatchStatus.OPEN)
        self.status_filter.addItem("اعمال‌شده", BatchStatus.APPLIED)
        self.status_filter.addItem("حذف‌شده", BatchStatus.DISCARDED)
        self.status_filter.currentIndexChanged.connect(lambda _: self.refresh())
        toolbar.addWidget(self.status_filter)
        toolbar.addStretch(1)
        self.review_button = QPushButton("بررسی")
        self.review_button.clicked.connect(self.on_review)
        self.discard_button = QPushButton("حذف پیش‌نویس")
        self.discard_button.clicked.connect(self.on_discard)
        self.scan_button = QPushButton("اسکن سریع بارکد")
        self.scan_button.clicked.connect(self.on_scan)
        self.text_button = QPushButton("از متن…")
        self.text_button.clicked.connect(self.on_text)
        self.file_button = QPushButton("ورود از فایل…")
        self.file_button.setProperty("variant", "primary")
        self.file_button.clicked.connect(self.on_file)
        for b in (self.review_button, self.discard_button, self.text_button, self.scan_button,
                  self.file_button):
            toolbar.addWidget(b)
        layout.addLayout(toolbar)

        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(BATCH_COLUMNS)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self.on_review())
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        self._update_buttons()

    def _update_buttons(self) -> None:
        selected = self.table.selected_id() is not None
        self.review_button.setEnabled(selected)
        self.discard_button.setEnabled(selected and self.status_filter.currentData() == BatchStatus.OPEN)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    @asyncSlot()
    async def refresh(self) -> None:
        try:
            rows = await imports.list_batches(self._ctx.db, self._ctx.actor,
                                              self.status_filter.currentData())
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        warn = self._ctx.themes.current.warning
        self.table.set_rows(
            [(r.id, (r.title or "—", KIND_NAMES[r.kind], SOURCE_NAMES[r.source],
                     jalali.format_date(r.created_at), to_persian_digits(r.total),
                     to_persian_digits(r.counts[LineStatus.NEW]),
                     to_persian_digits(r.counts[LineStatus.EXISTING_MATCH]),
                     to_persian_digits(r.counts[LineStatus.CONFLICT]),
                     to_persian_digits(r.counts[LineStatus.ERROR]),
                     to_persian_digits(r.unresolved))) for r in rows],
            highlight={(i, 9): warn for i, r in enumerate(rows) if r.unresolved},
        )
        self._update_buttons()

    async def _targets(self):
        return (await master.list_warehouses(self._ctx.db),
                await master.search_persons(self._ctx.db))

    async def open_review(self, batch_id: int) -> None:
        dialog = ReviewDialog(self._ctx, batch_id, self)
        await dialog.reload()
        if await exec_dialog(dialog):
            if dialog.result_document_id:
                await self._after_apply(dialog.result_document_id, dialog.result_message)
            elif dialog.result_message:
                show_info(self, dialog.result_message)
        await self.refresh()

    async def _after_apply(self, doc_id: int, message: str) -> None:
        """Importing only drafts a document; stock changes once it is posted (#9)."""
        choices = [("open", "باز کردن سند"), ("later", "بعداً")]
        if self._ctx.actor.can(Perm.DOCUMENTS_POST):
            choices.insert(0, ("post", "ثبت نهایی همین حالا"))
        choice = await ask(self, message, choices, "ورود اطلاعات اعمال شد")
        if choice == "post":
            from caspian.services import documents as docs
            from caspian.ui.documents_page import with_stocktake_override

            try:
                await with_stocktake_override(self._ctx, self, lambda approval: docs.post_document(
                    self._ctx.db, self._ctx.actor, doc_id, approval=approval))
            except ServiceError as exc:
                show_error(self, f"{exc.message}\nسند به‌صورت پیش‌نویس باقی ماند.")
                return
            show_info(self, "سند ثبت نهایی شد و موجودی به‌روز شد.")
        elif choice == "open" and self._open_document is not None:
            await self._open_document(doc_id)

    @asyncSlot()
    async def on_file(self) -> None:
        dialog = FileImportDialog(self._ctx, *await self._targets(), self)
        if await exec_dialog(dialog) and dialog.batch_id:
            await self.open_review(dialog.batch_id)

    @asyncSlot()
    async def on_scan(self) -> None:
        dialog = ScanDialog(self._ctx, *await self._targets(), self)
        if await exec_dialog(dialog) and dialog.batch_id:
            await self.open_review(dialog.batch_id)

    @asyncSlot()
    async def on_text(self) -> None:
        dialog = TextImportDialog(self._ctx, *await self._targets(), self)
        if await exec_dialog(dialog) and dialog.batch_id:
            await self.open_review(dialog.batch_id)

    @asyncSlot()
    async def on_review(self) -> None:
        if (batch_id := self.table.selected_id()) is not None:
            await self.open_review(batch_id)

    @asyncSlot()
    async def on_discard(self) -> None:
        if (batch_id := self.table.selected_id()) is None:
            return
        try:
            await imports.discard_batch(self._ctx.db, self._ctx.actor, batch_id)
        except ServiceError as exc:
            show_error(self, exc.message)
        await self.refresh()
