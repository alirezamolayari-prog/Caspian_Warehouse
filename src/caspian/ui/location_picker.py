"""`LocationPicker`: one keyboard-driven entry for «محل مصرف» (spec §2.6).

Type a plate number and press Enter → selected directly («پلاک ۶۸ · همکف»). Type a name or
alias → arrow through the suggestions → Enter. Stairs, elevators and «مشاعات عمومی طبقه» show a
floor box next to the field, and the document can't be saved until a floor is chosen. Phase 1
plates are refused with «این پلاک مربوط به فاز ۱ است».
"""

from collections.abc import Awaitable, Callable

from PySide6.QtCore import QEvent, QObject, QStringListModel, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from caspian.db.database import Database
from caspian.services import locations
from caspian.services.errors import ValidationError
from caspian.services.locations import FloorRow, LocationIndex, LocationRow, Phase1Plate
from caspian.ui.tasks import spawn

NOTHING_CHOSEN = "محل مصرف را انتخاب کنید (شماره پلاک، نام یا محل)."


class LocationPicker(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._index: LocationIndex | None = None
        self._db: Database | None = None
        self._results: list[LocationRow] = []
        self.location: LocationRow | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("پلاک، نام یا محل… (مثلاً ۶۸ یا باری اسدنژاد)")
        self.edit.setClearButtonEnabled(True)
        self._model = QStringListModel(self)
        self.completer = QCompleter(self._model, self)
        self.completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.completer.setMaxVisibleItems(12)
        self.completer.setWidget(self.edit)
        self.completer.activated[str].connect(self._on_completion)
        self.edit.textEdited.connect(self._on_text)
        self.edit.returnPressed.connect(self._on_enter)
        self.edit.editingFinished.connect(self._on_editing_finished)
        row.addWidget(self.edit, 1)
        self.floor = QComboBox()
        self.floor.setMinimumContentsLength(10)
        self.floor.setToolTip("طبقه (برای پله، آسانسور و مشاعات)")
        self.floor.hide()
        self.floor.currentIndexChanged.connect(self._on_floor)
        row.addWidget(self.floor)
        layout.addLayout(row)
        self.summary = QLabel(objectName="StatusText")  # muted; red when property error=true
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.add_button = QPushButton("+ افزودن محل جدید")
        self.add_button.setProperty("variant", "link")
        self.add_button.hide()
        self.add_button.clicked.connect(self._on_add)
        layout.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignRight)
        self._add: Callable[[str], Awaitable[int | None]] | None = None
        self.edit.installEventFilter(self)
        self.setFocusProxy(self.edit)

    def enable_add(self, create: Callable[[str], Awaitable[int | None]]) -> None:
        """«+ افزودن محل جدید»: `create(typed)` makes the place and returns its id (None = cancelled)."""
        self._add = create
        self.add_button.show()

    def _on_add(self) -> None:
        if self._add is not None:
            spawn(self._add_place())

    async def _add_place(self) -> None:
        location_id = await self._add(self.edit.text().strip())
        if location_id is None:
            return
        self.set_index(await locations.load_index(self._db))
        self.set_value(location_id)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (watched is self.edit and event.type() == QEvent.Type.FocusIn and self.location is None
                and not self.edit.text().strip()):
            self.show_common()
        return super().eventFilter(watched, event)

    def show_common(self) -> None:
        """Before anything is typed: the places picked most often (toilets, parking, floors)."""
        if self._index is None:
            return
        self._results = self._index.common()
        self._model.setStringList([self._display(loc) for loc in self._results])
        if self._results:
            self.completer.complete()

    # ----- data -----

    async def load(self, db: Database) -> None:
        self._db = db
        self.set_index(await locations.load_index(db))

    def set_index(self, index: LocationIndex) -> None:
        self._index = index
        self.floor.blockSignals(True)
        self.floor.clear()
        self.floor.addItem("— طبقه —", None)
        for f in index.floors:
            if f.is_active:
                self.floor.addItem(f.display, f.id)
        self.floor.blockSignals(False)

    def value(self) -> tuple[int, int | None] | None:
        """(location_id, floor_id); floor_id None means «the location's own floor»."""
        if self.location is None:
            return None
        return self.location.id, self.floor.currentData() if self.location.needs_floor else None

    def chosen_floor(self) -> FloorRow | None:
        if self.location is None:
            return None
        if not self.location.needs_floor:
            return self.location.floor
        floor_id = self.floor.currentData()
        return next((f for f in self._index.floors if f.id == floor_id), None) if self._index else None

    def set_value(self, location_id: int | None, floor_id: int | None = None) -> None:
        loc = None
        if location_id is not None and self._index is not None:
            loc = next((x for x in self._index.locations if x.id == location_id), None)
        self._select(loc, floor_id)

    def clear(self) -> None:
        self._select(None)

    def validate(self) -> None:
        """Raises ValidationError when the place is incomplete (nothing chosen / floor missing)."""
        if self.location is None:
            raise ValidationError(NOTHING_CHOSEN)
        if self.location.needs_floor and self.floor.currentData() is None:
            raise ValidationError(locations.FLOOR_REQUIRED)

    # ----- interaction -----

    def _on_text(self, text: str) -> None:
        if self.location is not None:  # typing again starts a new choice
            self.location = None
            self.floor.hide()
            self.changed.emit()
        if self._index is None:
            return
        try:
            self._results = self._index.search(text, limit=30) if text.strip() else self._index.common()
        except Phase1Plate as exc:
            self._results = []
            self._model.setStringList([])
            self._show_error(exc.message)
            return
        self.summary.setText("")
        self.summary.setProperty("error", False)
        self._model.setStringList([self._display(loc) for loc in self._results])
        if self._results:
            self.completer.complete()
        else:
            self.completer.popup().hide()

    @staticmethod
    def _display(loc: LocationRow) -> str:
        return loc.label() if not loc.needs_floor else f"{loc.label()} (طبقه را انتخاب کنید)"

    def _on_completion(self, text: str) -> None:
        loc = next((x for x in self._results if self._display(x) == text), None)
        if loc is not None:
            self._select(loc)

    def _on_enter(self) -> None:
        if self.completer.popup().isVisible():
            return  # the completer handles Enter on the highlighted suggestion
        self._try_exact()

    def _on_editing_finished(self) -> None:
        if self.location is None and self.edit.text().strip():
            self._try_exact()

    def _try_exact(self) -> None:
        if self._index is None or self.location is not None:
            return
        text = self.edit.text()
        try:
            loc = self._index.exact(text)
        except Phase1Plate as exc:
            self._show_error(exc.message)
            return
        if loc is None and len(self._results) == 1:
            loc = self._results[0]
        if loc is not None:
            self._select(loc)
        elif text.strip():
            self._show_error("محلی با این نام پیدا نشد؛ از فهرست انتخاب کنید.")

    def _on_floor(self, _index: int) -> None:
        self._update_summary()
        self.changed.emit()

    def _select(self, loc: LocationRow | None, floor_id: int | None = None) -> None:
        self.location = loc
        self.completer.popup().hide()
        self.edit.blockSignals(True)
        self.edit.setText(loc.label() if loc and not loc.needs_floor else (locations.fa_digits(loc.name_fa)
                                                                          if loc else ""))
        self.edit.blockSignals(False)
        needs_floor = bool(loc and loc.needs_floor)
        self.floor.blockSignals(True)
        self.floor.setCurrentIndex(max(self.floor.findData(floor_id), 0) if needs_floor else 0)
        self.floor.blockSignals(False)
        self.floor.setVisible(needs_floor)
        self._update_summary()
        if needs_floor and floor_id is None:
            self.floor.setFocus()
        self.changed.emit()

    def _update_summary(self) -> None:
        self.summary.setProperty("error", False)
        if self.location is None:
            self.summary.setText("")
        elif self.location.needs_floor and self.floor.currentData() is None:
            self.summary.setText(locations.FLOOR_REQUIRED)
            self.summary.setProperty("error", True)
        else:
            self.summary.setText(self.location.label(self.chosen_floor()))
        self._repolish()

    def _show_error(self, text: str) -> None:
        self.summary.setText(text)
        self.summary.setProperty("error", True)
        self._repolish()

    def _repolish(self) -> None:
        self.summary.style().unpolish(self.summary)
        self.summary.style().polish(self.summary)

    def keyPressEvent(self, event) -> None:
        # Enter inside the picker must never reach the dialog's default button (saving the
        # document) — it only picks a location.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)
