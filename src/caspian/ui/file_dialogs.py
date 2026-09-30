"""File and print dialogs that can be awaited from async code.

The static QFileDialog.get*() / dialog.exec() run a nested event loop. Called from inside a
running task (any @asyncSlot), that nested loop lets qasync step other tasks — e.g. the
scheduler — while the calling task is still executing, and asyncio fails with "Cannot enter
into task … while another task … is being executed" (#35). These helpers open the dialog with
open() and await its result instead.
"""

from pathlib import Path

from PySide6.QtWidgets import QDialog, QFileDialog, QWidget

from caspian.core.files import export_path, remember_export_dir
from caspian.core.settings import Settings
from caspian.ui.app_context import exec_dialog


async def ask_save_path(parent: QWidget | None, title: str, settings: Settings, default_name: str,
                        name_filter: str) -> str | None:
    """Save dialog starting in the last export folder (else Documents) with a valid file name."""
    suggested = export_path(settings, default_name)
    dialog = QFileDialog(parent, title, suggested, name_filter)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    if suffix := Path(suggested).suffix.lstrip("."):
        dialog.setDefaultSuffix(suffix)
    dialog.selectFile(Path(suggested).name)
    if await exec_dialog(dialog) != QDialog.DialogCode.Accepted or not dialog.selectedFiles():
        return None
    path = dialog.selectedFiles()[0]
    remember_export_dir(settings, path)
    return path


async def ask_open_path(parent: QWidget | None, title: str, name_filter: str,
                        directory: str = "") -> str | None:
    dialog = QFileDialog(parent, title, directory, name_filter)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
    dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
    if await exec_dialog(dialog) != QDialog.DialogCode.Accepted or not dialog.selectedFiles():
        return None
    return dialog.selectedFiles()[0]


async def ask_print(printer, parent: QWidget | None = None) -> bool:
    """The system print dialog for `printer` (a QPrinter). False if cancelled."""
    from PySide6.QtPrintSupport import QPrintDialog

    dialog = QPrintDialog(printer, parent)
    return await exec_dialog(dialog) == QDialog.DialogCode.Accepted
