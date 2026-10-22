"""«درخواست کالا»: requests sent to the owners through Caspian Server, their status and chat.

The manager registers requests one after another; each is saved here at once and sent in the
background (`ui.server_status.ServerLink`), so nothing waits for the network. Everything is picked
from the app's lists — requester (with a quick «new person»), place (LocationPicker) and items —
never typed freely.
"""

import html
import uuid
from dataclasses import dataclass
from decimal import Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.db.models import PersonKind, RequestStatus
from caspian.services import items, locations, master
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.server_link import goods
from caspian.services.server_link.goods import STATUS_NAMES, RequestDetail, RequestRow
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.dialogs import Cancelled, FormDialog, ltr_field
from caspian.ui.document_print import document_print_menu
from caspian.ui.location_picker import LocationPicker
from caspian.ui.messages import show_error
from caspian.ui.widgets import Card, DataTable, QtyEdit, SearchableCombo

COLUMNS = ("شماره", "درخواست‌کننده", "محل مصرف", "اقلام", "وضعیت", "تاریخ", "توضیح")
LINE_COLUMNS = ("کد", "نام کالا", "واحد", "مقدار", "")


# ----- requester: picked from persons, or added with just a name and a phone -----


class QuickPersonDialog(FormDialog):
    def __init__(self, ctx: AppContext, typed: str = "", parent=None) -> None:
        super().__init__("درخواست‌کننده جدید", "فقط نام و تلفن؛ بقیه را بعداً در «اطلاعات پایه» کامل کنید.",
                         submit_text="ذخیره", parent=parent)
        self._ctx = ctx
        self.saved_id: int | None = None
        self.name = self.add_row("نام:", QLineEdit(typed.strip()))
        self.phone = self.add_row("تلفن:", ltr_field())

    async def submit(self) -> None:
        self.saved_id = await master.save_person(self._ctx.db, self._ctx.actor, self.name.text(),
                                                 PersonKind.EMPLOYEE, phone=self.phone.text())


async def quick_person(ctx: AppContext, parent, typed: str = "") -> tuple[str, int] | None:
    if not ctx.actor.can(Perm.PERSONS_EDIT):
        show_error(parent, "شما مجوز تعریف شخص جدید را ندارید.")
        return None
    dialog = QuickPersonDialog(ctx, typed, parent)
    if not await exec_dialog(dialog) or dialog.saved_id is None:
        return None
    return " ".join(dialog.name.text().split()), dialog.saved_id


def _person_text(p) -> str:
    return f"{p.name} — {to_persian_digits(p.phone)}" if p.phone else p.name


# ----- new / corrected request -----


@dataclass
class _Line:
    item_id: int
    unit: QComboBox
    qty: QtyEdit


class RequestDialog(FormDialog):
    """New request, or a rejected one corrected and sent again (`detail`)."""

    def __init__(self, ctx: AppContext, persons, warehouses, units, detail: RequestDetail | None = None,
                 parent=None) -> None:
        super().__init__("ویرایش و ارسال دوباره درخواست" if detail else "درخواست کالای جدید",
                         "پس از «ثبت و ارسال» درخواست در صف قرار می‌گیرد و خودکار برای تأیید مالکان فرستاده "
                         "می‌شود؛ می‌توانید بلافاصله درخواست بعدی را ثبت کنید.",
                         submit_text="ارسال دوباره" if detail else "ثبت و ارسال", parent=parent)
        self.setMinimumSize(760, 600)
        self._ctx, self._detail = ctx, detail
        self._unit_names = {u.id: u.name for u in units}
        self._item_units: dict[int, list[tuple[int, str]]] = {}
        self.lines: list[_Line] = []
        # One id per dialog: a second click or a retry can never register the request twice.
        self.client_request_id = str(uuid.uuid4())
        self.saved_id: int | None = None
        self._saving = False
        data = detail.input if detail else None

        self.person = SearchableCombo("نام درخواست‌کننده را بنویسید…")
        self.person.enable_add("+ درخواست‌کننده جدید (نام و تلفن)",
                               lambda typed: quick_person(ctx, self, typed))
        self.person.set_items(((_person_text(p), p.id) for p in persons), none_text="—",
                              current=data.person_id if data else None)
        self.add_row("درخواست‌کننده:", self.person)
        self.place = LocationPicker()
        self.add_row("محل مصرف:", self.place)
        self.warehouse = QComboBox()
        for w in warehouses:
            self.warehouse.addItem(w.name, w.id)
        if data and data.warehouse_id is not None:
            self.warehouse.setCurrentIndex(max(self.warehouse.findData(data.warehouse_id), 0))
        if len(warehouses) > 1:
            self.add_row("از انبار:", self.warehouse)
        self.note = self.add_row("توضیحات:", QLineEdit(data.note if data else ""))
        self.note.setPlaceholderText("مثلاً فوری، برای نظافت پله…")

        self.item_input = QLineEdit()
        self.item_input.setPlaceholderText("کد، نام یا بارکد کالا را بنویسید و Enter بزنید")
        self.item_input.returnPressed.connect(self.on_item_entered)
        self.body.addWidget(self.item_input)
        self.table = QTableWidget(0, len(LINE_COLUMNS))
        self.table.setHorizontalHeaderLabels(LINE_COLUMNS)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 90), (2, 120), (3, 120), (4, 40)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, width)
        self.table.setMinimumHeight(220)
        self.body.addWidget(self.table, 1)
        self._pending = list(detail.row.lines) if detail else []
        self._place_value = (data.location_id, data.floor_id) if data else (None, None)
        self._inputs += [self.item_input, self.table]
        if detail and detail.row.rejection_reason:
            self.show_status(f"دلیل رد: {detail.row.rejection_reason}", is_error=False)

    async def load(self) -> None:
        await self.place.load(self._ctx.db)
        self.place.set_value(*self._place_value)
        for ln in self._pending:
            if ln.item_id is not None:
                await self._ensure_units(ln.item_id)
                self._add_line(ln.item_id, ln.code, ln.name, ln.unit_id, ln.qty)
        self._pending = []

    async def _ensure_units(self, item_id: int) -> list[tuple[int, str]]:
        if not self._item_units.get(item_id):
            detail = await items.get_item(self._ctx.db, self._ctx.actor, item_id)
            base = detail.input.base_unit_id
            self._item_units[item_id] = [(base, self._unit_names.get(base, "?"))] + [
                (u, self._unit_names.get(u, "?")) for u, _f in detail.input.units]
        return self._item_units[item_id]

    def _add_line(self, item_id: int, code: str, name: str, unit_id: int | None, qty: Decimal) -> None:
        unit = QComboBox()
        for uid, uname in self._item_units[item_id]:
            unit.addItem(uname, uid)
        unit.setCurrentIndex(max(unit.findData(unit_id), 0))
        line = _Line(item_id, unit, QtyEdit(qty))
        self.lines.append(line)
        r = self.table.rowCount()
        self.table.insertRow(r)
        for col, text in ((0, locations.fa_digits(code)), (1, name)):
            cell = QTableWidgetItem(text)
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, col, cell)
        self.table.setCellWidget(r, 2, unit)
        self.table.setCellWidget(r, 3, line.qty)
        remove = QToolButton(objectName="IconButton")
        remove.setText("✕")
        remove.setToolTip("حذف ردیف")
        remove.clicked.connect(lambda: self._remove_line(line))
        self.table.setCellWidget(r, 4, remove)

    def _remove_line(self, line: _Line) -> None:
        r = self.lines.index(line)
        self.lines.pop(r)
        self.table.removeRow(r)

    async def add_item(self, item_id: int, code: str, name: str, unit_id: int | None = None) -> None:
        units = await self._ensure_units(item_id)
        unit_id = unit_id or units[0][0]
        for line in self.lines:
            if line.item_id == item_id and line.unit.currentData() == unit_id:
                line.qty.set_value((line.qty.value() or Decimal(0)) + 1)
                return
        self._add_line(item_id, code, name, unit_id, Decimal(1))

    @asyncSlot()
    async def on_item_entered(self) -> None:
        from caspian.ui.documents_page import ItemChooserDialog

        text = self.item_input.text().strip()
        if not text:
            return
        self.show_status("")
        try:
            unit_id = None
            if hit := await items.lookup_barcode(self._ctx.db, text):
                item_id, unit_id = hit
                rows = [r for r in await items.search_items(self._ctx.db, self._ctx.actor, text)
                        if r.id == item_id]
            else:
                rows = await items.search_items(self._ctx.db, self._ctx.actor, text, limit=50)
                exact = [r for r in rows if r.code == text]
                if exact:
                    rows = exact
            if not rows:
                self.show_status(f"کالایی با «{text}» پیدا نشد.")
                return
            row = rows[0]
            if len(rows) > 1:
                chooser = ItemChooserDialog(self._ctx, text, rows, self)
                if not await exec_dialog(chooser):
                    return
                row = chooser.chosen
            await self.add_item(row.id, row.code, row.name, unit_id)
        except ServiceError as exc:
            self.show_status(exc.message)
        finally:
            self.item_input.clear()
            self.item_input.setFocus()

    def keyPressEvent(self, event) -> None:
        if self.focusWidget() is self.item_input and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()  # Enter adds an item; it never sends the request
            return
        super().keyPressEvent(event)

    def collect(self) -> goods.RequestInput:
        lines = []
        for no, line in enumerate(self.lines, start=1):
            if line.qty.value() is None:
                raise ValidationError(f"ردیف {no}: مقدار را وارد کنید.")
            lines.append(goods.RequestLineInput(line.item_id, line.unit.currentData(), line.qty.value()))
        value = self.place.value()
        if value is None and self.place.edit.text().strip():
            raise ValidationError("محل مصرف را از فهرست انتخاب کنید.")
        location_id, floor_id = value if value else (None, None)
        return goods.RequestInput(self.person.currentData(), location_id, floor_id, self.note.text(), lines,
                                  self.warehouse.currentData())

    def _widgets(self, field: str) -> list:
        if field.startswith("line:"):
            no = int(field.split(":", 1)[1])
            return [self.lines[no - 1].qty, self.lines[no - 1].unit] if 0 < no <= len(self.lines) else []
        return {"person": [self.person], "location": [self.place.edit], "floor": [self.place.floor],
                "warehouse": [self.warehouse], "lines": [self.item_input]}.get(field, [])

    def set_busy(self, busy: bool) -> None:
        super().set_busy(busy)
        if self.saved_id is not None:
            self.submit_button.setEnabled(False)  # saved: this dialog never sends again

    async def submit(self) -> None:
        if self._saving:  # a second click while the first is still saving: ignore it
            raise Cancelled
        if self.saved_id is not None:  # already saved: just close
            return
        self._saving = True
        try:
            await self._save()
        finally:
            self._saving = False
        self.submit_button.setEnabled(False)  # never a second time from this dialog

    async def _save(self) -> None:
        self.clear_invalid()
        data = self.collect()
        problems = await goods.check(self._ctx.db, self._ctx.actor, data)
        if problems:
            self.show_problems([p.message for p in problems])
            self.mark_invalid(w for p in problems for w in self._widgets(p.field))
            raise Cancelled
        if self._detail is None:
            self.saved_id = await goods.create_request(self._ctx.db, self._ctx.actor, data,
                                                       self.client_request_id)
        else:
            await goods.resubmit_request(self._ctx.db, self._ctx.actor, self._detail.row.id, data)
            self.saved_id = self._detail.row.id
        if self._ctx.server_link is not None:
            self._ctx.server_link.wake()


# ----- chat -----


class RequestChatDialog(QDialog):
    """One request: its details and the conversation with the owners (newest at the bottom)."""

    def __init__(self, ctx: AppContext, row: RequestRow, parent=None) -> None:
        super().__init__(parent)
        self._ctx, self._row = ctx, row
        self.setWindowTitle(f"‏گفتگوی درخواست {row.number_text}")
        self.setMinimumSize(620, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(QLabel(f"درخواست {row.number_text} — {row.requester}", objectName="PageTitle"))
        self.details = QLabel(objectName="Muted")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.transcript = QTextBrowser(objectName="Transcript")
        self.transcript.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout.addWidget(self.transcript, 1)
        row_layout = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("پیام به مالکان…")
        self.input.returnPressed.connect(self.on_send)
        row_layout.addWidget(self.input, 1)
        self.send_button = QPushButton("ارسال")
        self.send_button.setProperty("variant", "primary")
        self.send_button.clicked.connect(self.on_send)
        row_layout.addWidget(self.send_button)
        close = QPushButton("بستن")
        close.clicked.connect(self.accept)
        row_layout.addWidget(close)
        layout.addLayout(row_layout)
        self.status = QLabel(objectName="StatusText")
        self.status.hide()
        layout.addWidget(self.status)
        self._show_details(row)
        if ctx.server_link is not None:
            ctx.server_link.result.connect(self._on_sync)

    def _show_details(self, row: RequestRow) -> None:
        parts = [f"وضعیت: {row.status_name}", f"محل مصرف: {row.location}", f"اقلام: {row.items_text}"]
        if row.note:
            parts.append(f"توضیحات: {row.note}")
        if row.rejection_reason:
            parts.append(f"دلیل رد: {row.rejection_reason}")
        if row.issue_number:
            parts.append(f"حواله: {row.issue_number}")
        self.details.setText("\n".join(parts))
        can_chat = row.status != RequestStatus.DRAFT
        self.input.setEnabled(can_chat)
        self.send_button.setEnabled(can_chat)

    def _on_sync(self, result) -> None:
        if result.changed or result.events:
            self.reload()

    @asyncSlot()
    async def reload(self) -> None:
        db, actor = self._ctx.db, self._ctx.actor
        try:
            detail = await goods.get_request(db, actor, self._row.id)
            messages = await goods.list_messages(db, actor, self._row.id)
            await goods.mark_seen(db, actor, self._row.id)
        except ServiceError as exc:
            self.status.setText(exc.message)
            self.status.show()
            return
        self._row = detail.row
        self._show_details(detail.row)
        theme = self._ctx.themes.current
        self.transcript.clear()
        if not messages:
            self.transcript.append(f'<p style="color:{theme.text_muted}">هنوز پیامی رد و بدل نشده است.</p>')
        for m in messages:
            bg = theme.primary_soft if m.mine else theme.surface_alt
            when = jalali.format_date(m.created_at.date()) if m.created_at else ""
            note = " · در صف ارسال" if m.queued else ""
            who = f"{html.escape(m.sender)} · {when}{note}"
            self.transcript.append(
                f'<table width="100%" cellpadding="10" style="margin:6px 0"><tr><td '
                f'style="background:{bg}; color:{theme.text}; border-radius:10px">'
                f'<span style="color:{theme.text_muted}; font-size:8pt">{who}</span><br>'
                f'{html.escape(m.text).replace(chr(10), "<br>")}</td></tr></table>')
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())  # always opens at the latest message

    @asyncSlot()
    async def on_send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.send_button.setEnabled(False)
        try:
            await goods.queue_message(self._ctx.db, self._ctx.actor, self._row.id, text)
        except ServiceError as exc:
            self.status.setText(exc.message)
            self.status.show()
            return
        finally:
            self.send_button.setEnabled(True)
        self.input.clear()
        self.status.hide()
        if self._ctx.server_link is not None:
            self._ctx.server_link.wake()
        await self.reload()

    def done(self, result: int) -> None:
        if self._ctx.server_link is not None:
            self._ctx.server_link.result.disconnect(self._on_sync)
        super().done(result)


# ----- page -----


class RequestsPage(QWidget):
    refreshed = Signal()  # the list was reloaded (e.g. a request was opened: the badge may change)

    def __init__(self, ctx: AppContext, open_document=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._open_document = open_document  # async (doc_id) -> None, from the main window
        self._rows: dict[int, RequestRow] = {}
        self._seq = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem("همه وضعیت‌ها", None)
        for status, name in STATUS_NAMES.items():
            self.status_filter.addItem(name, status)
        self.status_filter.currentIndexChanged.connect(lambda _: self.refresh())
        toolbar.addWidget(self.status_filter)
        self.count_label = QLabel(objectName="Muted")
        toolbar.addWidget(self.count_label)
        toolbar.addStretch(1)
        self.new_button = QPushButton("درخواست جدید")
        self.new_button.setProperty("variant", "primary")
        self.new_button.clicked.connect(self.on_new)
        toolbar.addWidget(self.new_button)
        layout.addLayout(toolbar)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.chat_button = QPushButton("جزئیات و گفتگو")
        self.chat_button.clicked.connect(self.on_chat)
        self.edit_button = QPushButton("ویرایش و ارسال دوباره")
        self.edit_button.clicked.connect(self.on_edit)
        self.open_button = QPushButton("باز کردن حواله")
        self.open_button.clicked.connect(self.on_open_document)
        self.print_button = QPushButton("چاپ حواله")
        self.print_button.setToolTip("با چاپ، حواله ثبت نهایی و از موجودی کم می‌شود.")
        self.print_button.setMenu(document_print_menu(self, ctx, self._document_id, self.refresh))
        for b in (self.chat_button, self.edit_button, self.open_button, self.print_button):
            actions.addWidget(b)
        layout.addLayout(actions)

        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(COLUMNS)
        self.table.set_empty_text("درخواستی ثبت نشده است. برای شروع «درخواست جدید» را بزنید.")
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self.on_chat())
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        if ctx.server_link is not None:
            ctx.server_link.result.connect(self._on_sync)
        self._update_buttons()

    def selected(self) -> RequestRow | None:
        row_id = self.table.selected_id()
        return self._rows.get(row_id) if row_id is not None else None

    def _document_id(self) -> int | None:
        row = self.selected()
        return row.document_id if row else None

    def _update_buttons(self, *_args) -> None:
        row = self.selected()
        self.chat_button.setEnabled(row is not None)
        self.edit_button.setEnabled(row is not None and row.editable)
        self.open_button.setEnabled(row is not None and row.document_id is not None)
        printable = row is not None and row.document_id is not None and row.status in (
            RequestStatus.APPROVED, RequestStatus.ISSUED)
        self.print_button.setEnabled(printable and self._ctx.actor.can(Perm.DOCUMENTS_VIEW))

    def _on_sync(self, result) -> None:
        if (result.changed or result.events) and self.isVisible():
            self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    def _note(self, r: RequestRow) -> str:
        if r.last_error:
            return r.last_error
        if r.status == RequestStatus.REJECTED and r.rejection_reason:
            return f"دلیل رد: {r.rejection_reason}"
        if r.origin == "remote":
            return "ثبت‌شده خارج از این برنامه (فقط نمایش)"
        if r.status == RequestStatus.APPROVED and r.document_id is None:
            return "حواله خروج پیش‌نویس به‌زودی ساخته می‌شود"
        if r.status == RequestStatus.APPROVED:
            return "آماده چاپ حواله"
        if r.issue_number:
            return f"حواله {r.issue_number}"
        return r.note

    @asyncSlot()
    async def refresh(self) -> None:
        self._seq += 1
        seq = self._seq
        try:
            rows = await goods.list_requests(self._ctx.db, self._ctx.actor, self.status_filter.currentData())
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        if seq != self._seq:
            return
        self._rows = {r.id: r for r in rows}
        theme = self._ctx.themes.current
        colors = {RequestStatus.APPROVED: theme.success, RequestStatus.REJECTED: theme.danger,
                  RequestStatus.QUEUED: theme.warning, RequestStatus.DRAFT: theme.warning}
        self.table.set_rows(
            [(r.id, (r.number_text, r.requester, r.location, r.items_text,
                     ("● " if r.unseen else "") + r.status_name,
                     jalali.format_date(r.created_at.date()) if r.created_at else "", self._note(r)))
             for r in rows],
            muted=[r.origin == "remote" for r in rows],
            highlight={**{(i, 4): colors[r.status] for i, r in enumerate(rows) if r.status in colors},
                       **{(i, 6): theme.danger for i, r in enumerate(rows) if r.last_error}})
        self.count_label.setText(f"{to_persian_digits(len(rows))} درخواست")
        self._update_buttons()
        self.refreshed.emit()

    async def _lists(self):
        db = self._ctx.db
        return (await master.search_persons(db), await master.list_warehouses(db),
                await master.list_units(db, include_inactive=True))

    async def _open_dialog(self, detail: RequestDetail | None) -> None:
        persons, warehouses, units = await self._lists()
        dialog = RequestDialog(self._ctx, persons, warehouses, units, detail, self)
        await dialog.load()
        await exec_dialog(dialog)
        if dialog.saved_id is not None:
            await self.refresh()
            self.table.select_id(dialog.saved_id)

    @asyncSlot()
    async def on_new(self) -> None:
        await self._open_dialog(None)

    @asyncSlot()
    async def on_edit(self) -> None:
        row = self.selected()
        if row is None or not row.editable:
            return
        await self._open_dialog(await goods.get_request(self._ctx.db, self._ctx.actor, row.id))

    @asyncSlot()
    async def on_chat(self) -> None:
        row = self.selected()
        if row is None:
            return
        dialog = RequestChatDialog(self._ctx, row, self)
        await dialog.reload()
        await exec_dialog(dialog)
        await self.refresh()

    @asyncSlot()
    async def on_open_document(self) -> None:
        row = self.selected()
        if row is not None and row.document_id is not None and self._open_document is not None:
            await self._open_document(row.document_id)
