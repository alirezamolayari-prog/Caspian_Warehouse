from PySide6.QtCore import Qt

from caspian.ui.widgets import SearchableCombo
from helpers import wait_until


def _combo(qtbot) -> SearchableCombo:
    combo = SearchableCombo("جستجو…")
    qtbot.addWidget(combo)
    combo.set_items([("علی کریمی", 1), ("کیان یکتا", 2), ("انبار مرکزی", 3)], none_text="—")
    return combo


def _suggestions(combo: SearchableCombo, typed: str) -> list[str]:
    completer = combo.completer()
    completer.setCompletionPrefix(typed)
    model = completer.completionModel()
    return [model.index(i, 0).data() for i in range(model.rowCount())]


def test_typing_arabic_letters_finds_persian_names(qtbot):
    """Arabic keyboards type ي/ك; the entries use Persian ی/ک (#10)."""
    combo = _combo(qtbot)
    assert _suggestions(combo, "كيان") == ["کیان یکتا"]
    assert _suggestions(combo, "کریمی") == ["علی کریمی"]  # contains, not only prefix
    assert _suggestions(combo, "مرکز") == ["انبار مرکزی"]


def test_enter_selects_and_never_leaks(qtbot):
    combo = _combo(qtbot)
    combo.lineEdit().setText("يكتا")
    qtbot.keyClick(combo, Qt.Key.Key_Return)
    assert combo.currentData() == 2 and combo.currentText() == "کیان یکتا"


def test_unmatched_text_is_reported_and_reverted(qtbot):
    combo = _combo(qtbot)
    combo.select_value(3)
    seen = []
    combo.unmatched.connect(seen.append)
    combo.lineEdit().setText("626111")
    qtbot.keyClick(combo, Qt.Key.Key_Return)
    assert seen == ["626111"]
    assert combo.currentData() == 3 and combo.lineEdit().text() == "انبار مرکزی"


async def test_add_entry_creates_and_selects(qtbot):
    combo = _combo(qtbot)
    asked = []

    async def create(typed):
        asked.append(typed)
        return ("شخص تازه", 9)

    combo.enable_add("+ افزودن شخص جدید", create)
    combo.addItem("بعدی", 10)  # added later: the add entry stays last
    assert combo.itemText(combo.count() - 1) == "+ افزودن شخص جدید"
    assert combo.itemText(combo.count() - 2) == "بعدی"
    assert _suggestions(combo, "افزودن") == []  # never suggested while typing
    combo.lineEdit().setText("رضا")
    combo.activated.emit(combo.count() - 1)
    assert await wait_until(lambda: combo.currentData() == 9)
    assert asked == ["رضا"]
    assert combo.itemText(combo.count() - 1) == "+ افزودن شخص جدید"
