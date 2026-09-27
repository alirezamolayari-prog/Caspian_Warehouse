from decimal import Decimal

import pytest

from caspian.services.ai.text_parser import parse_line, parse_text

UNITS = {"عدد", "جعبه", "کارتن", "متر", "بسته"}


@pytest.mark.parametrize(
    ("line", "qty", "unit", "name"),
    [
        ("5 Bosch Drills", Decimal(5), "", "Bosch Drills"),
        ("۵ عدد دریل بوش", Decimal(5), "عدد", "دریل بوش"),
        ("دریل بوش ۵ تا", Decimal(5), "", "دریل بوش"),
        ("پیچ ام دی اف ۲ کارتن", Decimal(2), "کارتن", "پیچ ام دی اف"),
        ("دو کارتن چسب چوب", Decimal(2), "کارتن", "چسب چوب"),
        ("بیست و پنج متر نوار", Decimal(25), "متر", "نوار"),
        ("۲٫۵ متر کابل", Decimal("2.5"), "متر", "کابل"),
        ("لولا گازور ۱۲۰", Decimal(120), "", "لولا گازور"),
    ],
)
def test_parse_line(line, qty, unit, name):
    row = parse_line(line, UNITS)
    assert (row.qty, row.unit_name, row.name, row.error) == (qty, unit, name, "")


def test_problems_are_kept_for_review():
    assert "مقدار" in parse_line("دریل بوش", UNITS).error
    assert "نام" in parse_line("۵ عدد", UNITS).error


def test_parse_text_splits_lines_and_commas():
    rows = parse_text("۵ دریل، ۲ کارتن پیچ\n\nمیز کار ۳ تا", UNITS)
    assert [(r.name, r.qty) for r in rows] == [("دریل", 5), ("پیچ", 2), ("میز کار", 3)]
