"""Confirmation card for a stock change the assistant prepared (QA, Part A, issue 1).

The assistant only creates drafts. Its summary is shown here in plain Persian with two big
buttons; only «بله، ثبت کن» posts — as the signed-in person, through the normal service, so every
rule applies — and «نه» deletes the draft. Nothing changes stock without that click.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from caspian.core.permissions import Perm
from caspian.services.documents import Confirmation
from caspian.ui.widgets import Card


class ConfirmCard(Card):
    confirmed = Signal(object)  # self
    rejected = Signal(object)
    open_requested = Signal(int)  # document id

    def __init__(self, summary: Confirmation, can_post: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summary = summary
        self.setObjectName("ConfirmCard")
        title = QLabel(f"آیا این {summary.title} ثبت نهایی شود؟", objectName="CardTitle")
        title.setWordWrap(True)
        self.body.addWidget(title)
        lines = "\n".join(f"• {line}" for line in summary.lines)
        details = "\n".join(f"{label}: {value}" for label, value in summary.details if value)
        self.text = QLabel(f"{lines}\n{details}".strip())
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.text)
        self.status = QLabel(objectName="StatusText")
        self.status.setWordWrap(True)
        self.status.hide()
        self.body.addWidget(self.status)
        row = QHBoxLayout()
        self.yes = QPushButton("بله، ثبت کن")
        self.yes.setProperty("variant", "primary")
        self.yes.setMinimumHeight(40)
        self.yes.clicked.connect(lambda: self.confirmed.emit(self))
        self.no = QPushButton("نه")
        self.no.setProperty("variant", "danger")
        self.no.setMinimumHeight(40)
        self.no.clicked.connect(lambda: self.rejected.emit(self))
        self.open_button = QPushButton("باز کردن سند")
        self.open_button.clicked.connect(lambda: self.open_requested.emit(summary.document_id))
        row.addWidget(self.yes, 2)
        row.addWidget(self.no, 1)
        row.addWidget(self.open_button)
        self.body.addLayout(row)
        if not can_post:
            self.yes.hide()
            self.show_message(f"ثبت نهایی نیاز به مجوز «{Perm.DOCUMENTS_POST.value}» دارد؛ پیش‌نویس در "
                              "«اسناد انبار» می‌ماند تا کاربر مجاز آن را ثبت کند.", error=False)

    def show_message(self, text: str, error: bool = True) -> None:
        self.status.setText(text)
        self.status.setProperty("error", error)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setVisible(bool(text))

    def set_busy(self, busy: bool) -> None:
        for b in (self.yes, self.no, self.open_button):
            b.setEnabled(not busy)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self.yes.isHidden():
            self.yes.click()
        elif event.key() == Qt.Key.Key_Escape:
            self.no.click()
        else:
            super().keyPressEvent(event)
