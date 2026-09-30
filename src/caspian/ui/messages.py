import traceback

import shiboken6
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from caspian.core.logging_setup import log_file

# Message boxes are opened non-modally with open(); a box without a parent is owned by Python
# and would be garbage-collected (never shown) once the caller returns. Keep them until closed.
_open_boxes: set[QMessageBox] = set()
MAX_ERROR_DIALOGS = 3  # a loop of failures must not bury the window in dialogs


def _live_boxes() -> set[QMessageBox]:
    """Open boxes; drops ones destroyed together with their parent window."""
    _open_boxes.difference_update([b for b in _open_boxes if not shiboken6.isValid(b)])
    return _open_boxes


def _show(box: QMessageBox) -> QMessageBox:
    _open_boxes.add(box)
    box.finished.connect(lambda _result: _open_boxes.discard(box))
    box.destroyed.connect(lambda *_: _open_boxes.discard(box))
    box.open()
    return box


def show_error(parent: QWidget | None, text: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Warning, "خطا", text, QMessageBox.StandardButton.Ok, parent)
    box.button(QMessageBox.StandardButton.Ok).setText("باشه")
    _show(box)


def show_info(parent: QWidget | None, text: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Information, "اطلاع", text,
                      QMessageBox.StandardButton.Ok, parent)
    box.button(QMessageBox.StandardButton.Ok).setText("باشه")
    _show(box)


def show_unexpected_error(exc_type, exc, tb) -> QMessageBox | None:
    """What the global exception hook shows: a Persian explanation, the log path, details."""
    if sum(b.objectName() == "UnexpectedError" for b in _live_boxes()) >= MAX_ERROR_DIALOGS:
        return None
    path = log_file()
    summary = f"{exc_type.__name__}: {exc}"[:300]
    box = QMessageBox(
        QMessageBox.Icon.Critical, "خطای غیرمنتظره",
        "خطای غیرمنتظره‌ای رخ داد و این کار انجام نشد.\n"
        f"{summary}\n\n"
        f"جزئیات در فایل گزارش ثبت شد:\n{path}\n\n"
        "اگر تکرار شد، این فایل را برای پشتیبانی بفرستید.",
        QMessageBox.StandardButton.Ok, QApplication.activeWindow())
    box.setObjectName("UnexpectedError")
    box.button(QMessageBox.StandardButton.Ok).setText("باشه")
    details = "".join(traceback.format_exception(exc_type, exc, tb))
    box.setDetailedText(details)
    copy = box.addButton("کپی جزئیات", QMessageBox.ButtonRole.ActionRole)
    copy.clicked.connect(
        lambda: QApplication.clipboard().setText(f"{details}\nLog: {path}"))
    return _show(box)
