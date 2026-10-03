"""Reference data tabs: warehouses, persons, categories, units."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits as fa  # display-only digits (#19)
from caspian.db.models import PersonKind
from caspian.services import master
from caspian.services.errors import ServiceError
from caspian.services.master import PERSON_KIND_NAMES, PersonRow
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.dialogs import FormDialog, ltr_field
from caspian.ui.messages import show_error
from caspian.ui.widgets import Card, DataTable, SearchableCombo, SearchBox


class _Tab(QWidget):
    """Toolbar (optional filters + New) / action buttons / table."""

    perm: Perm = Perm.ITEMS_EDIT
    columns: tuple[str, ...] = ()
    new_text = "جدید"
    empty_text = "موردی تعریف نشده است."
    toggles_active = True  # False: the second button deletes instead

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        self.toolbar = QHBoxLayout()
        layout.addLayout(self.toolbar)
        self.build_filters()
        self.toolbar.addStretch(1)
        self.edit_button = QPushButton("ویرایش")
        self.edit_button.clicked.connect(self.on_edit)
        self.toolbar.addWidget(self.edit_button)
        self.active_button = QPushButton("غیرفعال‌سازی" if self.toggles_active else "حذف")
        self.active_button.clicked.connect(self.on_toggle_active)
        self.toolbar.addWidget(self.active_button)
        self.new_button = QPushButton(self.new_text)
        self.new_button.setProperty("variant", "primary")
        self.new_button.clicked.connect(self.on_new)
        self.toolbar.addWidget(self.new_button)

        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(self.columns)
        self.table.set_empty_text(self.empty_text)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.doubleClicked.connect(lambda _: self.on_edit())
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        self.rows: dict = {}
        ctx.user_changed.connect(self.update_buttons)

    def build_filters(self) -> None:
        pass

    @property
    def can_edit(self) -> bool:
        return self.ctx.actor.can(self.perm)

    def selected(self):
        row_id = self.table.selected_id()
        return self.rows.get(row_id) if row_id is not None else None

    def update_buttons(self, *_args) -> None:
        row = self.selected()
        self.new_button.setVisible(self.can_edit)
        self.edit_button.setVisible(self.can_edit)
        self.active_button.setVisible(self.can_edit)
        self.edit_button.setEnabled(row is not None)
        self.active_button.setEnabled(row is not None)
        if row is not None and self.toggles_active:
            self.active_button.setText("غیرفعال‌سازی" if row.is_active else "فعال‌سازی")

    async def run_dialog(self, dialog: FormDialog) -> None:
        if await exec_dialog(dialog):
            await self.refresh()

    async def refresh(self) -> None:
        raise NotImplementedError

    @asyncSlot()
    async def reload(self) -> None:
        try:
            await self.refresh()
        except ServiceError as exc:
            show_error(self, exc.message)
        self.update_buttons()

    @asyncSlot()
    async def on_new(self) -> None:
        await self.open_editor(None)

    @asyncSlot()
    async def on_edit(self) -> None:
        if (row := self.selected()) is not None and self.can_edit:
            await self.open_editor(row)

    @asyncSlot()
    async def on_toggle_active(self) -> None:
        row = self.selected()
        if row is None:
            return
        try:
            await self.set_active(row, not row.is_active)
        except ServiceError as exc:
            show_error(self, exc.message)
        await self.refresh()
        self.update_buttons()

    async def open_editor(self, row) -> None:
        raise NotImplementedError

    async def set_active(self, row, active: bool) -> None:
        raise NotImplementedError


# ----- warehouses -----


class WarehouseDialog(FormDialog):
    def __init__(self, ctx: AppContext, row=None, parent=None) -> None:
        super().__init__("ویرایش انبار" if row else "انبار جدید", submit_text="ذخیره",
                         parent=parent)
        self._ctx, self._row = ctx, row
        self.code = self.add_row("کد:", ltr_field(row.code if row else ""))
        self.name = self.add_row("نام انبار:", QLineEdit(row.name if row else ""))
        self.notes = self.add_row("توضیحات:", QLineEdit(row.notes if row else ""))

    async def submit(self) -> None:
        await master.save_warehouse(self._ctx.db, self._ctx.actor, self.code.text(),
                                    self.name.text(), self.notes.text(),
                                    self._row.id if self._row else None)


class WarehousesTab(_Tab):
    perm = Perm.WAREHOUSES_EDIT
    columns = ("کد", "نام انبار", "توضیحات", "وضعیت")
    new_text = "انبار جدید"

    async def refresh(self) -> None:
        rows = await master.list_warehouses(self.ctx.db, include_inactive=True)
        self.rows = {r.id: r for r in rows}
        self.table.set_rows(
            [(r.id, (fa(r.code), r.name, r.notes, "فعال" if r.is_active else "غیرفعال"))
             for r in rows], muted=[not r.is_active for r in rows])

    async def open_editor(self, row) -> None:
        await self.run_dialog(WarehouseDialog(self.ctx, row, self))

    async def set_active(self, row, active: bool) -> None:
        await master.set_warehouse_active(self.ctx.db, self.ctx.actor, row.id, active)


# ----- persons -----


def _kind_combo(selected: PersonKind | None = None, with_all: bool = False) -> QComboBox:
    combo = QComboBox()
    if with_all:
        combo.addItem("همه اشخاص", None)
    for kind, name in PERSON_KIND_NAMES.items():
        combo.addItem(name, kind)
    if selected is not None:
        combo.setCurrentIndex(combo.findData(selected))
    return combo


class PersonDialog(FormDialog):
    def __init__(self, ctx: AppContext, row: PersonRow | None = None, parent=None) -> None:
        super().__init__("ویرایش شخص" if row else "شخص جدید",
                         "کد خالی = تخصیص خودکار." if not row else "",
                         submit_text="ذخیره", parent=parent)
        self._ctx, self._row = ctx, row
        self.saved_id: int | None = None
        self.name = self.add_row("نام:", QLineEdit(row.name if row else ""))
        self.kind = self.add_row("نوع:", _kind_combo(row.kind if row else PersonKind.SUPPLIER))
        self.code = self.add_row("کد:", ltr_field(row.code if row else ""))
        self.phone = self.add_row("تلفن:", ltr_field(row.phone if row else ""))
        self.address = self.add_row("نشانی:", QLineEdit(row.address if row else ""))

    async def submit(self) -> None:
        self.saved_id = await master.save_person(
            self._ctx.db, self._ctx.actor, self.name.text(), self.kind.currentData(),
            self.phone.text(), self.address.text(), self.code.text(),
            self._row.id if self._row else None, self._row.version_id if self._row else None,
        )


async def add_person(ctx: AppContext, parent, typed: str = "") -> tuple[str, int] | None:
    """«+ افزودن شخص جدید» in person pickers: (display text, id) of the new person, or None."""
    if not ctx.actor.can(Perm.PERSONS_EDIT):
        show_error(parent, "شما مجوز تعریف شخص جدید را ندارید.")
        return None
    dialog = PersonDialog(ctx, parent=parent)
    dialog.name.setText(typed.strip())
    if not await exec_dialog(dialog) or dialog.saved_id is None:
        return None
    return (f"{' '.join(dialog.name.text().split())} ({PERSON_KIND_NAMES[dialog.kind.currentData()]})",
            dialog.saved_id)


def person_picker(ctx: AppContext, parent, persons, current: int | None = None) -> SearchableCombo:
    combo = SearchableCombo("نام شخص را بنویسید…")
    combo.enable_add("+ افزودن شخص جدید", lambda typed: add_person(ctx, parent, typed))
    combo.set_items(((f"{p.name} ({p.kind_name})", p.id) for p in persons), none_text="—",
                    current=current)
    return combo


class PersonsTab(_Tab):
    perm = Perm.PERSONS_EDIT
    columns = ("کد", "نام", "نوع", "تلفن", "نشانی", "وضعیت")
    new_text = "شخص جدید"

    def build_filters(self) -> None:
        self._seq = 0
        self.search = SearchBox("جستجو: نام، کد یا تلفن…")
        self.search.search.connect(lambda _: self.reload())
        self.toolbar.addWidget(self.search)
        self.kind = _kind_combo(with_all=True)
        self.kind.currentIndexChanged.connect(lambda _: self.reload())
        self.toolbar.addWidget(self.kind)
        self.show_inactive = QCheckBox("نمایش غیرفعال‌ها")
        self.show_inactive.toggled.connect(lambda _: self.reload())
        self.toolbar.addWidget(self.show_inactive)

    async def refresh(self) -> None:
        self._seq += 1
        seq = self._seq
        rows = await master.search_persons(self.ctx.db, self.search.text(),
                                           self.kind.currentData(),
                                           self.show_inactive.isChecked())
        if seq != self._seq:
            return
        self.rows = {r.id: r for r in rows}
        self.table.set_rows(
            [(r.id, (fa(r.code), r.name, r.kind_name, fa(r.phone), r.address,
                     "فعال" if r.is_active else "غیرفعال")) for r in rows],
            muted=[not r.is_active for r in rows])

    async def open_editor(self, row) -> None:
        await self.run_dialog(PersonDialog(self.ctx, row, self))

    async def set_active(self, row, active: bool) -> None:
        await master.set_person_active(self.ctx.db, self.ctx.actor, row.id, active)


# ----- categories -----


class CategoryDialog(FormDialog):
    def __init__(self, ctx: AppContext, categories, row=None, parent=None) -> None:
        super().__init__("ویرایش گروه" if row else "گروه جدید", submit_text="ذخیره",
                         parent=parent)
        self._ctx, self._row = ctx, row
        self.name = self.add_row("نام گروه:", QLineEdit(row.name if row else ""))
        self.parent_combo = SearchableCombo()
        self.parent_combo.addItem("— (گروه اصلی)", None)
        for cat in categories:
            if row is None or cat.id != row.id:
                self.parent_combo.addItem(cat.name, cat.id)
        if row is not None:
            self.parent_combo.setCurrentIndex(max(self.parent_combo.findData(row.parent_id), 0))
        self.add_row("زیرمجموعه:", self.parent_combo)

    async def submit(self) -> None:
        await master.save_category(self._ctx.db, self._ctx.actor, self.name.text(),
                                   self._row.id if self._row else None,
                                   self.parent_combo.currentData())


class CategoriesTab(_Tab):
    columns = ("نام گروه", "زیرمجموعه", "تعداد کالا")
    new_text = "گروه جدید"
    empty_text = "هنوز گروهی تعریف نشده. با «گروه جدید» کالاها را دسته‌بندی کنید."
    toggles_active = False

    async def refresh(self) -> None:
        rows = await master.list_categories(self.ctx.db)
        names = {r.id: r.name for r in rows}
        self.rows = {r.id: r for r in rows}
        self.table.set_rows([(r.id, (r.name, names.get(r.parent_id, "—"),
                                     str(r.item_count))) for r in rows])

    async def open_editor(self, row) -> None:
        await self.run_dialog(CategoryDialog(self.ctx, list(self.rows.values()), row, self))

    @asyncSlot()
    async def on_toggle_active(self) -> None:
        row = self.selected()
        if row is None:
            return
        try:
            await master.delete_category(self.ctx.db, self.ctx.actor, row.id)
        except ServiceError as exc:
            show_error(self, exc.message)
        await self.refresh()


# ----- units -----


class UnitDialog(FormDialog):
    def __init__(self, ctx: AppContext, row=None, parent=None) -> None:
        super().__init__("ویرایش واحد" if row else "واحد جدید", submit_text="ذخیره",
                         parent=parent)
        self._ctx, self._row = ctx, row
        self.name = self.add_row("نام واحد:", QLineEdit(row.name if row else ""))
        self.allow_decimal = QCheckBox("مقدار اعشاری مجاز است (مثل متر، کیلوگرم؛ برای «عدد» و «کارتن» خاموش)")
        self.allow_decimal.setChecked(row.allow_decimal if row else True)
        self.add_row("", self.allow_decimal)

    async def submit(self) -> None:
        await master.save_unit(self._ctx.db, self._ctx.actor, self.name.text(),
                               self._row.id if self._row else None, self.allow_decimal.isChecked())


class UnitsTab(_Tab):
    columns = ("نام واحد", "اعشار", "وضعیت")
    new_text = "واحد جدید"

    async def refresh(self) -> None:
        rows = await master.list_units(self.ctx.db, include_inactive=True)
        self.rows = {r.id: r for r in rows}
        self.table.set_rows([(r.id, (r.name, "مجاز" if r.allow_decimal else "فقط عدد صحیح",
                                     "فعال" if r.is_active else "غیرفعال"))
                             for r in rows], muted=[not r.is_active for r in rows])

    async def open_editor(self, row) -> None:
        await self.run_dialog(UnitDialog(self.ctx, row, self))

    async def set_active(self, row, active: bool) -> None:
        await master.set_unit_active(self.ctx.db, self.ctx.actor, row.id, active)


class MasterDataPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.warehouses = WarehousesTab(ctx)
        self.persons = PersonsTab(ctx)
        self.categories = CategoriesTab(ctx)
        self.units = UnitsTab(ctx)
        self.tabs.addTab(self.persons, "اشخاص")
        self.tabs.addTab(self.warehouses, "انبارها")
        self.tabs.addTab(self.categories, "گروه‌های کالا")
        self.tabs.addTab(self.units, "واحدها")
        self.tabs.currentChanged.connect(lambda _: self.tabs.currentWidget().reload())
        layout.addWidget(self.tabs)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.tabs.currentWidget().reload()
