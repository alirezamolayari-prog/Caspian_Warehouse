"""Warehouse documents: list, editor (scanner-friendly), and open loans."""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.db.models import DocStatus, DocType
from caspian.services import documents as docs
from caspian.services import items, master
from caspian.services.documents import (
    DOC_TYPE_NAMES,
    STATUS_NAMES,
    DocumentDetail,
    DocumentInput,
    DocumentRow,
    LineInput,
    LoanRow,
)
from caspian.services.errors import ServiceError, StocktakeFrozen, ValidationError
from caspian.services.items import ItemRow
from caspian.services.protected import ProtectedAction
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.auth_dialogs import request_approval
from caspian.ui.dialogs import Cancelled, FormDialog
from caspian.ui.document_print import document_print_menu
from caspian.ui.master_page import person_picker
from caspian.ui.messages import confirm, show_error, show_info
from caspian.ui.widgets import Card, DataTable, JalaliDateEdit, QtyEdit, SearchableCombo, SearchBox

LIST_COLUMNS = ("شماره", "نوع سند", "تاریخ", "انبار", "طرف حساب", "اقلام", "وضعیت", "ثبت‌کننده")
LOAN_COLUMNS = ("شماره امانی", "تاریخ", "تحویل‌گیرنده", "کالا", "مانده", "روز")
OUTGOING = {DocType.ISSUE, DocType.TRANSFER, DocType.LOAN_OUT}
LINE_COLUMNS = ("کد", "نام کالا", "واحد", "مقدار", "فی", "توضیح", "")


async def with_stocktake_override(ctx: AppContext, parent, action):
    """Run `action(approval)`; if a stocktake freezes the items (#7), offer an admin PIN override
    and retry. Re-raises the freeze error when no approval is given."""
    try:
        return await action(None)
    except StocktakeFrozen as exc:
        approval = await request_approval(
            ctx.db, ctx.actor, ProtectedAction.STOCKTAKE_OVERRIDE,
            f"{exc.message}\nبا وجود انبارگردانی باز ادامه داده شود؟ مغایرت آن انبارگردانی نادرست خواهد شد.",
            parent)
        if approval is None:
            raise
        return await action(approval)


def doc_title(row_type: DocType, number: int | None) -> str:
    name = DOC_TYPE_NAMES[row_type]
    return f"{name} {docs.number_text(row_type, number)}" if number else f"{name} جدید"


# ----- item chooser -----


class ItemChooserDialog(FormDialog):
    """Pick one of several items matching a typed name."""

    confirm_discard = False  # nothing to lose on closing

    def __init__(self, ctx: AppContext, query: str, rows: list[ItemRow], parent=None) -> None:
        super().__init__("انتخاب کالا", f"چند کالا با «{query}» پیدا شد.", submit_text="انتخاب",
                         parent=parent)
        self.setMinimumWidth(560)
        self._rows = {r.id: r for r in rows}
        self.chosen: ItemRow | None = None
        self.table = DataTable(("کد", "نام کالا", "واحد", "موجودی"))
        self.table.setMinimumHeight(280)
        self.table.set_rows([(r.id, (r.code, r.name, r.base_unit, format_qty(r.on_hand) or "—"))
                             for r in rows])
        self.table.selectRow(0)
        self.table.doubleClicked.connect(lambda _: self.submit_button.click())
        self.body.addWidget(self.table)

    async def submit(self) -> None:
        row_id = self.table.selected_id()
        if row_id is None:
            raise ValidationError("یک کالا انتخاب کنید.")
        self.chosen = self._rows[row_id]


# ----- document editor -----


@dataclass
class _Line:
    item_id: int
    code: str
    name: str
    unit: QComboBox
    qty: QtyEdit
    price: QtyEdit
    notes: QLineEdit


class DocumentDialog(FormDialog):
    """Create/edit a draft, or view a posted/cancelled document read-only."""

    def __init__(self, ctx: AppContext, doc_type: DocType, detail: DocumentDetail | None,
                 warehouses, persons, units, loans: list[LoanRow], parent=None) -> None:
        number = detail.number if detail else None
        self._read_only = detail is not None and detail.status != DocStatus.DRAFT
        super().__init__(doc_title(doc_type, number), submit_text="ذخیره و ثبت نهایی",
                         cancel_text="بستن" if self._read_only else "انصراف", parent=parent)
        self.setMinimumSize(900, 620)
        self._ctx, self._type, self._detail = ctx, doc_type, detail
        self._unit_names = {u.id: u.name for u in units}
        self._loans = loans
        self._item_units: dict[int, list[tuple[int, str]]] = {}
        self.lines: list[_Line] = []
        self.saved_id: int | None = detail.id if detail else None
        self.posted = False
        data = detail.input if detail else DocumentInput(doc_type, dt.date.today(),
                                                         warehouses[0].id if warehouses else 0)

        if detail is not None and detail.status != DocStatus.DRAFT:
            self.subtitle_label = QLabel(f"وضعیت: {STATUS_NAMES[detail.status]} — فقط مشاهده",
                                         objectName="Muted")
            self._layout.insertWidget(1, self.subtitle_label)

        header = QHBoxLayout()
        left, right = QFormLayout(), QFormLayout()
        for f in (left, right):
            f.setSpacing(10)
        header.addLayout(right, 1)
        header.addSpacing(24)
        header.addLayout(left, 1)
        self.form.addRow(header)

        self.date = JalaliDateEdit(data.doc_date)
        right.addRow("تاریخ:", self.date)
        self.warehouse = SearchableCombo()
        for w in warehouses:
            self.warehouse.addItem(w.name, w.id)
        self.warehouse.setCurrentIndex(max(self.warehouse.findData(data.warehouse_id), 0))
        right.addRow("از انبار:" if doc_type == DocType.TRANSFER else "انبار:", self.warehouse)
        self.dest = SearchableCombo()
        if doc_type == DocType.TRANSFER:
            for w in warehouses:
                self.dest.addItem(w.name, w.id)
            index = self.dest.findData(data.dest_warehouse_id)
            self.dest.setCurrentIndex(index if index >= 0 else min(1, self.dest.count() - 1))
            right.addRow("به انبار:", self.dest)

        self.person = person_picker(ctx, self, persons, data.person_id)
        person_label = {DocType.RECEIPT: "تأمین‌کننده:", DocType.ISSUE: "تحویل‌گیرنده:",
                        DocType.LOAN_OUT: "تحویل‌گیرنده:", DocType.LOAN_RETURN: "برگشت‌دهنده:"}
        if doc_type in person_label:
            left.addRow(person_label[doc_type], self.person)
        self.loan = QComboBox()
        if doc_type == DocType.LOAN_RETURN:
            left.addRow("سند امانی:", self.loan)
            self.person.currentIndexChanged.connect(lambda _: self._fill_loans())
            self.loan.currentIndexChanged.connect(lambda _: self._on_loan_selected())
            self._fill_loans(data.related_document_id)
        self.description = QLineEdit(data.description)
        left.addRow("توضیحات:", self.description)

        # Line entry: barcode scanner or typed code/name, then Enter.
        entry = QHBoxLayout()
        self.item_input = QLineEdit()
        self.item_input.setPlaceholderText(
            "بارکد را اسکن کنید یا کد / نام کالا را بنویسید و Enter بزنید"
        )
        self.item_input.returnPressed.connect(self.on_item_entered)
        entry.addWidget(self.item_input, 1)
        self.stock_hint = QLabel(objectName="Muted")
        entry.addWidget(self.stock_hint)
        self.body.addLayout(entry)

        self.table = QTableWidget(0, len(LINE_COLUMNS))
        self.table.setHorizontalHeaderLabels(LINE_COLUMNS)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setShowGrid(False)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 90), (2, 110), (3, 110), (4, 120), (6, 40)):
            header_view.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, width)
        self.table.setMinimumHeight(260)
        self.body.addWidget(self.table, 1)
        self.totals = QLabel(objectName="Muted")
        self.body.addWidget(self.totals)

        self._pending_lines = list(detail.lines) if detail else []

        self.draft_button = QPushButton("ذخیره پیش‌نویس")
        self.draft_button.clicked.connect(self.on_save_draft)
        self.buttons.insertWidget(self.buttons.count() - 1, self.draft_button)
        # Prints the saved document (a draft is printed as «پیش‌نویس — فاقد اعتبار»).
        self.print_button = QPushButton("چاپ / پیش‌نمایش")
        self.print_button.setMenu(document_print_menu(self, ctx, lambda: self.saved_id))
        self.print_button.setEnabled(self.saved_id is not None)
        self.buttons.insertWidget(1, self.print_button)
        self._inputs += [self.date, self.warehouse, self.dest, self.person, self.loan,
                         self.description, self.item_input, self.draft_button]
        if self._read_only:
            for w in self._inputs:
                w.setEnabled(False)
            self.submit_button.hide()
            self.draft_button.hide()
        if not ctx.actor.can(Perm.DOCUMENTS_POST):
            self.submit_button.hide()
        self._update_totals()
        self.item_input.setFocus()

    async def load_lines(self) -> None:
        """Existing lines need each item's unit list; loaded once after construction."""
        for v in self._pending_lines:
            await self._ensure_units(v.item_id)
            self._add_line(v.item_id, v.item_code, v.item_name, v.unit_id, v.qty, v.unit_price,
                           v.notes)
        self._pending_lines = []
        if self._read_only:
            for line in self.lines:
                for w in (line.unit, line.qty, line.price, line.notes):
                    w.setEnabled(False)

    def set_busy(self, busy: bool) -> None:
        if self._read_only:
            return
        super().set_busy(busy)

    # ----- loans -----

    def _fill_loans(self, selected: int | None = None) -> None:
        person_id = self.person.currentData()
        self.loan.blockSignals(True)
        self.loan.clear()
        self.loan.addItem("—", None)
        seen = set()
        for r in self._loans:
            if r.person_id == person_id and r.document_id not in seen:
                seen.add(r.document_id)
                self.loan.addItem(f"{docs.number_text(DocType.LOAN_OUT, r.number)} — "
                                  f"{jalali.format_date(r.doc_date)}", r.document_id)
        index = self.loan.findData(selected)
        self.loan.setCurrentIndex(max(index, 0))
        self.loan.blockSignals(False)

    @asyncSlot()
    async def _on_loan_selected(self) -> None:
        loan_id = self.loan.currentData()
        if loan_id is None or self.lines:
            return
        for r in self._loans:
            if r.document_id == loan_id and r.outstanding > 0:
                await self._ensure_units(r.item_id)
                self._add_line(r.item_id, r.item_code, r.item_name, None, r.outstanding)

    # ----- lines -----

    async def _ensure_units(self, item_id: int) -> list[tuple[int, str]]:
        if not self._item_units.get(item_id):
            detail = await items.get_item(self._ctx.db, self._ctx.actor, item_id)
            base = detail.input.base_unit_id
            self._item_units[item_id] = [(base, self._unit_names.get(base, "?"))] + [
                (u, self._unit_names.get(u, "?")) for u, _f in detail.input.units
            ]
        return self._item_units[item_id]

    def _add_line(self, item_id: int, code: str, name: str, unit_id: int | None,
                  qty: Decimal, price: Decimal | None = None, notes: str = "") -> None:
        unit = QComboBox()
        for uid, uname in self._item_units[item_id]:
            unit.addItem(uname, uid)
        unit.setCurrentIndex(max(unit.findData(unit_id), 0))
        qty_edit = QtyEdit(qty)
        qty_edit.textChanged.connect(lambda _: self._update_totals())
        price_edit = QtyEdit(price)
        price_edit.setPlaceholderText("اختیاری")
        price_edit.textChanged.connect(lambda _: self._update_totals())
        notes_edit = QLineEdit(notes)
        line = _Line(item_id, code, name, unit, qty_edit, price_edit, notes_edit)
        self.lines.append(line)

        r = self.table.rowCount()
        self.table.insertRow(r)
        for col, text in ((0, code), (1, name)):
            cell = QTableWidgetItem(text)
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, col, cell)
        self.table.setCellWidget(r, 2, unit)
        self.table.setCellWidget(r, 3, qty_edit)
        self.table.setCellWidget(r, 4, price_edit)
        self.table.setCellWidget(r, 5, notes_edit)
        remove = QToolButton(objectName="IconButton")
        remove.setText("✕")
        remove.setToolTip("حذف ردیف")
        remove.clicked.connect(lambda: self._remove_line(line))
        remove.setEnabled(not self._read_only)
        self.table.setCellWidget(r, 6, remove)
        self._update_totals()

    def _remove_line(self, line: _Line) -> None:
        r = self.lines.index(line)
        self.lines.pop(r)
        self.table.removeRow(r)
        self._update_totals()

    def _update_totals(self) -> None:
        total = Decimal(0)
        for line in self.lines:
            q, p = line.qty.value(), line.price.value()
            if q is not None and p is not None:
                total += q * p
        text = f"{to_persian_digits(len(self.lines))} ردیف"
        if total:
            text += f" — جمع مبلغ: {format_qty(total)}"
        self.totals.setText(text)

    @asyncSlot()
    async def on_item_entered(self) -> None:
        text = self.item_input.text().strip()
        if not text:
            return
        self.show_status("")
        try:
            item_id, unit_id = None, None
            if hit := await items.lookup_barcode(self._ctx.db, text):
                item_id, unit_id = hit
                rows = await items.search_items(self._ctx.db, self._ctx.actor, text)
                row = next((r for r in rows if r.id == item_id), None)
            else:
                rows = await items.search_items(self._ctx.db, self._ctx.actor, text, limit=50)
                exact = [r for r in rows if r.code == text]
                if exact or len(rows) == 1:
                    row = (exact or rows)[0]
                elif not rows:
                    self.show_status(f"کالایی با «{text}» پیدا نشد.")
                    return
                else:
                    chooser = ItemChooserDialog(self._ctx, text, rows, self)
                    if not await exec_dialog(chooser):
                        return
                    row = chooser.chosen
            if row is None:
                self.show_status("کالای این بارکد غیرفعال است.")
                return
            units = await self._ensure_units(row.id)
            unit_id = unit_id or units[0][0]
            # Scanning the same barcode again increments the existing line.
            for line in self.lines:
                if line.item_id == row.id and line.unit.currentData() == unit_id:
                    line.qty.set_value((line.qty.value() or Decimal(0)) + 1)
                    break
            else:
                self._add_line(row.id, row.code, row.name, unit_id, Decimal(1))
            if row.on_hand is not None:
                hint = f"موجودی «{row.name}»: {format_qty(row.on_hand)} {row.base_unit}"
                if self._type in OUTGOING and self.warehouse.currentData() is not None:
                    pending = await docs.pending_incoming(self._ctx.db, self._ctx.actor, row.id,
                                                          self.warehouse.currentData())
                    if pending:
                        hint += " — " + docs.pending_hint(pending, row.base_unit)
                self.stock_hint.setText(hint)
        except ServiceError as exc:
            self.show_status(exc.message)
        finally:
            self.item_input.clear()
            self.item_input.setFocus()

    def keyPressEvent(self, event) -> None:
        # Enter in the scan box adds a line; it must never save the whole document.
        if self.focusWidget() is self.item_input and event.key() in (
                Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    # ----- save -----

    def collect(self) -> DocumentInput:
        date = self.date.date()
        if date is None:
            raise ValidationError("تاریخ نامعتبر است (نمونه: ۱۴۰۵/۰۷/۰۵).")
        lines = []
        for no, line in enumerate(self.lines, start=1):
            if line.qty.value() is None:
                raise ValidationError(f"ردیف {no}: مقدار «{line.name}» را وارد کنید.")
            if line.price.text().strip() and line.price.value() is None:
                raise ValidationError(f"ردیف {no}: فی نامعتبر است.")
            lines.append(LineInput(line.item_id, line.unit.currentData(), line.qty.value(),
                                   line.price.value(), line.notes.text()))
        return DocumentInput(
            doc_type=self._type, doc_date=date, warehouse_id=self.warehouse.currentData(),
            dest_warehouse_id=self.dest.currentData() if self._type == DocType.TRANSFER
            else None,
            person_id=self.person.currentData(),
            related_document_id=self.loan.currentData() if self._type == DocType.LOAN_RETURN
            else None,
            description=self.description.text(), lines=lines,
        )

    async def _save(self) -> int:
        data = self.collect()
        db, actor = self._ctx.db, self._ctx.actor
        if self.saved_id is None:
            self.saved_id = await docs.create_document(db, actor, data)
        else:
            version = self._detail.version_id if self._detail else None
            await docs.update_document(db, actor, self.saved_id, version, data)
        # Later saves in this dialog must compare against the new version.
        self._detail = await docs.get_document(db, actor, self.saved_id)
        self.print_button.setEnabled(True)
        return self.saved_id

    async def submit(self) -> None:
        if not await confirm(self, "سند ذخیره و ثبت نهایی شود؟ موجودی تغییر می‌کند و سند پس از آن فقط با "
                             "ابطال قابل برگشت است.", "ثبت نهایی"):
            raise Cancelled
        doc_id = await self._save()
        await with_stocktake_override(self._ctx, self, lambda approval: docs.post_document(
            self._ctx.db, self._ctx.actor, doc_id, self._detail.version_id, approval))
        self.posted = True

    @asyncSlot()
    async def on_save_draft(self) -> None:
        self.set_busy(True)
        try:
            await self._save()
        except ServiceError as exc:
            self.show_status(exc.message)
            return
        finally:
            self.set_busy(False)
        self.accept()


class CancelDialog(FormDialog):
    confirm_discard = False  # nothing to lose on closing

    def __init__(self, ctx: AppContext, row: DocumentRow, parent=None) -> None:
        super().__init__(f"ابطال {doc_title(row.doc_type, row.number)}",
                         "اثر این سند روی موجودی برگردانده می‌شود و سند به حالت «ابطال شده» "
                         "درمی‌آید. سابقه آن حذف نمی‌شود.", submit_text="ابطال سند", parent=parent)
        self.submit_button.setProperty("variant", "danger")
        self._ctx, self._row = ctx, row
        self.reason = self.add_row("علت ابطال:", QLineEdit())
        self.reason.setPlaceholderText("مثلاً: مقدار اشتباه وارد شده بود")
        # Typing must land in the reason field right away (the default button had focus, #34).
        self.reason.setFocus()

    async def submit(self) -> None:
        if not self.reason.text().strip():
            raise ValidationError("علت ابطال را بنویسید.")
        await with_stocktake_override(self._ctx, self, lambda approval: docs.cancel_document(
            self._ctx.db, self._ctx.actor, self._row.id, self.reason.text(), approval))


# ----- page -----


def _status_text(row: DocumentRow) -> str:
    if row.print_count:
        return f"{row.status_name} — چاپ‌شده ({to_persian_digits(row.print_count)})"
    return row.status_name


class DocumentsList(QWidget):
    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._rows: dict[int, DocumentRow] = {}
        self._seq = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        self.search = SearchBox("جستجو: شماره (مثل ر-۱۲)، طرف حساب یا توضیحات…")
        self.search.search.connect(lambda _: self.refresh())
        toolbar.addWidget(self.search)
        self.type_filter = QComboBox()
        self.type_filter.addItem("همه انواع", None)
        for t, name in DOC_TYPE_NAMES.items():
            self.type_filter.addItem(name, t)
        self.type_filter.currentIndexChanged.connect(lambda _: self.refresh())
        toolbar.addWidget(self.type_filter)
        self.status_filter = QComboBox()
        self.status_filter.addItem("همه وضعیت‌ها", None)
        for st, name in STATUS_NAMES.items():
            self.status_filter.addItem(name, st)
        self.status_filter.currentIndexChanged.connect(lambda _: self.refresh())
        toolbar.addWidget(self.status_filter)
        toolbar.addStretch(1)
        self.new_button = QPushButton("سند جدید")
        self.new_button.setProperty("variant", "primary")
        menu = QMenu(self)
        for t in (DocType.RECEIPT, DocType.ISSUE, DocType.TRANSFER, DocType.LOAN_OUT,
                  DocType.LOAN_RETURN, DocType.ADJUSTMENT, DocType.OPENING):
            menu.addAction(DOC_TYPE_NAMES[t], lambda t=t: self.open_new(t))
        self.new_button.setMenu(menu)
        toolbar.addWidget(self.new_button)
        layout.addLayout(toolbar)

        actions = QHBoxLayout()
        self.count_label = QLabel(objectName="Muted")
        actions.addWidget(self.count_label)
        actions.addStretch(1)
        self.open_button = QPushButton("باز کردن")
        self.post_button = QPushButton("ثبت نهایی")
        self.cancel_button = QPushButton("ابطال")
        self.delete_button = QPushButton("حذف پیش‌نویس")
        self.print_button = QPushButton("چاپ / پیش‌نمایش")
        self.print_button.setMenu(document_print_menu(self, ctx, self.table_selected_id, self.refresh))
        self.open_button.clicked.connect(self.on_open)
        self.post_button.clicked.connect(self.on_post)
        self.cancel_button.clicked.connect(self.on_cancel)
        self.delete_button.clicked.connect(self.on_delete)
        for b in (self.print_button, self.open_button, self.post_button, self.cancel_button,
                  self.delete_button):
            actions.addWidget(b)
        layout.addLayout(actions)

        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(LIST_COLUMNS)
        self.table.set_empty_text("سندی با این شرایط پیدا نشد. برای شروع «سند جدید» را بزنید.")
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self.on_open())
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        ctx.user_changed.connect(self._update_buttons)
        self._update_buttons()

    def selected(self) -> DocumentRow | None:
        row_id = self.table.selected_id()
        return self._rows.get(row_id) if row_id is not None else None

    def table_selected_id(self) -> int | None:
        return self.table.selected_id()

    def _update_buttons(self, *_args) -> None:
        row, actor = self.selected(), self._ctx.actor
        self.new_button.setVisible(actor.can(Perm.DOCUMENTS_EDIT))
        self.delete_button.setVisible(actor.can(Perm.DOCUMENTS_EDIT))
        self.post_button.setVisible(actor.can(Perm.DOCUMENTS_POST))
        self.cancel_button.setVisible(actor.can(Perm.DOCUMENTS_POST))
        self.open_button.setEnabled(row is not None)
        self.open_button.setText("باز کردن" if row is None or row.status == DocStatus.DRAFT else "مشاهده")
        self.print_button.setEnabled(row is not None)
        draft = row is not None and row.status == DocStatus.DRAFT
        self.post_button.setEnabled(draft)
        self.delete_button.setEnabled(draft)
        self.cancel_button.setEnabled(row is not None and row.status == DocStatus.POSTED)

    @asyncSlot()
    async def refresh(self) -> None:
        self._seq += 1
        seq = self._seq
        try:
            rows = await docs.list_documents(
                self._ctx.db, self._ctx.actor, self.type_filter.currentData(),
                self.status_filter.currentData(), self.search.text())
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        if seq != self._seq:
            return
        self._rows = {r.id: r for r in rows}
        theme = self._ctx.themes.current
        status_color = {DocStatus.DRAFT: theme.warning, DocStatus.CANCELLED: theme.danger}
        self.table.set_rows(
            [(r.id, (r.number_text, r.type_name, jalali.format_date(r.doc_date),
                     r.warehouse + (f" ← {r.dest_warehouse}" if r.dest_warehouse else ""),
                     r.person or "—", to_persian_digits(r.line_count), _status_text(r),
                     r.created_by or "—")) for r in rows],
            muted=[r.status == DocStatus.CANCELLED for r in rows],
            highlight={(i, 6): status_color[r.status] for i, r in enumerate(rows)
                       if r.status in status_color},
        )
        self.count_label.setText(f"{to_persian_digits(len(rows))} سند")
        self._update_buttons()

    async def open_editor(self, doc_type: DocType, detail: DocumentDetail | None,
                          person_id: int | None = None, loan_id: int | None = None) -> bool:
        db = self._ctx.db
        warehouses = await master.list_warehouses(db)
        persons = await master.search_persons(db)
        units = await master.list_units(db, include_inactive=True)
        loans = await docs.outstanding_loans(db, self._ctx.actor) \
            if doc_type == DocType.LOAN_RETURN else []
        dialog = DocumentDialog(self._ctx, doc_type, detail, warehouses, persons, units, loans,
                                self)
        if person_id is not None:
            dialog.person.setCurrentIndex(max(dialog.person.findData(person_id), 0))
            dialog._fill_loans(loan_id)
            await dialog._on_loan_selected()
        await dialog.load_lines()
        accepted = bool(await exec_dialog(dialog))
        if accepted or dialog.saved_id:
            await self.refresh()
            self.table.select_id(dialog.saved_id)
        return accepted

    @asyncSlot()
    async def open_new(self, doc_type: DocType) -> None:
        await self.open_editor(doc_type, None)

    async def open_document(self, doc_id: int) -> None:
        await self.refresh()
        self.table.select_id(doc_id)
        detail = await docs.get_document(self._ctx.db, self._ctx.actor, doc_id)
        await self.open_editor(detail.input.doc_type, detail)

    @asyncSlot()
    async def on_open(self) -> None:
        row = self.selected()
        if row is None:
            return
        detail = await docs.get_document(self._ctx.db, self._ctx.actor, row.id)
        await self.open_editor(row.doc_type, detail)

    @asyncSlot()
    async def on_post(self) -> None:
        if (row := self.selected()) is None:
            return
        title = doc_title(row.doc_type, row.number)
        if not await confirm(self, f"«{title}» ثبت نهایی شود؟\nموجودی تغییر می‌کند و سند پس از آن فقط با "
                             "ابطال قابل برگشت است.", "ثبت نهایی"):
            return
        try:
            await with_stocktake_override(self._ctx, self, lambda approval: docs.post_document(
                self._ctx.db, self._ctx.actor, row.id, approval=approval))
        except ServiceError as exc:
            show_error(self, exc.message)
        else:
            show_info(self, f"«{title}» ثبت نهایی شد و موجودی به‌روز شد.")
        await self.refresh()

    @asyncSlot()
    async def on_cancel(self) -> None:
        if (row := self.selected()) is None:
            return
        if await exec_dialog(CancelDialog(self._ctx, row, self)):
            await self.refresh()

    @asyncSlot()
    async def on_delete(self) -> None:
        if (row := self.selected()) is None:
            return
        if not await confirm(self, f"پیش‌نویس «{doc_title(row.doc_type, row.number)}» حذف شود؟",
                             "حذف پیش‌نویس", danger=True):
            return
        try:
            await docs.delete_draft(self._ctx.db, self._ctx.actor, row.id)
        except ServiceError as exc:
            show_error(self, exc.message)
        await self.refresh()


class LoansList(QWidget):
    def __init__(self, ctx: AppContext, documents: DocumentsList, parent=None) -> None:
        super().__init__(parent)
        self._ctx, self._documents = ctx, documents
        self._rows: dict[tuple[int, int], LoanRow] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        toolbar = QHBoxLayout()
        self.count_label = QLabel(objectName="Muted")
        toolbar.addWidget(self.count_label)
        toolbar.addStretch(1)
        self.return_button = QPushButton("ثبت برگشت")
        self.return_button.setProperty("variant", "primary")
        self.return_button.clicked.connect(self.on_return)
        toolbar.addWidget(self.return_button)
        layout.addLayout(toolbar)
        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(LOAN_COLUMNS)
        self.table.set_empty_text("امانی بازی وجود ندارد؛ همه اقلام امانی برگشته‌اند.")
        self.table.itemSelectionChanged.connect(
            lambda: self.return_button.setEnabled(self.table.selected_id() is not None))
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        self.return_button.setEnabled(False)

    @asyncSlot()
    async def refresh(self) -> None:
        rows = await docs.outstanding_loans(self._ctx.db, self._ctx.actor)
        self._rows = {(r.document_id, r.item_id): r for r in rows}
        warn = self._ctx.themes.current.warning
        self.table.set_rows(
            [((r.document_id, r.item_id),
              (docs.number_text(DocType.LOAN_OUT, r.number), jalali.format_date(r.doc_date), r.person,
               r.item_name, f"{format_qty(r.outstanding)} {r.base_unit}",
               to_persian_digits(r.days_out))) for r in rows],
            highlight={(i, 5): warn for i, r in enumerate(rows) if r.days_out > 30},
        )
        self.count_label.setText(f"{to_persian_digits(len(rows))} قلم امانی باز")
        self.return_button.setVisible(self._ctx.actor.can(Perm.DOCUMENTS_EDIT))

    @asyncSlot()
    async def on_return(self) -> None:
        key = self.table.selected_id()
        if key is None:
            return
        row = self._rows[key]
        await self._documents.open_editor(DocType.LOAN_RETURN, None, row.person_id,
                                          row.document_id)
        await self.refresh()


class DocumentsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.documents = DocumentsList(ctx)
        self.loans = LoansList(ctx, self.documents)
        self.tabs.addTab(self.documents, "اسناد انبار")
        self.tabs.addTab(self.loans, "امانی‌های باز")
        self.tabs.currentChanged.connect(lambda _: self.tabs.currentWidget().refresh())
        layout.addWidget(self.tabs)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.tabs.currentWidget().refresh()
