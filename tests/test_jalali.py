import datetime as dt

import pytest

from caspian.core import jalali

NOWRUZ_1405 = dt.date(2026, 3, 21)


def test_format_date():
    assert jalali.format_date(NOWRUZ_1405, persian_digits=False) == "1405/01/01"
    assert jalali.format_date(NOWRUZ_1405) == "۱۴۰۵/۰۱/۰۱"


def test_format_long():
    assert jalali.format_long(dt.date(2026, 9, 27)) == "۵ مهر ۱۴۰۵"


@pytest.mark.parametrize("text", ["1405/01/01", "۱۴۰۵/۰۱/۰۱", "1405-1-1"])
def test_parse_date(text):
    assert jalali.parse_date(text) == NOWRUZ_1405


def test_parse_invalid():
    with pytest.raises(ValueError):
        jalali.parse_date("1405/13")


def test_fiscal_year():
    assert jalali.fiscal_year_of(dt.date(2026, 3, 20)) == 1404
    assert jalali.fiscal_year_of(NOWRUZ_1405) == 1405
    start, end = jalali.fiscal_year_bounds(1404)
    assert start == dt.date(2025, 3, 21)
    assert end == dt.date(2026, 3, 20)
