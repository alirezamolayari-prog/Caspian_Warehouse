"""Bug list round 1 #4: a cell cut short with «…» shows its full text when the mouse rests on it."""

from caspian.ui.widgets import DataTable


def test_cells_show_their_full_text_on_hover(qtbot):
    table = DataTable(("کد", "نام کالا"))
    qtbot.addWidget(table)
    name = "کیسه زباله بزرگ ۱۲۰ لیتری مشکی مخصوص سطل‌های پارکینگ"
    table.set_rows([(1, ("1024", name))])
    assert table.item(0, 1).toolTip() == name
    assert table.item(0, 0).toolTip() == "1024"
