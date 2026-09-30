import gc
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from caspian import app as app_module
from caspian.core.logging_setup import log_file
from caspian.ui import messages
from helpers import wait_until


def _visible_boxes() -> list[QMessageBox]:
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()]


async def test_unhandled_error_shows_persian_dialog_with_log_path(qtbot, monkeypatch):
    """Errors in async slots end in sys.excepthook; the user must see them, not only the log (#2)."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # restored after the test
    app_module.install_exception_hook()
    try:
        raise ValueError("چیزی خراب شد")
    except ValueError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    gc.collect()  # the dialog must not depend on a local variable staying alive
    assert await wait_until(lambda: bool(_visible_boxes()))
    box = _visible_boxes()[0]
    assert "خطای غیرمنتظره" in box.text()
    assert "چیزی خراب شد" in box.text()
    assert str(log_file()) in box.text()
    assert "Traceback" in box.detailedText()
    copy = next(b for b in box.buttons() if b.text() == "کپی جزئیات")
    copy.click()
    assert "ValueError" in QApplication.clipboard().text()
    box.close()


async def test_parentless_messages_survive_garbage_collection(qtbot):
    messages.show_error(None, "خطای آزمایشی")
    messages.show_info(None, "پیام آزمایشی")
    gc.collect()
    assert await wait_until(lambda: len(_visible_boxes()) == 2)
    texts = sorted(b.text() for b in _visible_boxes())
    assert texts == ["خطای آزمایشی", "پیام آزمایشی"]
    for box in _visible_boxes():
        box.close()
    assert await wait_until(lambda: not messages._live_boxes())


async def test_box_destroyed_with_its_parent_does_not_break_the_hook(qtbot, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    messages.show_error(parent, "x")
    parent.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    try:
        raise KeyError("y")
    except KeyError as exc:
        assert messages.show_unexpected_error(type(exc), exc, exc.__traceback__) is not None
