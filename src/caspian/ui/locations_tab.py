"""«محل‌های مصرف» tab (اطلاعات پایه) — spec §2.7: list with search and type/floor filters,
add/edit/deactivate with aliases, CSV re-import with a preview, Excel export, settings."""

from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton
from qasync import asyncSlot

from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits as fa
from caspian.db.models import LocationType
from caspian.services import locations
from caspian.services.errors import ServiceError, ValidationError
from caspian.services.excel_export import write_xlsx
from caspian.services.locations import TYPE_NAMES, LocationInput, LocationRow, PreviewStatus
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.dialogs import FormDialog, ltr_field
from caspian.ui.file_dialogs import ask_open_path, ask_save_path
from caspian.ui.master_page import _Tab
from caspian.ui.messages import show_error, show_info
from caspian.ui.widgets import DataTable, SearchBox

ALIAS_SEPARATORS = ("؛", ";", "،", ",")


def _split_aliases(text: str) -> list[str]:
    for sep in ALIAS_SEPARATORS[1:]:
        text = text.replace(sep, ALIAS_SEPARATORS[0])
    return [a.strip() for a in text.split(ALIAS_SEPARATORS[0]) if a.strip()]


def _type_combo(selected: LocationType | None = None, with_all: bool = False) -> QComboBox:
    combo = QComboBox()
    if with_all:
        combo.addItem("همه انواع", None)
    for kind, name in TYPE_NAMES.items():
        combo.addItem(name, kind)
    if selected is not None:
        combo.setCurrentIndex(combo.findData(selected))
    return combo


def _floor_combo(floors, selected: int | None = None, first: str = "— طبقه —") -> QComboBox:
    combo = QComboBox()
    combo.addItem(first, None)
    for f in floors:
        combo.addItem(f.display, f.id)
    combo.setCurrentIndex(max(combo.findData(selected), 0))
    return combo


class LocationDialog(FormDialog):
    def __init__(self, ctx: AppContext, floors, row: LocationRow | None = None, parent=None) -> None:
        super().__init__("ویرایش محل مصرف" if row else "محل مصرف جدید",
                         "پله، آسانسور و «مشاعات» طبقه ثابت ندارند؛ طبقه هنگام ثبت حواله انتخاب می‌شود. "
                         "نام‌های دیگر (اسمی که کارکنان می‌گویند) را با «؛» جدا کنید.",
                         submit_text="ذخیره", parent=parent)
        self._ctx, self._row = ctx, row
        self.code = self.add_row("کد:", ltr_field(row.code if row else ""))
        self.name = self.add_row("نام:", QLineEdit(row.name_fa if row else ""))
        self.type = self.add_row("نوع:", _type_combo(row.type if row else LocationType.NAMED))
        self.floor = self.add_row("طبقه:", _floor_combo(floors, row.floor.id if row and row.floor else None))
        self.aliases = self.add_row("نام‌های دیگر:", QLineEdit("؛ ".join(row.aliases) if row else ""))
        self.merged = self.add_row("ادغام با (اختیاری):", ltr_field(row.merged_group or "" if row else ""))
        self.note = self.add_row("یادداشت:", QLineEdit(row.note if row else ""))
        self.type.currentIndexChanged.connect(self._update_floor)
        self._update_floor()

    def _update_floor(self, *_args) -> None:
        fixed = self.type.currentData() not in (LocationType.VERTICAL, LocationType.GENERAL)
        self.floor.setEnabled(fixed)
        if not fixed:
            self.floor.setCurrentIndex(0)

    async def submit(self) -> None:
        await locations.save_location(self._ctx.db, self._ctx.actor, LocationInput(
            self.code.text(), self.name.text(), self.type.currentData(), self.floor.currentData(),
            _split_aliases(self.aliases.text()), self.merged.text(), self.note.text()),
            self._row.id if self._row else None)


class LocationSettingsDialog(FormDialog):
    def __init__(self, ctx: AppContext, current: locations.LocationSettings, parent=None) -> None:
        super().__init__("تنظیمات محل مصرف", submit_text="ذخیره", parent=parent)
        self._ctx = ctx
        self.required = QCheckBox("محل مصرف برای حواله خروج الزامی است")
        self.required.setChecked(current.required_on_issue)
        self.add_row("", self.required)
        self.phase1 = self.add_row("پلاک‌های فاز ۱ (پذیرفته نمی‌شوند):", ltr_field(current.phase1_plates))
        self.phase1.setPlaceholderText("مثلاً 1-6,46-57,83-92")

    async def submit(self) -> None:
        await locations.save_settings(self._ctx.db, self._ctx.actor, self.phase1.text(),
                                      self.required.isChecked())


class CsvPreviewDialog(FormDialog):
    """Draft-first: shows what the file would change; nothing is written until «اعمال»."""

    COLUMNS = ("ردیف", "کد", "نام", "وضعیت", "تغییرات / خطا")

    def __init__(self, ctx: AppContext, text: str, preview: list[locations.PreviewRow], parent=None) -> None:
        counts = {s: sum(1 for p in preview if p.status == s) for s in locations.PREVIEW_NAMES}
        summary = "، ".join(f"{name}: {fa(counts[s])}" for s, name in locations.PREVIEW_NAMES.items())
        super().__init__("بررسی فایل محل‌های مصرف",
                         f"{summary}. ردیف‌هایی که در فایل نیستند تغییر نمی‌کنند (غیرفعال‌سازی دستی است).",
                         submit_text="اعمال تغییرات", parent=parent)
        self.setMinimumSize(820, 520)
        self._ctx, self._text = ctx, text
        self.result_counts: tuple[int, int] | None = None
        self.table = DataTable(self.COLUMNS)
        shown = [p for p in preview if p.status != PreviewStatus.UNCHANGED] or preview
        self.table.set_rows([(i, (fa(p.row.line), p.row.code, p.row.name, locations.PREVIEW_NAMES[p.status],
                                  p.message or "؛ ".join(p.changes)))
                             for i, p in enumerate(shown)],
                            muted=[p.status == PreviewStatus.UNCHANGED for p in shown])
        self.body.addWidget(self.table, 1)
        errors = counts[PreviewStatus.ERROR]
        if errors:
            self.show_status(f"{fa(errors)} ردیف خطا دارد؛ فایل را اصلاح و دوباره انتخاب کنید.")
        changes = counts[PreviewStatus.NEW] + counts[PreviewStatus.CHANGED]
        self.submit_button.setEnabled(not errors and changes > 0)

    def set_busy(self, busy: bool) -> None:
        enabled = self.submit_button.isEnabled()
        super().set_busy(busy)
        if not busy:
            self.submit_button.setEnabled(enabled)

    async def submit(self) -> None:
        self.result_counts = await locations.apply_csv(self._ctx.db, self._ctx.actor, self._text)


class LocationsTab(_Tab):
    perm = Perm.LOCATIONS_MANAGE
    columns = ("کد", "نام", "نوع", "طبقه", "نام‌های دیگر", "وضعیت")
    new_text = "محل جدید"
    empty_text = "محلی پیدا نشد."

    def build_filters(self) -> None:
        self.search = SearchBox("جستجو: پلاک، نام یا نام دیگر…")
        self.search.search.connect(lambda _t: self.reload())
        self.toolbar.addWidget(self.search, 1)
        self.type_filter = _type_combo(with_all=True)
        self.type_filter.currentIndexChanged.connect(lambda _i: self.reload())
        self.toolbar.addWidget(self.type_filter)
        self.floor_filter = QComboBox()
        self.floor_filter.addItem("همه طبقه‌ها", None)
        self.floor_filter.currentIndexChanged.connect(lambda _i: self.reload())
        self.toolbar.addWidget(self.floor_filter)

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._floors = []
        self.count_label = QLabel(objectName="Muted")
        extra = QHBoxLayout()
        extra.addWidget(self.count_label, 1)
        self.settings_button = QPushButton("تنظیمات…")
        self.settings_button.clicked.connect(self.on_settings)
        self.import_button = QPushButton("ورود دوباره از CSV…")
        self.import_button.clicked.connect(self.on_import)
        self.excel_button = QPushButton("خروجی اکسل")
        self.excel_button.clicked.connect(self.on_excel)
        for b in (self.settings_button, self.import_button, self.excel_button):
            extra.addWidget(b)
        self.layout().insertLayout(1, extra)

    def update_buttons(self, *_args) -> None:
        super().update_buttons()
        for b in (getattr(self, "settings_button", None), getattr(self, "import_button", None)):
            if b is not None:
                b.setVisible(self.can_edit)

    async def refresh(self) -> None:
        if not self._floors:
            self._floors = await locations.list_floors(self.ctx.db)
            self.floor_filter.blockSignals(True)
            for f in self._floors:
                self.floor_filter.addItem(f.display, f.id)
            self.floor_filter.blockSignals(False)
        rows = await locations.list_locations(self.ctx.db, self.search.text(), self.type_filter.currentData(),
                                              self.floor_filter.currentData())
        self.rows = {r.id: r for r in rows}
        self.table.set_rows(
            [(r.id, (locations.fa_digits(r.code), locations.fa_digits(r.name_fa), TYPE_NAMES[r.type],
                     r.floor.display if r.floor else "انتخاب هنگام ثبت", "؛ ".join(r.aliases),
                     "فعال" if r.is_active else "غیرفعال")) for r in rows],
            muted=[not r.is_active for r in rows])
        self.count_label.setText(f"{fa(len(rows))} محل")

    async def open_editor(self, row) -> None:
        await self.run_dialog(LocationDialog(self.ctx, self._floors, row, self))

    async def set_active(self, row, active: bool) -> None:
        await locations.set_location_active(self.ctx.db, self.ctx.actor, row.id, active)

    @asyncSlot()
    async def on_settings(self) -> None:
        current = await locations.read_settings(self.ctx.db)
        await exec_dialog(LocationSettingsDialog(self.ctx, current, self))

    @asyncSlot()
    async def on_import(self) -> None:
        path = await ask_open_path(self, "فایل محل‌های مصرف (CSV)", "CSV (*.csv)")
        if not path:
            return
        await self.import_file(path)

    async def import_file(self, path: str) -> None:
        try:
            raw = Path(path).read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValidationError("فایل باید با کدگذاری UTF-8 ذخیره شده باشد.") from None
            preview = await locations.preview_csv(self.ctx.db, self.ctx.actor, text)
        except (ServiceError, OSError) as exc:
            show_error(self, getattr(exc, "message", None) or str(exc))
            return
        dialog = CsvPreviewDialog(self.ctx, text, preview, self)
        if await exec_dialog(dialog) and dialog.result_counts:
            added, changed = dialog.result_counts
            show_info(self, f"{fa(added)} محل جدید اضافه و {fa(changed)} محل به‌روز شد.")
        await self.refresh()

    @asyncSlot()
    async def on_excel(self) -> None:
        path = await ask_save_path(self, "ذخیره اکسل", self.ctx.settings, "محل‌های مصرف.xlsx",
                                   "Excel (*.xlsx)")
        if path:
            write_xlsx(await locations.locations_table(self.ctx.db, self.ctx.actor), path)
            show_info(self, "فایل اکسل ذخیره شد.")
