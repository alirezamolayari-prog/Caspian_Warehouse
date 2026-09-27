from caspian.core.text import normalize, to_ascii_digits, to_persian_digits


def test_arabic_letters_unified():
    assert normalize("كيك") == normalize("کیک") == "کیک"


def test_digits_to_ascii():
    assert normalize("دریل ۱۲۳ و ٤٥٦") == "دریل 123 و 456"
    assert to_ascii_digits("۰۹۱۲") == "0912"


def test_diacritics_zwnj_and_spaces():
    assert normalize("  مِیز‌ها   بزرگ ") == "میز ها بزرگ"


def test_latin_lowercased():
    assert normalize("BOSCH دریل") == "bosch دریل"


def test_empty():
    assert normalize("") == ""


def test_persian_digits():
    assert to_persian_digits(1405) == "۱۴۰۵"
