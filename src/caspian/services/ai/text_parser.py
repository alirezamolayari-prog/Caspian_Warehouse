"""Offline parser for simple Persian item lists: "۵ عدد دریل بوش", "پیچ ۱۰۰ تا", "دو کارتن چسب".

Used when no AI provider is reachable, so typed-text drafts still work fully offline.
Anything it can't read is kept as a row with an error for the reviewer.
"""

import re
from decimal import Decimal

from caspian.core.numbers import parse_decimal
from caspian.core.text import normalize, to_ascii_digits
from caspian.services.import_files import RawRow

_ONES = {"یک": 1, "یه": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "شیش": 6, "هفت": 7,
         "هشت": 8, "نه": 9, "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14,
         "پانزده": 15, "پونزده": 15, "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19}
_TENS = {"بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60, "هفتاد": 70, "هشتاد": 80,
         "نود": 90}
_HUNDREDS = {"صد": 100, "یکصد": 100, "دویست": 200, "سیصد": 300, "چهارصد": 400, "پانصد": 500}
_WORDS = {normalize(k): v for k, v in {**_ONES, **_TENS, **_HUNDREDS}.items()}
# Colloquial counters meaning "pieces" -> the item's base unit. ("عدد" stays an explicit unit so
# "۵ عدد کابل" for an item counted in meters is flagged instead of silently read as 5 m.)
_PIECES = {"تا", "دانه"}
_NUMBER = re.compile(r"^[0-9]+(?:[.٫][0-9]+)?$")


def _read_number(tokens: list[str], start: int) -> tuple[Decimal | None, int]:
    """Parse a number (digits or Persian words like «بیست و پنج») at tokens[start]."""
    token = to_ascii_digits(tokens[start])
    if _NUMBER.match(token):
        return parse_decimal(token), start + 1
    total, i, seen = 0, start, False
    while i < len(tokens):
        value = _WORDS.get(normalize(tokens[i]))
        if value is None:
            break
        total += value
        seen = True
        i += 1
        if i + 1 < len(tokens) and tokens[i] == "و" and normalize(tokens[i + 1]) in _WORDS:
            i += 1  # «بیست و پنج»
    return (Decimal(total), i) if seen else (None, start)


def parse_line(line: str, unit_names: set[str]) -> RawRow | None:
    tokens = line.replace("×", " × ").split()
    if not tokens:
        return None
    units = {normalize(u) for u in unit_names}
    qty, unit = None, ""
    # number at the start: «۵ عدد دریل»
    value, nxt = _read_number(tokens, 0)
    if value is not None:
        qty, rest = value, tokens[nxt:]
        if rest and (normalize(rest[0]) in units or rest[0] in _PIECES):
            unit = "" if rest[0] in _PIECES else rest[0]
            rest = rest[1:]
    else:
        # number at the end: «دریل بوش ۵ تا» / «پیچ ۲ کارتن»
        rest = tokens
        for k in range(len(tokens) - 1, 0, -1):
            value, end = _read_number(tokens, k)
            tail = tokens[end:]
            if value is not None and (not tail or (len(tail) == 1 and (
                    normalize(tail[0]) in units or tail[0] in _PIECES))):
                qty = value
                unit = tail[0] if tail and tail[0] not in _PIECES else ""
                rest = tokens[:k]
                break
    name = " ".join(rest).strip(" -:،,.")
    row = RawRow(name=name, qty=qty, unit_name=unit, raw={"متن": line.strip()})
    if not name:
        row.error = "نام کالا در این خط پیدا نشد."
    elif qty is None:
        row.error = "مقدار در این خط پیدا نشد."
    return row


def parse_text(text: str, unit_names: set[str]) -> list[RawRow]:
    """One item per line (or separated by «،» / «,»)."""
    rows = []
    for part in re.split(r"[\n\r،,؛;]+", text):
        row = parse_line(part, unit_names)
        if row is not None:
            rows.append(row)
    return rows
