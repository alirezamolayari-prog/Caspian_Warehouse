"""Small reusable widgets styled through object names in theme.py."""

import datetime as dt
from collections.abc import Awaitable, Callable, Iterable, Sequence
from decimal import Decimal
from typing import Any

from PySide6.QtCore import QModelIndex, QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QFrame,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from caspian.core import jalali
from caspian.core.numbers import format_qty, parse_decimal
from caspian.core.text import normalize


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(6)


class StatCard(Card):
    clicked = Signal()

    def __init__(self, title: str, value: str = "—", hint: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title = QLabel(title, objectName="CardTitle")
        self.value = QLabel(value, objectName="CardValue")
        self.hint = QLabel(hint, objectName="CardHint")
        for label in (self.title, self.value, self.hint):
            self.body.addWidget(label)
        self.hint.setVisible(bool(hint))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def set_value(self, value: str, hint: str | None = None) -> None:
        self.value.setText(value)
        if hint is not None:
            self.hint.setText(hint)
            self.hint.setVisible(bool(hint))


class EmptyState(QWidget):
    """Centered title + explanation for pages with nothing to show yet."""

    def __init__(self, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        heading = QLabel(title, objectName="EmptyTitle")
        body = QLabel(text, objectName="EmptyText")
        body.setWordWrap(True)
        for label in (heading, body):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        layout.addStretch(2)


class DataTable(QTableWidget):
    """Read-only, row-selecting table. Rows carry an id retrievable via `selected_id()`."""

    def __init__(self, columns: Sequence[str], parent: QWidget | None = None,
                 multi_select: bool = False) -> None:
        super().__init__(0, len(columns), parent)
        self.setHorizontalHeaderLabels(list(columns))
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if multi_select
                              else QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setHighlightSections(False)
        self._ids: list[object] = []

    def set_rows(self, rows: Sequence[tuple[object, Sequence[str]]],
                 muted: Sequence[bool] | None = None,
                 highlight: dict[tuple[int, int], str] | None = None) -> None:
        """rows: (id, cell texts). muted: rows to grey out. highlight: (row, col) -> color."""
        keep = self.selected_id()
        self.setSortingEnabled(False)
        self.setRowCount(len(rows))
        self._ids = [row_id for row_id, _ in rows]
        muted_color = self.palette().placeholderText().color()
        for r, (_, cells) in enumerate(rows):
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, r)
                if muted and muted[r]:
                    item.setForeground(muted_color)
                if highlight and (r, c) in highlight:
                    item.setForeground(QColor(highlight[(r, c)]))
                self.setItem(r, c, item)
        if keep is not None and keep in self._ids:
            self.selectRow(self._ids.index(keep))

    def selected_id(self) -> object | None:
        model = self.selectionModel()
        rows = model.selectedRows() if model else []
        if not rows:
            return None
        index = self.item(rows[0].row(), 0)
        r = index.data(Qt.ItemDataRole.UserRole) if index else None
        return self._ids[r] if r is not None and r < len(self._ids) else None

    def selected_ids(self) -> list[object]:
        model = self.selectionModel()
        ids = []
        for index in model.selectedRows() if model else []:
            item = self.item(index.row(), 0)
            r = item.data(Qt.ItemDataRole.UserRole) if item else None
            if r is not None and r < len(self._ids):
                ids.append(self._ids[r])
        return ids

    def select_id(self, row_id: object) -> None:
        if row_id in self._ids:
            self.selectRow(self._ids.index(row_id))


class SearchBox(QLineEdit):
    """Emits `search(text)` shortly after the user stops typing (or on Enter)."""

    search = Signal(str)

    def __init__(self, placeholder: str, delay_ms: int = 250, parent: QWidget | None = None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.setMinimumWidth(280)
        self._timer = QTimer(self, singleShot=True, interval=delay_ms)
        self._timer.timeout.connect(lambda: self.search.emit(self.text()))
        self.textChanged.connect(lambda _: self._timer.start())
        self.returnPressed.connect(self._emit_now)

    def _emit_now(self) -> None:
        self._timer.stop()
        self.search.emit(self.text())


SEARCH_ROLE = Qt.ItemDataRole.UserRole + 50  # normalized text the completer matches against
_ADD = "__add__"


class _NormalizedCompleter(QCompleter):
    """Matches what the user types against normalized item text, so Arabic ي/ك, Persian ی/ک,
    ZWNJ and Persian/Latin digits all find the same entry."""

    def splitPath(self, path: str) -> list[str]:
        return [normalize(path)]

    def pathFromIndex(self, index: QModelIndex) -> str:
        return index.data(Qt.ItemDataRole.DisplayRole) or ""


class SearchableCombo(QComboBox):
    """Type -> suggestions (contains, normalized) -> Enter. Otherwise used like a QComboBox
    (addItem/findData/currentData). `enable_add` appends a «+ افزودن …» entry (#10)."""

    unmatched = Signal(str)  # Enter on text that matches no entry (e.g. a barcode)

    def __init__(self, placeholder: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(15)
        self.setMinimumContentsLength(14)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.lineEdit().setPlaceholderText(placeholder)
        self.model().rowsInserted.connect(self._index_rows)
        completer = _NormalizedCompleter(self.model(), self)
        completer.setCompletionRole(SEARCH_ROLE)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setMaxVisibleItems(15)
        self.setCompleter(completer)
        self._add: tuple[str, Callable[[str], Awaitable[tuple[str, Any] | None]]] | None = None
        self._last_index = -1
        self.activated.connect(self._on_activated)
        self.currentIndexChanged.connect(self._remember_index)

    # ----- items -----

    def _index_rows(self, _parent: QModelIndex, first: int, last: int) -> None:
        for row in range(max(first, 0), last + 1):
            text = "" if self.itemData(row) == _ADD else normalize(self.itemText(row))
            self.setItemData(row, text, SEARCH_ROLE)

    def _add_row(self) -> int:
        return self.findData(_ADD)

    def addItem(self, text: str, userData: Any = None) -> None:
        """Keeps the «+ افزودن …» entry last."""
        add_row = self._add_row()
        if add_row < 0:
            super().addItem(text, userData)
        else:
            self.insertItem(add_row, text, userData)

    def set_items(self, items: Iterable[tuple[str, Any]], none_text: str | None = None,
                  current: Any = None) -> None:
        self.blockSignals(True)
        self.clear()
        if none_text is not None:
            super().addItem(none_text, None)
        for text, value in items:
            super().addItem(text, value)
        if self._add is not None:
            super().addItem(self._add[0], _ADD)
        self.blockSignals(False)
        self.select_value(current)

    def select_value(self, value: Any) -> None:
        index = self.findData(value) if value is not None else -1
        if index < 0 and self.count() and self.itemData(0) != _ADD:
            index = 0
        self.setCurrentIndex(index)

    def enable_add(self, label: str, handler: Callable[[str], Awaitable[tuple[str, Any] | None]]) -> None:
        """`handler(typed_text)` creates the record and returns (text, value), or None."""
        self._add = (label, handler)
        if self._add_row() < 0:
            super().addItem(label, _ADD)

    # ----- interaction -----

    def _remember_index(self, index: int) -> None:
        if index >= 0 and self.itemData(index) != _ADD:
            self._last_index = index

    def _on_activated(self, index: int) -> None:
        if self.itemData(index) != _ADD or self._add is None:
            return
        typed = self.lineEdit().text()
        if normalize(typed) == normalize(self._add[0]):
            typed = ""
        self.setCurrentIndex(self._last_index)
        from caspian.ui.tasks import spawn

        spawn(self.run_add(typed))

    async def run_add(self, typed: str = "") -> Any:
        """Create a new entry through the add handler and select it; returns its value."""
        if self._add is None:
            return None
        result = await self._add[1](typed)
        if result is None:
            return None
        text, value = result
        if self.findData(value) < 0:
            self.addItem(text, value)
        self.setCurrentIndex(self.findData(value))
        return value

    def _matches(self, text: str) -> list[int]:
        key = normalize(text)
        rows = [r for r in range(self.count()) if self.itemData(r) != _ADD]
        exact = [r for r in rows if self.itemData(r, SEARCH_ROLE) == key]
        return exact or [r for r in rows if key and key in (self.itemData(r, SEARCH_ROLE) or "")]

    def commit_text(self) -> bool:
        """Select the entry the typed text stands for; restore the current text if none/ambiguous."""
        text = self.lineEdit().text()
        current = self.currentIndex()
        if current >= 0 and self.itemText(current) == text:
            return True
        matches = self._matches(text)
        if len(matches) == 1:
            self.setCurrentIndex(matches[0])
            self.lineEdit().setText(self.itemText(matches[0]))
            return True
        self.lineEdit().setText(self.itemText(current) if current >= 0 else "")
        return False

    def keyPressEvent(self, event) -> None:
        popup = self.completer().popup()
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not popup.isVisible():
            text = self.lineEdit().text()
            if self.commit_text():
                self.activated.emit(self.currentIndex())  # same as picking it from the list
            elif text.strip():
                self.unmatched.emit(text.strip())
            event.accept()  # Enter picks an entry; it must never submit the surrounding form
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        if not self.completer().popup().isVisible():
            self.commit_text()
        super().focusOutEvent(event)


class QtyEdit(QLineEdit):
    """Numeric input accepting Persian/Latin digits and separators. Empty = None."""

    def __init__(self, value: Decimal | None = None, allow_empty: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        pattern = QRegularExpression(r"^[-]?[0-9۰-۹٠-٩٬,]*([.٫][0-9۰-۹٠-٩]*)?$")
        self.setValidator(QRegularExpressionValidator(pattern, self))
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.allow_empty = allow_empty
        self.set_value(value)

    def value(self) -> Decimal | None:
        try:
            return parse_decimal(self.text())
        except ValueError:
            return None

    def set_value(self, value: Decimal | None) -> None:
        self.setText(format_qty(value, persian=False).replace(",", "") if value is not None
                     else "")


class JalaliDateEdit(QLineEdit):
    """Jalali date typed as YYYY/MM/DD (any digit script). Defaults to today."""

    def __init__(self, value: dt.date | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.setPlaceholderText("1405/01/01")
        self.setInputMask("")
        self.set_date(value or dt.date.today())

    def date(self) -> dt.date | None:
        try:
            return jalali.parse_date(self.text())
        except ValueError:
            return None

    def set_date(self, value: dt.date) -> None:
        self.setText(jalali.format_date(value, persian_digits=False))
