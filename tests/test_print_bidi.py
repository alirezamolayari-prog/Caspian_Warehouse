"""QA Part A #2: the printed voucher showed «پلاک۶۸همکف» and colons at the start of lines
(«:محل مصرف»). Cause: Unicode isolates (RLI/PDI) around every «label: value». Qt's text engine does
not support them and the bundled font has no glyph for them, so the Windows print engine drew them
with a fallback font as separate runs. The template now uses only characters the font has."""

import datetime as dt
from decimal import Decimal

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QRawFont

from caspian.db.models import DocStatus, DocType
from caspian.resources import FONTS_DIR
from caspian.services.documents import PrintLine, PrintSheet
from caspian.ui import printing
from caspian.ui.document_print import document_html

ISOLATES = "⁦⁧⁨⁩‪‫‬‭‮"


def _sheet(**kw) -> PrintSheet:
    values = dict(id=1, doc_type=DocType.ISSUE, number_text="ح-۹", type_name="حواله خروج",
                  status=DocStatus.POSTED, doc_date=dt.date(2026, 10, 11), warehouse="انبار مرکزی",
                  dest_warehouse="", person="خانم صفا", person_label="تحویل‌گیرنده", description="",
                  lines=[PrintLine("1016", "شمع", "عدد", Decimal(1), None, "")], company="",
                  created_by="admin", posted_by="admin", print_count=0, last_printed_by="",
                  place="پلاک ۶۸ · طبقه همکف", usage_note="روشنایی پله")
    values.update(kw)
    return PrintSheet(**values)


@pytest.mark.parametrize("paper", ["A5", "A4"])
def test_no_bidi_isolates_in_the_printed_html(paper):
    html_text = document_html(_sheet(description="یادداشت"), 2, paper)
    assert not any(ch in html_text for ch in ISOLATES)


def test_every_printed_character_exists_in_the_bundled_font(qapp):
    doc = printing.build_document(document_html(_sheet(description="یادداشت"), 1))
    text = doc.toPlainText()
    for weight in ("Regular", "Bold"):
        font = QRawFont(str(FONTS_DIR / f"Vazirmatn-UI-{weight}.ttf"), 11)
        missing = {ch for ch in text if not ch.isspace() and not font.supportsCharacter(ord(ch))}
        assert not missing, (weight, [hex(ord(c)) for c in missing])


def test_place_line_keeps_spaces_dot_and_label_order(qapp):
    doc = printing.build_document(document_html(_sheet(), 1))
    plain = doc.toPlainText().replace("‏", "").replace("\xa0", " ")
    assert "محل مصرف: پلاک ۶۸ · طبقه همکف" in plain
    assert "شرح مصرف: روشنایی پله" in plain and "انبار: انبار مرکزی" in plain
    assert "تحویل‌گیرنده: خانم صفا" in plain


def test_every_paragraph_is_right_to_left(qapp):
    doc = printing.build_document(document_html(_sheet(description="یادداشت"), 1))
    block = doc.begin()
    while block.isValid():
        if ":" in block.text():
            assert block.textDirection() == Qt.LayoutDirection.RightToLeft, block.text()
        block = block.next()
