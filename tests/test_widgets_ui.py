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


def test_table_sorts_numbers_and_keeps_ids(qtbot):
    """#29: clicking a header sorts; Persian-digit numbers sort by value, ids follow their rows."""
    from caspian.ui.widgets import DataTable

    table = DataTable(("شماره", "نام"))
    qtbot.addWidget(table)
    table.set_rows([(1, ("۱۰", "ب")), (2, ("۹", "الف")), (3, ("۱٬۰۰۰", "پ"))])
    assert [table.item(r, 0).text() for r in range(3)] == ["۱۰", "۹", "۱٬۰۰۰"]  # service order kept
    table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert [table.item(r, 0).text() for r in range(3)] == ["۹", "۱۰", "۱٬۰۰۰"]
    table.select_id(1)
    assert table.selected_id() == 1
    table.set_rows([(4, ("۵", "ت")), (1, ("۲", "ث"))])  # refresh keeps the user's sort column
    assert [table.item(r, 0).text() for r in range(2)] == ["۲", "۵"]


def test_empty_tables_explain_themselves(qtbot):
    from caspian.ui.widgets import DataTable

    table = DataTable(("الف",))
    qtbot.addWidget(table)
    table.set_empty_text("چیزی نیست")
    assert table.showing_empty_text()
    table.set_rows([(1, ("x",))])
    assert not table.showing_empty_text()


def test_inputs_show_persian_digits_and_jalali_picker(qtbot):
    """#28: forms show digits like the tables; the date field has a Jalali calendar."""
    import datetime as dt
    from decimal import Decimal

    from caspian.ui.widgets import JalaliDateEdit, QtyEdit

    qty = QtyEdit(Decimal("12.5"))
    qtbot.addWidget(qty)
    assert qty.text() == "۱۲٫۵" and qty.value() == Decimal("12.5")
    date = JalaliDateEdit(dt.date(2026, 9, 27))
    qtbot.addWidget(date)
    assert date.text() == "۱۴۰۵/۰۷/۰۵" and date.date() == dt.date(2026, 9, 27)
    popup = date.open_calendar()
    assert "مهر" in popup.title.text()
    days = popup.day_buttons()
    assert len(days) == 30
    days[1].click()
    assert date.date() == dt.date(2026, 9, 23)  # 1405/07/01
    popup = date.open_calendar()
    popup._move(-7)  # Mehr 1405 -> Esfand 1404
    assert "اسفند" in popup.title.text()
    import jdatetime

    assert len(popup.day_buttons()) == (30 if jdatetime.date(1404, 1, 1).isleap() else 29)
    assert all(b.isEnabled() for b in popup.day_buttons().values())  # the past is selectable
    popup._move(20)
    assert not any(b.isEnabled() for b in popup.day_buttons().values())  # the future is not (#18)
