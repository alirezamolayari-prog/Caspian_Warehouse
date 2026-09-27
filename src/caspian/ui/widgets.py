"""Small reusable widgets styled through object names in theme.py."""

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

from PySide6.QtCore import QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
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
