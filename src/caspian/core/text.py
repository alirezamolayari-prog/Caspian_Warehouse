"""Persian text normalization.

Search and matching must treat Arabic and Persian variants of the same letter,
Persian/Arabic/Latin digits and stray diacritics as equivalent.
"""

import re

_CHAR_MAP = str.maketrans(
    {
        "ي": "ی",  # Arabic yeh
        "ى": "ی",  # Alef maksura
        "ئ": "ی",
        "ك": "ک",  # Arabic kaf
        "ة": "ه",
        "ۀ": "ه",
        "أ": "ا",
        "إ": "ا",
        "ٱ": "ا",
        "ؤ": "و",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
    }
)

# Harakat, tanwin, shadda, sukun, superscript alef, tatweel
_DIACRITICS = re.compile("[ً-ٰٟـ]")
_SPACES = re.compile(r"[\s‌‏‎]+")

_TO_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def normalize(text: str) -> str:
    """Canonical form for storage-side search keys and fuzzy matching.

    Unifies letter variants, converts all digits to ASCII, drops diacritics,
    treats ZWNJ as a space, collapses whitespace and lowercases Latin text.
    """
    if not text:
        return ""
    text = text.translate(_CHAR_MAP)
    text = _DIACRITICS.sub("", text)
    text = _SPACES.sub(" ", text)
    return text.strip().lower()


def to_ascii_digits(text: str) -> str:
    """Convert Persian/Arabic digits to ASCII without touching anything else."""
    return text.translate(_CHAR_MAP) if text else ""


def to_persian_digits(value: object) -> str:
    """Render a value with Persian digits for display."""
    return str(value).translate(_TO_PERSIAN_DIGITS)
