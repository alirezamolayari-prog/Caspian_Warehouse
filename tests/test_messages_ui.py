import gc
import re
import sys

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from caspian import app as app_module
from caspian.core.logging_setup import log_file
from caspian.ui import messages
from helpers import wait_until


@pytest.fixture(autouse=True)
def _fresh_error_memory(monkeypatch):
    monkeypatch.setattr(messages, "_recent", {})


def _visible_boxes() -> list[QMessageBox]:
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()]


async def test_unhandled_error_shows_persian_dialog_with_log_path(qtbot, monkeypatch):
    """Errors in async slots end in sys.excepthook; the user sees a short Persian sentence, never the
    exception, SQL or an English «Show Details…» button (#2, 1.3)."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # restored after the test
    app_module.install_exception_hook()
    try:
        raise ValueError("UPDATE goods_requests SET unseen=%s WHERE id=%s")
    except ValueError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    gc.collect()  # the dialog must not depend on a local variable staying alive
    assert await wait_until(lambda: bool(_visible_boxes()))
    box = _visible_boxes()[0]
    assert messages.UNEXPECTED_TEXT in box.text()
    assert str(log_file()) in box.text()
    shown = box.text().replace(str(log_file()), "")
    assert not re.search(r"[A-Za-z]", shown), shown  # no English, no SQL, no exception name
    assert box.detailedText() == ""
    assert all(not re.search(r"[A-Za-z]", b.text()) for b in box.buttons())
    copy = next(b for b in box.buttons() if b.text() == "کپی جزئیات برای پشتیبانی")
    copy.click()
    assert "ValueError" in QApplication.clipboard().text()
    assert "Traceback" in QApplication.clipboard().text()
    box.close()


async def test_the_same_error_twice_shows_one_dialog(qtbot):
    for _ in range(2):  # e.g. MariaDB 1020 reported by two slots at once
        try:
            raise RuntimeError("(1020, Record has changed since last read)")
        except RuntimeError as exc:
            messages.show_unexpected_error(type(exc), exc, exc.__traceback__)
    assert await wait_until(lambda: bool(_visible_boxes()))
    assert len([b for b in _visible_boxes() if b.objectName() == "UnexpectedError"]) == 1
    for box in _visible_boxes():
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


async def test_ask_returns_the_clicked_choice_or_none(qtbot):
    import asyncio

    task = asyncio.ensure_future(messages.ask(None, "ادامه؟", [("yes", "بله"), ("no", "خیر")]))
    assert await wait_until(lambda: bool(_visible_boxes()))
    box = _visible_boxes()[0]
    next(b for b in box.buttons() if b.text() == "خیر").click()
    assert await task == "no"
    task = asyncio.ensure_future(messages.ask(None, "ادامه؟", [("yes", "بله")]))
    assert await wait_until(lambda: bool(_visible_boxes()))
    next(b for b in _visible_boxes()[0].buttons() if b.text() == "انصراف").click()
    assert await task is None
