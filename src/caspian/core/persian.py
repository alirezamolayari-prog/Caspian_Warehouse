"""Persian names, mobile numbers and passwords for Caspian Server accounts — the same rules the
server applies (Caspian-Server docs/owner-auth-api.md «Data rules»), so the person sees a problem in
the form before anything is sent (and before an SMS is spent)."""

import re
import unicodedata

from caspian.core.text import to_ascii_digits

ZWNJ = "‌"
_ARABIC = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک"})
_MARKS = re.compile("[ً-ْٰـ]")  # vowel marks, tanwin, shadda, sukun, kashida
_BIDI = re.compile("[‎‏‪-‮⁦-⁩؜﻿]")
_SPACES = re.compile(r"[ \t   ]+")
# Persian/Arabic letters the server accepts, space and ZWNJ.
_NAME_OK = re.compile("^[ء-غف-يپچژکگیۀة"
                      "آأإؤئ ‌]+$")
NAME_MIN, NAME_MAX = 2, 50
NAME_ERROR = "نام و نام خانوادگی را با حروف فارسی بنویسید (۲ تا ۵۰ حرف)"

_MOBILE = re.compile(r"^(?:\+98|0098|98|0)?(9\d{9})$")

COMMON_PASSWORDS = frozenset({
    "12345678", "123456789", "1234567890", "87654321", "11111111", "00000000", "password",
    "password1", "password123", "admin123", "admin1234", "qwerty123", "qwertyui", "1q2w3e4r",
    "abc12345", "iloveyou", "11223344", "12341234", "09123456789",
})
PASSWORD_MIN = 8
PASSWORD_MAX_BYTES = 72


def clean_person_name(text: str) -> str:
    """«عَلي  » → «علی»: NFC, Arabic letters → Persian, hamza joined, marks and kashida removed,
    direction marks removed, spaces collapsed, ZWNJ only where it joins two letters."""
    text = unicodedata.normalize("NFC", text or "").translate(_ARABIC)
    text = text.replace("یٔ", "ئ").replace("هٔ", "ۀ")
    text = _MARKS.sub("", _BIDI.sub("", text))
    text = _SPACES.sub(" ", text).strip()
    text = re.sub(f"{ZWNJ}+", ZWNJ, text)
    text = re.sub(f" ?{ZWNJ} ?", lambda m: ZWNJ if m.group(0) == ZWNJ else " ", text)
    return text.strip(f" {ZWNJ}")


def name_problem(name: str) -> str | None:
    """None if the (cleaned) name is accepted, else the server's own Persian sentence."""
    if not NAME_MIN <= len(name) <= NAME_MAX or not _NAME_OK.match(name):
        return NAME_ERROR
    return None


def normalize_mobile(text: str) -> str | None:
    """«۰۹۱۲ ۳۴۵-۶۷۸۹», «+98 912…», «0098912…» → «09123456789»; None if it isn't a mobile."""
    digits = re.sub(r"[\s\-()]", "", to_ascii_digits(text or ""))
    match = _MOBILE.match(digits)
    return f"0{match.group(1)}" if match else None


def password_rules(password: str) -> list[tuple[str, bool]]:
    """The live checklist under the password box (every rule the server checks)."""
    return [
        ("حداقل ۸ حرف یا رقم", len(password) >= PASSWORD_MIN),
        ("خیلی ساده یا رایج نباشد", bool(password) and password.lower() not in COMMON_PASSWORDS),
        ("حداقل ۴ حرف یا رقم متفاوت", len(set(password)) >= 4),
        ("خیلی طولانی نباشد", len(password.encode()) <= PASSWORD_MAX_BYTES),
    ]


def password_problem(password: str) -> str | None:
    failed = [rule for rule, ok in password_rules(password) if not ok]
    return f"رمز عبور: {failed[0]}." if failed else None
