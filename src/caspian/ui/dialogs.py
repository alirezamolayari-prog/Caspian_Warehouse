"""Base dialog: title, form, inline status line, and an async submit action."""

import logging

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core.logging_setup import log_file
from caspian.services.errors import ServiceError

log = logging.getLogger(__name__)


def password_field(placeholder: str = "") -> QLineEdit:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.setPlaceholderText(placeholder)
    return edit


def ltr_field(text: str = "") -> QLineEdit:
    """For usernames, codes etc. that are typed left-to-right."""
    edit = QLineEdit(text)
    edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    edit.setLocale(QLocale.c())
    return edit


class FormDialog(QDialog):
    """Subclasses add rows with `add_row` and implement `async submit()`.

    `submit` may raise ServiceError; its message is shown and the dialog stays open.
    Returning normally accepts the dialog.
    """

    def __init__(self, title: str, subtitle: str = "", submit_text: str = "تأیید",
                 cancel_text: str = "انصراف", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(24, 22, 24, 22)
        self._layout.setSpacing(14)
        self._layout.addWidget(QLabel(title, objectName="PageTitle"))
        if subtitle:
            self.subtitle = QLabel(subtitle, objectName="Muted")
            self.subtitle.setWordWrap(True)
            self._layout.addWidget(self.subtitle)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        self._layout.addLayout(self.body)
        self.form = QFormLayout()
        self.form.setSpacing(10)
        self.body.addLayout(self.form)

        self.status = QLabel(objectName="StatusText")
        self.status.setWordWrap(True)
        self.status.hide()
        self._layout.addWidget(self.status)

        self.buttons = buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton(cancel_text)
        self.cancel_button.clicked.connect(self.reject)
        self.submit_button = QPushButton(submit_text)
        self.submit_button.setProperty("variant", "primary")
        self.submit_button.setDefault(True)
        self.submit_button.clicked.connect(self.on_submit)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.submit_button)
        self._layout.addLayout(buttons)
        self._inputs: list[QWidget] = []

    def add_row(self, label: str, widget: QWidget) -> QWidget:
        self.form.addRow(label, widget)
        self._inputs.append(widget)
        return widget

    def show_status(self, text: str, is_error: bool = True) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.status.setProperty("error", is_error)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def set_busy(self, busy: bool) -> None:
        self.submit_button.setEnabled(not busy)
        for widget in self._inputs:
            widget.setEnabled(not busy)

    async def submit(self) -> None:
        raise NotImplementedError

    @asyncSlot()
    async def on_submit(self) -> None:
        self.set_busy(True)
        try:
            await self.submit()
        except ServiceError as exc:
            self.show_status(exc.message)
            return
        except Exception:
            log.exception("Dialog action failed")
            self.show_status(f"خطای غیرمنتظره رخ داد. جزئیات در فایل گزارش ثبت شد:\n{log_file()}")
            return
        finally:
            self.set_busy(False)
        self.accept()
