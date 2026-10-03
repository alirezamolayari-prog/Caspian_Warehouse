import asyncio
from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog

from caspian.core import files
from caspian.core.settings import Settings
from caspian.ui import file_dialogs
from helpers import wait_until


def _open_file_dialog() -> QFileDialog | None:
    for w in QApplication.topLevelWidgets():
        if isinstance(w, QFileDialog) and w.isVisible():
            return w
    return None


async def test_save_dialog_is_awaitable_and_remembers_folder(qtbot, tmp_path, monkeypatch):
    """The dialog must not block the event loop: other tasks keep running while it is open (#35)."""
    monkeypatch.setattr(files, "user_documents_dir", lambda: str(tmp_path))
    settings = Settings()
    ticks = 0

    async def other_task():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.ensure_future(other_task())
    task = asyncio.ensure_future(file_dialogs.ask_save_path(
        None, "ذخیره", settings, "کاردکس کالا: جارو.xlsx", "Excel (*.xlsx)"))
    assert await wait_until(lambda: _open_file_dialog() is not None)
    dialog = _open_file_dialog()
    assert dialog.directory().absolutePath() == Path(tmp_path).as_posix()
    assert dialog.selectedFiles()[0].endswith("کاردکس کالا جارو.xlsx")  # sanitized name (#6)
    before = ticks
    assert await wait_until(lambda: ticks > before + 3)  # the loop is alive meanwhile
    target = tmp_path / "out"
    target.mkdir()
    dialog.selectFile(str(target / "r.xlsx"))
    dialog.accept()
    assert Path(await task) == target / "r.xlsx"
    assert settings.last_export_dir == str(target)
    ticker.cancel()


async def test_cancelled_dialog_returns_none(qtbot):
    task = asyncio.ensure_future(file_dialogs.ask_open_path(None, "باز کردن", "*.xlsx"))
    assert await wait_until(lambda: _open_file_dialog() is not None)
    _open_file_dialog().reject()
    assert await task is None
