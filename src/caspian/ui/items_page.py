"""Items list and the item editor."""

from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core.numbers import format_qty
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.core.text import to_persian_digits as fa  # display-only digits (#19)
from caspian.services import items, master
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.items import ItemDetail, ItemInput, ItemRow
from caspian.services.master import CategoryRow, UnitRow
from caspian.services.protected import ProtectedAction
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.auth_dialogs import request_approval
from caspian.ui.dialogs import Cancelled, FormDialog, ltr_field
from caspian.ui.messages import confirm, show_error, show_info
from caspian.ui.widgets import Card, DataTable, EmptyState, QtyEdit, SearchableCombo, SearchBox, Toast

COLUMNS = ("کد", "نام کالا", "گروه", "واحد", "موجودی", "نقطه سفارش", "وضعیت")


class _RowList(QWidget):
    """Editable list of small rows (alternate units, barcodes) with add/remove."""

    def __init__(self, add_text: str, make_row, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._make_row = make_row
        self.rows: list[tuple[QWidget, ...]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._grid = QGridLayout()
        self._grid.setSpacing(6)
        layout.addLayout(self._grid)
        self.add_button = QPushButton(add_text)
        self.add_button.clicked.connect(lambda: self.add_row())
        add_row = QHBoxLayout()
        add_row.addWidget(self.add_button)
        add_row.addStretch(1)
        layout.addLayout(add_row)

    def add_row(self, *values) -> tuple[QWidget, ...]:
        widgets = self._make_row(*values)
        remove = QToolButton(objectName="IconButton")
        remove.setText("✕")
        remove.setToolTip("حذف")
        r = self._grid.rowCount()
        for c, w in enumerate((*widgets, remove)):
            self._grid.addWidget(w, r, c)
        entry = (*widgets, remove)
        remove.clicked.connect(lambda: self._remove(entry))
        self.rows.append(entry)
        widgets[0].setFocus()
        return entry

    def _remove(self, entry: tuple[QWidget, ...]) -> None:
        self.rows.remove(entry)
        for w in entry:
            self._grid.removeWidget(w)
            w.deleteLater()


def _unit_combo(units: list[UnitRow], selected: int | None = None,
                base_label: str | None = None) -> QComboBox:
    combo = QComboBox()
    if base_label:
        combo.addItem(base_label, None)
    for unit in units:
        combo.addItem(unit.name, unit.id)
    index = combo.findData(selected)
    combo.setCurrentIndex(max(index, 0))
    return combo


class ItemDialog(FormDialog):
    def __init__(self, ctx: AppContext, units: list[UnitRow], categories: list[CategoryRow],
                 detail: ItemDetail | None = None, suggested_code: str = "",
                 parent: QWidget | None = None, warehouses=()) -> None:
        editing = detail is not None
        super().__init__("ویرایش کالا" if editing else "کالای جدید", submit_text="ذخیره",
                         parent=parent)
        self.setMinimumWidth(620)
        self._ctx, self._detail, self._units = ctx, detail, units
        self.saved_id: int | None = None
        self.result_message = ""
        data = detail.input if editing else ItemInput(code=suggested_code, name="",
                                                      base_unit_id=units[0].id)

        self.code = self.add_row("کد کالا:", ltr_field(data.code))
        self.name = self.add_row("نام کالا:", QLineEdit(data.name))
        self.category = SearchableCombo()
        self.category.addItem("بدون گروه", None)
        for cat in categories:
            self.category.addItem(cat.name, cat.id)
        self.category.setCurrentIndex(max(self.category.findData(data.category_id), 0))
        self.add_row("گروه:", self.category)
        self.base_unit = self.add_row("واحد اصلی:", _unit_combo(units, data.base_unit_id))
        if editing and detail.has_movements:
            self.base_unit.setToolTip("واحد اصلی کالایی که گردش دارد قابل تغییر نیست.")
        self._base_locked = editing and detail.has_movements
        if self._base_locked:  # documents and printed forms refer to the code (#23)
            self.code.setReadOnly(True)
            self.code.setToolTip("کد کالایی که در سندی استفاده شده قابل تغییر نیست.")

        reorder = QHBoxLayout()
        self.reorder_point = QtyEdit(data.reorder_point)
        self.reorder_point.setPlaceholderText("خالی = بدون هشدار")
        self.reorder_qty = QtyEdit(data.reorder_qty)
        self.reorder_qty.setPlaceholderText("مقدار پیشنهادی خرید")
        reorder.addWidget(self.reorder_point)
        reorder.addWidget(QLabel("مقدار سفارش:"))
        reorder.addWidget(self.reorder_qty)
        self.form.addRow("نقطه سفارش:", reorder)
        self._inputs += [self.reorder_point, self.reorder_qty]

        self.returnable = QCheckBox("کالای امانی / برگشتی (مثل پالت، کپسول آتش‌نشانی)")
        self.returnable.setChecked(data.is_returnable)
        self.add_row("", self.returnable)
        self.description = QPlainTextEdit(data.description)
        self.description.setFixedHeight(60)
        self.add_row("توضیحات:", self.description)

        self.body.addWidget(QLabel("واحدهای فرعی (مثلاً ۱ جعبه = ۲۴ عدد)", objectName="CardTitle"))
        self.alt_units = _RowList("+ افزودن واحد", self._make_unit_row)
        self.body.addWidget(self.alt_units)
        for unit_id, factor in data.units:
            self.alt_units.add_row(unit_id, factor)

        self.body.addWidget(
            QLabel("بارکدها — با اسکنر بخوانید و Enter بزنید", objectName="CardTitle")
        )
        self.barcodes = _RowList("+ افزودن بارکد", self._make_barcode_row)
        self.body.addWidget(self.barcodes)
        for barcode, unit_id in data.barcodes:
            self.barcodes.add_row(barcode, unit_id)

        # Opening stock is recorded as an OPENING document, never written directly (#12).
        self.opening_qty = QtyEdit()
        self.opening_wh = SearchableCombo()
        self.opening_price = QtyEdit()
        if not editing and warehouses and ctx.actor.can(Perm.DOCUMENTS_EDIT):
            self.body.addWidget(QLabel("موجودی اولیه (اختیاری) — با سند «موجودی اول دوره» ثبت می‌شود",
                                       objectName="CardTitle"))
            opening = QHBoxLayout()
            self.opening_qty.setPlaceholderText("مقدار به واحد اصلی")
            self.opening_wh.set_items((w.name, w.id) for w in warehouses)
            self.opening_price.setPlaceholderText("فی (اختیاری)")
            for label, widget in (("مقدار:", self.opening_qty), ("انبار:", self.opening_wh),
                                  ("فی:", self.opening_price)):
                opening.addWidget(QLabel(label))
                opening.addWidget(widget, 1)
            self.body.addLayout(opening)
            self._inputs += [self.opening_qty, self.opening_wh, self.opening_price]
        self.name.setFocus()
        self.set_busy(False)

    def set_busy(self, busy: bool) -> None:
        super().set_busy(busy)
        self.base_unit.setEnabled(not busy and not self._base_locked)

    def _make_unit_row(self, unit_id: int | None = None, factor: Decimal | None = None):
        combo = _unit_combo(self._units, unit_id)
        equals = QLabel("=")
        qty = QtyEdit(factor)
        qty.setPlaceholderText("تعداد واحد اصلی")
        return combo, equals, qty

    def _make_barcode_row(self, barcode: str = "", unit_id: int | None = None):
        edit = ltr_field(barcode)
        edit.setPlaceholderText("بارکد")
        edit.returnPressed.connect(self._on_barcode_enter)
        combo = _unit_combo(self._units, unit_id, base_label="واحد اصلی")
        return edit, combo

    def _on_barcode_enter(self) -> None:
        # A scanner types the code then Enter: start a fresh row instead of saving.
        last = self.barcodes.rows[-1][0] if self.barcodes.rows else None
        if last is None or last.text().strip():
            self.barcodes.add_row()
        else:
            last.setFocus()

    def keyPressEvent(self, event) -> None:
        in_barcode = any(self.focusWidget() is row[0] for row in self.barcodes.rows)
        if in_barcode and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def collect(self) -> ItemInput:
        units = []
        for combo, _eq, qty, _rm in self.alt_units.rows:
            if qty.value() is None:
                raise ValidationError(f"ضریب واحد «{combo.currentText()}» را وارد کنید.")
            units.append((combo.currentData(), qty.value()))
        barcodes = [(edit.text(), combo.currentData())
                    for edit, combo, _rm in self.barcodes.rows if edit.text().strip()]
        for field, label in ((self.reorder_point, "نقطه سفارش"), (self.reorder_qty, "مقدار سفارش")):
            if field.text().strip() and field.value() is None:
                raise ValidationError(f"{label} عدد معتبر نیست.")
        return ItemInput(
            code=self.code.text(), name=self.name.text(),
            base_unit_id=self.base_unit.currentData(), category_id=self.category.currentData(),
            reorder_point=self.reorder_point.value(), reorder_qty=self.reorder_qty.value(),
            is_returnable=self.returnable.isChecked(),
            description=self.description.toPlainText(), units=units, barcodes=barcodes,
        )

    def collect_opening(self) -> items.OpeningStock | None:
        if not self.opening_qty.text().strip():
            return None
        if self.opening_qty.value() is None:
            raise ValidationError("مقدار موجودی اولیه عدد معتبر نیست.")
        if self.opening_price.text().strip() and self.opening_price.value() is None:
            raise ValidationError("فی موجودی اولیه عدد معتبر نیست.")
        return items.OpeningStock(self.opening_wh.currentData(), self.opening_qty.value(),
                                  self.opening_price.value())

    async def submit(self) -> None:
        data = self.collect()
        same = await items.same_name_items(self._ctx.db, data.name,
                                           self._detail.id if self._detail else None)
        if same and not await confirm(
                self, f"کالای فعال دیگری با همین نام وجود دارد (کد {'، '.join(c for c, _n in same)}). "
                      "باز هم ذخیره شود؟", "ذخیره"):
            raise Cancelled
        if self._detail is None:
            created = await items.create_item_with_opening(self._ctx.db, self._ctx.actor, data,
                                                           self.collect_opening())
            self.saved_id = created.item_id
            if created.document_id and not created.posted:
                self.result_message = ("کالا ساخته شد. سند «موجودی اول دوره» به‌صورت پیش‌نویس ثبت شد؛ "
                                       "کاربر دارای مجوز ثبت نهایی باید آن را در «اسناد انبار» ثبت کند.")
        else:
            await items.update_item(self._ctx.db, self._ctx.actor, self._detail.id,
                                    self._detail.version_id, data)
            self.saved_id = self._detail.id


class ItemsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._rows: dict[int, ItemRow] = {}
        self._seq = 0
        self._categories: list[CategoryRow] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        self.search = SearchBox("جستجو: نام، کد یا بارکد…")
        self.search.search.connect(lambda _: self.refresh())
        toolbar.addWidget(self.search)
        self.category = SearchableCombo("گروه…")
        self.category.setMinimumWidth(180)
        self.category.currentIndexChanged.connect(lambda _: self.refresh())
        toolbar.addWidget(self.category)
        self.low_only = QCheckBox("فقط زیر نقطه سفارش")
        self.low_only.toggled.connect(lambda _: self.refresh())
        self.low_only.setVisible(ctx.actor.can(Perm.STOCK_VIEW))
        toolbar.addWidget(self.low_only)
        self.show_inactive = QCheckBox("نمایش غیرفعال‌ها")
        self.show_inactive.toggled.connect(lambda _: self.refresh())
        toolbar.addWidget(self.show_inactive)
        toolbar.addStretch(1)
        self.new_button = QPushButton("کالای جدید")
        self.new_button.setProperty("variant", "primary")
        self.new_button.clicked.connect(self.on_new)
        toolbar.addWidget(self.new_button)
        layout.addLayout(toolbar)

        actions = QHBoxLayout()
        self.count_label = QLabel(objectName="Muted")
        actions.addWidget(self.count_label)
        actions.addStretch(1)
        self.edit_button = QPushButton("ویرایش")
        self.active_button = QPushButton("غیرفعال‌سازی")
        self.delete_button = QPushButton("حذف")
        self.edit_button.clicked.connect(self.on_edit)
        self.active_button.clicked.connect(self.on_toggle_active)
        self.delete_button.clicked.connect(self.on_delete)
        for b in (self.edit_button, self.active_button, self.delete_button):
            actions.addWidget(b)
        layout.addLayout(actions)

        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(COLUMNS)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self.on_edit())
        card.body.addWidget(self.table)
        self.empty = EmptyState(
            "کالایی پیدا نشد",
            "با «کالای جدید» اولین کالا را تعریف کنید یا عبارت جستجو را تغییر دهید.",
        )
        card.body.addWidget(self.empty)
        self.empty.hide()
        layout.addWidget(card, 1)

        ctx.user_changed.connect(self._apply_permissions)
        self._apply_permissions()

    def _apply_permissions(self, *_args) -> None:
        can_edit = self._ctx.actor.can(Perm.ITEMS_EDIT)
        for b in (self.new_button, self.edit_button, self.active_button, self.delete_button):
            b.setVisible(can_edit)
        self._update_buttons()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        # Set from the dashboard's «زیر نقطه سفارش» card; it must not stick after leaving (#30).
        self.low_only.setChecked(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.reload_all()

    @asyncSlot()
    async def reload_all(self) -> None:
        self._categories = await master.list_categories(self._ctx.db)
        current = self.category.currentData()
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("همه گروه‌ها", None)
        for cat in self._categories:
            self.category.addItem(cat.name, cat.id)
        self.category.setCurrentIndex(max(self.category.findData(current), 0))
        self.category.blockSignals(False)
        await self.refresh()

    @asyncSlot()
    async def refresh(self) -> None:
        self._seq += 1
        seq = self._seq
        try:
            rows = await items.search_items(
                self._ctx.db, self._ctx.actor, self.search.text(),
                include_inactive=self.show_inactive.isChecked(),
                category_id=self.category.currentData(),
                only_below_reorder=self.low_only.isChecked(),
            )
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        if seq != self._seq:  # a newer search started while this one ran; drop stale results
            return
        self._rows = {r.id: r for r in rows}
        theme = self._ctx.themes.current
        highlight = {(i, 4): theme.warning for i, r in enumerate(rows) if r.below_reorder}
        self.table.set_rows(
            [(r.id, (fa(r.code), r.name, r.category or "—", r.base_unit, format_qty(r.on_hand) or "—",
                     format_qty(r.reorder_point) or "—", "فعال" if r.is_active else "غیرفعال"))
             for r in rows],
            muted=[not r.is_active for r in rows],
            highlight=highlight,
        )
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        self.count_label.setText(f"{to_persian_digits(len(rows))} کالا")
        self._update_buttons()

    def selected(self) -> ItemRow | None:
        item_id = self.table.selected_id()
        return self._rows.get(item_id) if item_id is not None else None

    def _update_buttons(self) -> None:
        row = self.selected()
        for b in (self.edit_button, self.active_button, self.delete_button):
            b.setEnabled(row is not None)
        if row is not None:
            self.active_button.setText("غیرفعال‌سازی" if row.is_active else "فعال‌سازی")

    async def _open_editor(self, detail: ItemDetail | None) -> None:
        units = await master.list_units(self._ctx.db)
        code = "" if detail else await items.next_code(self._ctx.db)
        warehouses = [] if detail else await master.list_warehouses(self._ctx.db)
        dialog = ItemDialog(self._ctx, units, self._categories, detail, code, self, warehouses)
        if await exec_dialog(dialog):
            await self.refresh()
            self.table.select_id(dialog.saved_id)
            if dialog.result_message:
                show_info(self, dialog.result_message)
            else:
                self.toast = Toast(self, f"کالای «{dialog.name.text().strip()}» ذخیره شد.")

    @asyncSlot()
    async def on_new(self) -> None:
        await self._open_editor(None)

    @asyncSlot()
    async def on_edit(self) -> None:
        row = self.selected()
        if row is None or not self._ctx.actor.can(Perm.ITEMS_EDIT):
            return
        await self._open_editor(await items.get_item(self._ctx.db, self._ctx.actor, row.id))

    @asyncSlot()
    async def on_toggle_active(self) -> None:
        row = self.selected()
        if row is None:
            return
        try:
            if row.is_active:
                approval = await request_approval(
                    self._ctx.db, self._ctx.actor, ProtectedAction.DEACTIVATE_ITEM,
                    f"غیرفعال‌سازی کالای «{row.name}» (کد {row.code}). کالای غیرفعال در "
                    "جستجوها و اسناد جدید نمایش داده نمی‌شود.", self,
                )
                if approval is None:
                    return
                await items.set_item_active(self._ctx.db, self._ctx.actor, row.id, False,
                                            approval)
            else:
                await items.set_item_active(self._ctx.db, self._ctx.actor, row.id, True)
        except ServiceError as exc:
            show_error(self, exc.message)
        await self.refresh()

    @asyncSlot()
    async def on_delete(self) -> None:
        row = self.selected()
        if row is None:
            return
        if await items.has_movements(self._ctx.db, row.id):
            show_error(self, "این کالا در اسناد انبار استفاده شده و قابل حذف نیست. "
                             "به‌جای حذف، آن را غیرفعال کنید.")
            return
        try:
            approval = await request_approval(
                self._ctx.db, self._ctx.actor, ProtectedAction.DELETE_ITEM,
                f"حذف کامل کالای «{row.name}» (کد {row.code}) به همراه واحدها و بارکدهایش.",
                self,
            )
            if approval is None:
                return
            await items.delete_item(self._ctx.db, self._ctx.actor, row.id, approval)
        except ServiceError as exc:
            show_error(self, exc.message)
        await self.refresh()

