"""Small reusable widgets styled through object names in theme.py."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(6)


class StatCard(Card):
    def __init__(self, title: str, value: str = "—", hint: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = QLabel(title, objectName="CardTitle")
        self.value = QLabel(value, objectName="CardValue")
        self.hint = QLabel(hint, objectName="CardHint")
        for label in (self.title, self.value, self.hint):
            self.body.addWidget(label)
        self.hint.setVisible(bool(hint))

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
