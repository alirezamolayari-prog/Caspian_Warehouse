from PySide6.QtWidgets import QMessageBox, QWidget


def show_error(parent: QWidget | None, text: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Warning, "خطا", text, QMessageBox.StandardButton.Ok, parent)
    box.button(QMessageBox.StandardButton.Ok).setText("باشه")
    box.open()


def show_info(parent: QWidget | None, text: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Information, "اطلاع", text,
                      QMessageBox.StandardButton.Ok, parent)
    box.button(QMessageBox.StandardButton.Ok).setText("باشه")
    box.open()
