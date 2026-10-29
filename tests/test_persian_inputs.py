"""4.2: inputs show Persian digits while typing (with «٬» between thousands) and accept Persian or
Latin digits; the stored value is a plain number."""

from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from caspian.ui.widgets import PersianSpinBox, QtyEdit, persian_number_text


def test_number_text_while_typing():
    assert persian_number_text("150000") == "۱۵۰٬۰۰۰"
    assert persian_number_text("۱۵۰۰۰۰") == "۱۵۰٬۰۰۰"
    assert persian_number_text("4") == "۴"
    assert persian_number_text("12.5") == "۱۲٫۵"
    assert persian_number_text("-1500") == "-۱٬۵۰۰"
    assert persian_number_text("") == ""


def test_typing_latin_digits_shows_persian_and_keeps_the_value(qtbot):
    edit = QtyEdit()
    qtbot.addWidget(edit)
    QTest.keyClicks(edit, "150000")
    assert edit.text() == "۱۵۰٬۰۰۰" and edit.value() == Decimal(150000)
    assert edit.cursorPosition() == len(edit.text())
    edit.set_value(Decimal(150000))
    assert edit.text() == "۱۵۰٬۰۰۰"
    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    assert edit.value() == Decimal(15000) and edit.text() == "۱۵٬۰۰۰"


def test_spin_box_shows_persian_and_reads_both(qtbot):
    spin = PersianSpinBox()
    qtbot.addWidget(spin)
    spin.setRange(1, 1000)
    spin.setValue(30)
    assert spin.text() == "۳۰"
    assert spin.valueFromText("۴۵") == 45 and spin.valueFromText("45") == 45
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin, "60")
    spin.interpretText()
    assert spin.value() == 60
