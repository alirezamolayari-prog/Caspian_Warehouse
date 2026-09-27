"""Parsing and display of quantities/prices (Decimal, Persian digits, separators)."""

from decimal import Decimal, InvalidOperation

from caspian.core.text import to_ascii_digits, to_persian_digits

_SEPARATORS = str.maketrans({",": "", "،": "", "٬": "", " ": "", "‌": ""})


def parse_decimal(text: str | None) -> Decimal | None:
    """Parse user input like '۱٬۲۵۰٫۵' or '1,250.5'. Empty -> None; invalid -> ValueError."""
    if text is None:
        return None
    text = to_ascii_digits(text.strip()).translate(_SEPARATORS).replace("٫", ".")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Not a number: {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"Not a number: {text!r}")
    return value


def format_qty(value: Decimal | int | None, persian: bool = True) -> str:
    """1250.5000 -> '۱٬۲۵۰٫۵'. None -> ''."""
    if value is None:
        return ""
    value = Decimal(value)
    text = f"{value:,.4f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
    if text in ("-0", ""):
        text = "0"
    if not persian:
        return text
    return to_persian_digits(text.replace(",", "٬").replace(".", "٫"))
