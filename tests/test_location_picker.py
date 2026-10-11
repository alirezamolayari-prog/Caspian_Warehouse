"""LocationPicker keyboard flow (spec §2.6). Latin input only: QTest with Persian text kills the
test process on Windows; Persian forms are covered by tests/test_locations.py."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from caspian.services import locations
from caspian.services.errors import ValidationError
from caspian.ui.location_picker import LocationPicker


@pytest.fixture
async def picker(qtbot, db):
    widget = LocationPicker()
    qtbot.addWidget(widget)
    widget.show()
    await widget.load(db)
    return widget


def _type(widget: LocationPicker, text: str) -> None:
    widget.edit.setFocus()
    QTest.keyClicks(widget.edit, text)


async def test_plate_number_then_enter_selects_directly(picker):
    _type(picker, "68")
    picker.completer.popup().hide()  # Enter on an exact plate selects it without the list
    QTest.keyClick(picker.edit, Qt.Key.Key_Return)
    assert picker.location.code == "68" and picker.floor.isHidden()
    assert picker.summary.text() == "پلاک ۶۸ · طبقه همکف"
    loc_id, floor_id = picker.value()
    assert floor_id is None and loc_id == picker.location.id
    picker.validate()


async def test_vertical_location_needs_a_floor(picker):
    _type(picker, "AB-ASD")
    picker.completer.popup().hide()
    QTest.keyClick(picker.edit, Qt.Key.Key_Return)
    assert picker.location.code == "AB-ASD" and not picker.floor.isHidden()
    with pytest.raises(ValidationError, match="طبقه"):
        picker.validate()
    assert picker.summary.property("error") is True
    picker.floor.setCurrentIndex(picker.floor.findText("طبقه دوم"))
    picker.validate()
    assert picker.summary.text() == "آسانسور باری اسدنژاد · طبقه دوم"
    assert picker.value()[1] is not None


async def test_arrow_and_enter_in_the_suggestions(picker, qtbot):
    _type(picker, "12")
    popup = picker.completer.popup()
    assert popup.isVisible() and picker._results[0].code == "12"
    QTest.keyClick(popup, Qt.Key.Key_Down)
    QTest.keyClick(popup, Qt.Key.Key_Down)
    chosen = picker._results[popup.currentIndex().row()]
    QTest.keyClick(popup, Qt.Key.Key_Return)
    assert picker.location is not None and picker.location.code == chosen.code


async def test_phase1_plate_is_refused(picker):
    _type(picker, "50")
    assert picker.summary.text() == locations.PHASE1_MESSAGE
    QTest.keyClick(picker.edit, Qt.Key.Key_Return)
    assert picker.location is None
    with pytest.raises(ValidationError):
        picker.validate()


async def test_enter_never_reaches_the_dialog(qtbot, db):
    from PySide6.QtWidgets import QDialog, QPushButton, QVBoxLayout

    dialog = QDialog()
    qtbot.addWidget(dialog)
    layout = QVBoxLayout(dialog)
    picker = LocationPicker()
    layout.addWidget(picker)
    ok = QPushButton("OK")
    ok.setDefault(True)
    ok.clicked.connect(dialog.accept)
    layout.addWidget(ok)
    dialog.show()
    await picker.load(db)
    _type(picker, "68")
    picker.completer.popup().hide()
    QTest.keyClick(picker.edit, Qt.Key.Key_Return)
    assert picker.location.code == "68" and dialog.result() == 0 and dialog.isVisible()


async def test_set_value_and_typing_again_clears_the_choice(picker):
    gen = next(loc for loc in picker._index.locations if loc.code == "GEN")
    f1 = next(f for f in picker._index.floors if f.code == "F1")
    picker.set_value(gen.id, f1.id)
    assert picker.value() == (gen.id, f1.id) and "طبقه اول" in picker.summary.text()
    _type(picker, "x")
    assert picker.location is None and picker.value() is None

