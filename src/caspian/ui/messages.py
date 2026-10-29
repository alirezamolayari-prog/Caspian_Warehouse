import asyncio
import time
import traceback
from collections.abc import Sequence

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


async def ask(parent: QWidget | None, text: str, choices: Sequence[tuple[str, str]],
              title: str = "تأیید", danger: bool = False, cancel: bool = True) -> str | None:
    """Ask without blocking the event loop. `choices` = (key, button text), the first one is the
    default; returns the chosen key, or None for «انصراف» / Esc / closing the box. `cancel=False`:
    no extra «انصراف» button — the last choice is what Esc / closing means."""
    box = QMessageBox(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question, title, text,
                      QMessageBox.StandardButton.NoButton, parent)
    keys = {}
    for i, (key, label) in enumerate(choices):
        button = box.addButton(label, QMessageBox.ButtonRole.AcceptRole)
        keys[button] = key
        if i == 0:
            box.setDefaultButton(button)
    if cancel:
        box.setEscapeButton(box.addButton("انصراف", QMessageBox.ButtonRole.RejectRole))
    else:
        box.setEscapeButton(button)
    future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    box.buttonClicked.connect(lambda b: future.done() or future.set_result(keys.get(b)))
    box.finished.connect(lambda _r: future.done() or future.set_result(None))
    _show(box)
    return await future


async def confirm(parent: QWidget | None, text: str, yes_text: str = "بله", danger: bool = False,
                  title: str = "تأیید") -> bool:
    """«بله / انصراف» without blocking the event loop; Esc or closing counts as «انصراف»."""
    return await ask(parent, text, [("yes", yes_text)], title, danger) == "yes"


def show_error(parent: QWidget | None, text: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Warning, "خطا", text, QMessageBox.StandardButton.Ok, parent)
    box.button(QMessageBox.StandardButton.Ok).setText("باشه")
    _show(box)


def show_info(parent: QWidget | None, text: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Information, "اطلاع", text,
                      QMessageBox.StandardButton.Ok, parent)
    box.button(QMessageBox.StandardButton.Ok).setText("باشه")
    _show(box)


UNEXPECTED_TEXT = "خطایی رخ داد و این کار انجام نشد. دوباره امتحان کنید."
REPEAT_SECONDS = 5.0  # the same error again within this time (or while its box is open): no new box
_recent: dict[tuple[str, str], float] = {}


def _error_key(exc_type, exc) -> tuple[str, str]:
    return (getattr(exc_type, "__qualname__", str(exc_type)), str(exc)[:500])


def show_unexpected_error(exc_type, exc, tb) -> QMessageBox | None:
    """What the global exception hook shows: one short Persian sentence and the log path. The
    technical text (exception, SQL, traceback) never appears in the window — it is in the log, and
    «کپی جزئیات برای پشتیبانی» copies it. The same error twice shows one box."""
    key = _error_key(exc_type, exc)
    now = time.monotonic()
    boxes = [b for b in _live_boxes() if b.objectName() == "UnexpectedError"]
    if any(b.property("errorKey") == "|".join(key) for b in boxes):
        return None
    if now - _recent.get(key, -REPEAT_SECONDS) < REPEAT_SECONDS or len(boxes) >= MAX_ERROR_DIALOGS:
        return None
    _recent[key] = now
    path = log_file()
    box = QMessageBox(
        QMessageBox.Icon.Critical, "خطا",
        f"{UNEXPECTED_TEXT}\n\n"
        "اگر دوباره پیش آمد، دکمهٔ «کپی جزئیات برای پشتیبانی» را بزنید و متن را برای پشتیبانی "
        f"بفرستید. گزارش کامل در این فایل است:\n{path}",
        QMessageBox.StandardButton.Ok, QApplication.activeWindow())
    box.setObjectName("UnexpectedError")
    box.setProperty("errorKey", "|".join(key))
    box.button(QMessageBox.StandardButton.Ok).setText("بستن")
    details = "".join(traceback.format_exception(exc_type, exc, tb))
    copy = box.addButton("کپی جزئیات برای پشتیبانی", QMessageBox.ButtonRole.ActionRole)
    copy.clicked.connect(lambda: QApplication.clipboard().setText(f"{details}\nLog: {path}"))
    return _show(box)
