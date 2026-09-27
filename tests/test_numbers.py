from decimal import Decimal

import pytest

from caspian.core.numbers import format_qty, parse_decimal


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12", Decimal(12)),
        ("۱۲", Decimal(12)),
        ("۱٬۲۵۰٫۵", Decimal("1250.5")),
        ("1,250.50", Decimal("1250.50")),
        (" -3 ", Decimal(-3)),
        ("", None),
        (None, None),
    ],
)
def test_parse(text, expected):
    assert parse_decimal(text) == expected


@pytest.mark.parametrize("text", ["abc", "1.2.3", "NaN", "inf"])
def test_parse_invalid(text):
    with pytest.raises(ValueError):
        parse_decimal(text)


def test_format():
    assert format_qty(Decimal("1250.5000")) == "۱٬۲۵۰٫۵"
    assert format_qty(Decimal("24.0000")) == "۲۴"
    assert format_qty(Decimal("1234567"), persian=False) == "1,234,567"
    assert format_qty(Decimal("0.2500"), persian=False) == "0.25"
    assert format_qty(Decimal("-0.0000"), persian=False) == "0"
    assert format_qty(None) == ""
