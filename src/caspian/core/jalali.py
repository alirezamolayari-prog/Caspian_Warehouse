"""Jalali (Solar Hijri) calendar helpers. The fiscal year starts on 1 Farvardin."""

import datetime as dt

import jdatetime

from caspian.core.text import to_ascii_digits, to_persian_digits

MONTH_NAMES = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)


def to_jalali(value: dt.date) -> jdatetime.date:
    if isinstance(value, dt.datetime):
        value = value.date()
    return jdatetime.date.fromgregorian(date=value)


def format_date(value: dt.date, persian_digits: bool = True) -> str:
    """Format as YYYY/MM/DD in Jalali."""
    j = to_jalali(value)
    text = f"{j.year:04d}/{j.month:02d}/{j.day:02d}"
    return to_persian_digits(text) if persian_digits else text


def format_long(value: dt.date) -> str:
    """Format as e.g. '۵ مهر ۱۴۰۵'."""
    j = to_jalali(value)
    return to_persian_digits(f"{j.day} {MONTH_NAMES[j.month - 1]} {j.year}")


def parse_date(text: str) -> dt.date:
    """Parse a Jalali date written as YYYY/MM/DD or YYYY-MM-DD (any digit script)."""
    parts = to_ascii_digits(text.strip()).replace("-", "/").split("/")
    if len(parts) != 3:
        raise ValueError(f"Invalid Jalali date: {text!r}")
    year, month, day = (int(p) for p in parts)
    return jdatetime.date(year, month, day).togregorian()


def fiscal_year_of(value: dt.date) -> int:
    return to_jalali(value).year


def fiscal_year_bounds(year: int) -> tuple[dt.date, dt.date]:
    """Gregorian first and last day of a Jalali fiscal year."""
    start = jdatetime.date(year, 1, 1).togregorian()
    end = jdatetime.date(year + 1, 1, 1).togregorian() - dt.timedelta(days=1)
    return start, end
