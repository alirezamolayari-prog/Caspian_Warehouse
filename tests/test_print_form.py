"""The printed document form (A5/A4): phone, unit totals, four signature boxes, footer, logo,
right-to-left labels, and printing through the real paths."""

import datetime as dt
import re
from decimal import Decimal

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from caspian import branding
from caspian.db.models import DocStatus, DocType
from caspian.services.documents import PrintLine, PrintSheet
from caspian.ui import printing
from caspian.ui.document_print import SIGNATURES, document_html, printed_stamp, unit_totals

ISOLATES = "⁦⁧⁨⁩"
WHEN = dt.datetime(2026, 10, 4, 14, 35)  # ۱۴۰۵/۰۷/۱۲
CENTRED_HEAD = r'<th width="\d+%" align="center" valign="middle" dir="rtl">{}</th>'


def _sheet(lines=None, **kw) -> PrintSheet:
    values = dict(id=1, doc_type=DocType.ISSUE, number_text="ح-۹", type_name="حواله خروج",
                  status=DocStatus.POSTED, doc_date=dt.date(2026, 10, 4), warehouse="انبار مرکزی",
                  dest_warehouse="", person="خانم صفا", person_label="تحویل‌گیرنده", description="",
                  lines=lines or [PrintLine("1016", "شمع", "عدد", Decimal(20), None, ""),
                                  PrintLine("2040", "لامپ", "عدد", Decimal(3), None, "")],
                  company="بازار مبلمان کاسپین", created_by="مریم", posted_by="علی", print_count=0,
                  last_printed_by="", person_phone="09121234567")
    values.update(kw)
    return PrintSheet(**values)


def _plain(html_text: str) -> str:
    doc = printing.build_document(html_text)
    return doc.toPlainText().replace("‏", "").replace("\xa0", " ")


# ----- 1. phone -----


def test_phone_shown_under_the_recipient(qapp):
    text = _plain(document_html(_sheet(), 1, printed_at=WHEN))
    assert "شماره تماس: ۰۹۱۲۱۲۳۴۵۶۷" in text
    assert text.index("تحویل‌گیرنده: خانم صفا") < text.index("شماره تماس")


@pytest.mark.parametrize("phone", ["", "   "])
def test_no_phone_line_when_empty(qapp, phone):
    assert "شماره تماس" not in document_html(_sheet(person_phone=phone), 1, printed_at=WHEN)


# ----- 3. totals of «مقدار» -----


def test_one_unit_is_added_up(qapp):
    assert unit_totals(_sheet()) == "۲۳ عدد"
    assert "جمع مقدار: ۲۳ عدد" in _plain(document_html(_sheet(), 1, printed_at=WHEN))


def test_mixed_units_are_never_added_together(qapp):
    sheet = _sheet([PrintLine("1", "شمع", "عدد", Decimal(15), None, ""),
                    PrintLine("2", "لامپ", "کارتن", Decimal(2), None, ""),
                    PrintLine("3", "دستکش", "عدد", Decimal(5), None, "")])
    assert unit_totals(sheet) == "۲۰ عدد، ۲ کارتن"
    text = _plain(document_html(sheet, 1, printed_at=WHEN))
    assert "جمع مقدار: ۲۰ عدد، ۲ کارتن" in text and "۲۲" not in text


# ----- 4/5. table alignment and widths -----


def test_numbers_are_centred_and_the_name_column_is_widest(qapp):
    html_text = document_html(_sheet(), 1, printed_at=WHEN)
    widths = {title: int(w) for w, title in re.findall(r'<th width="(\d+)%"[^>]*>([^<]+)</th>', html_text)}
    assert max(widths, key=widths.get) == "نام کالا" and sum(widths.values()) == 100
    for title in ("ردیف", "کد", "واحد", "مقدار"):
        assert re.search(CENTRED_HEAD.format(title), html_text), title
    assert '<td align="center" valign="middle" dir="rtl">۲۰</td>' in html_text  # «مقدار»
    assert '<td align="right" valign="middle" dir="rtl">شمع</td>' in html_text  # «نام کالا»


def test_priced_columns_are_centred_too(qapp):
    sheet = _sheet([PrintLine("1", "شمع", "عدد", Decimal(2), Decimal(15000), "")])
    html_text = document_html(sheet, 1, printed_at=WHEN)
    for title in ("فی", "مبلغ"):
        assert re.search(CENTRED_HEAD.format(title), html_text), title
    widths = {t: int(w) for w, t in re.findall(r'<th width="(\d+)%"[^>]*>([^<]+)</th>', html_text)}
    assert max(widths, key=widths.get) == "نام کالا" and sum(widths.values()) == 100


# ----- 6. four signature boxes -----


def test_four_equal_signature_boxes_right_to_left(qapp):
    assert SIGNATURES == ("مدیریت", "تحویل‌دهنده", "تحویل‌گیرنده", "انباردار")
    html_text = document_html(_sheet(), 1, printed_at=WHEN)
    boxes = re.findall(r'<td width="25%" align="center" valign="top" dir="rtl">([^<]+)<br>', html_text)
    # Qt lays table columns out left-to-right, so they are emitted reversed (rtl_cells).
    assert boxes == list(reversed(SIGNATURES))


# ----- 7. footer -----


def test_footer_is_one_line_with_the_person_printing_and_the_jalali_date_and_time(qapp):
    assert printed_stamp(WHEN) == "۱۴۰۵/۰۷/۱۲ – ۱۴:۳۵"
    text = _plain(document_html(_sheet(), 1, "A5", "مریم انباردار", WHEN))
    lines = [ln for ln in text.splitlines() if "صادرکننده" in ln]
    assert len(lines) == 1
    assert "صادرکننده: مریم انباردار — تاریخ و ساعت چاپ: ۱۴۰۵/۰۷/۱۲ – ۱۴:۳۵" in lines[0]
    # The creator («مریم») and poster («علی») are not printed; they stay in the database/audit log.
    assert "ثبت نهایی" not in text and "چاپ‌کننده" not in text and "علی" not in text
    assert "صادرکننده: مریم —" not in text


def test_footer_falls_back_to_the_creator_without_a_printing_person(qapp):
    text = _plain(document_html(_sheet(), 1, "A5", "", WHEN))
    assert "صادرکننده: مریم — تاریخ و ساعت چاپ" in text


def test_no_owner_or_approver_name(qapp):
    html_text = document_html(_sheet(), 2, printed_at=WHEN)
    assert "تأییدکننده" not in html_text and "مالک" not in html_text


# ----- 8. right-to-left labels and the copy stamp -----


def test_no_reversed_colons_and_an_intact_copy_stamp(qapp):
    html_text = document_html(_sheet(description="یادداشت", place="پلاک ۶۸ · طبقه همکف",
                                     usage_note="روشنایی"), 2, "A5", "مریم انباردار", WHEN)
    assert not any(ch in html_text for ch in ISOLATES)
    text = _plain(html_text)
    assert "(نسخه ۲)" in text
    for label in ("شماره", "تاریخ", "انبار", "تحویل‌گیرنده", "شماره تماس", "محل مصرف", "صادرکننده",
                  "تعداد اقلام", "جمع مقدار", "تاریخ و ساعت چاپ"):
        assert f"{label}: " in text, label
        assert f":{label}" not in text, label
    doc = printing.build_document(html_text)
    block = doc.begin()
    while block.isValid():
        if block.text().strip():
            assert block.textDirection() == Qt.LayoutDirection.RightToLeft, block.text()
        block = block.next()
    assert 'class="stamp">‏' in html_text and 'dir="rtl" align="center" class="stamp"' in html_text


# ----- 9. logo and branding -----


def test_larger_logo_replaces_the_company_name(qapp):
    image = QImage(str(branding.LOGO_PATH))
    for paper, height in (("A5", 76), ("A4", 96)):
        html_text = document_html(_sheet(), 1, paper, printed_at=WHEN)
        width = int(re.search(rf'<img src="{printing.LOGO_URL}" width="(\d+)" height="{height}">',
                              html_text).group(1))
        assert abs(width - height * image.width() / image.height()) <= 1  # proportions kept
        assert "بازار مبلمان کاسپین" not in html_text  # the logo already shows the name
        assert 'align="left" valign="top">\n<img' in html_text  # top-left corner
    doc = printing.build_document(html_text)
    resource = doc.resource(doc.ResourceType.ImageResource, printing.QUrl(printing.LOGO_URL))
    assert resource is not None and not resource.isNull()


def test_no_logo_file_prints_the_company_name(qapp, monkeypatch):
    monkeypatch.setattr(branding, "LOGO_PATH", branding.LOGO_PATH.with_name("missing.png"))
    html_text = document_html(_sheet(), 1, printed_at=WHEN)
    assert "<img" not in html_text
    assert "<b>بازار مبلمان کاسپین</b>" in html_text


def test_branding_default_and_database_override():
    assert branding.LOGO_PATH.is_file()
    assert branding.company_name(None) == branding.COMPANY_NAME == "بازار مبلمان کاسپین"
    assert branding.company_name("  ") == branding.COMPANY_NAME
    assert branding.company_name("فروشگاه نمونه") == "فروشگاه نمونه"


async def test_logo_on_login_dialog_and_sidebar(qtbot, themes, db, admin, monkeypatch, tmp_path):
    from caspian.core.settings import Settings
    from caspian.db.database import DbConfig
    from caspian.ui.app_context import AppContext
    from caspian.ui.auth_dialogs import LoginDialog
    from caspian.ui.main_window import MainWindow

    login = LoginDialog(db)
    qtbot.addWidget(login)
    assert login.logo.has_logo and login.logo.pixmap() is not None
    monkeypatch.setattr("caspian.core.settings.settings_path", lambda: tmp_path / "settings.json")
    win = MainWindow(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(win)
    assert win._logo.has_logo and win._brand_icon.isHidden()

    monkeypatch.setattr(branding, "LOGO_PATH", branding.LOGO_PATH.with_name("missing.png"))
    plain = MainWindow(AppContext(db, DbConfig(), Settings(), themes, admin))
    qtbot.addWidget(plain)
    assert not plain._logo.has_logo and not plain._brand_icon.isHidden()  # the box icon fallback


# ----- the real print paths -----


def test_pdf_fills_the_page(qapp, tmp_path):
    """«ذخیره PDF» used to put the whole form, tiny, in a corner of the page."""
    target = tmp_path / "slip.pdf"
    printing.save_pdf(document_html(_sheet(), 1, printed_at=WHEN), str(target),
                      printing.QPageSize.PageSizeId.A5)
    data = target.read_bytes()
    assert data.startswith(b"%PDF") and data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") >= 1


def test_printer_pages_are_painted_as_images(qapp):
    """Native Windows printing scrambled right-to-left lines; pages are painted with the screen
    engine instead (page_images), filling the page."""
    pages = printing.page_images(document_html(_sheet(), 1, printed_at=WHEN), 1600, 2300)
    assert len(pages) == 1
    image = pages[0]
    assert image.width() == 1600 and image.dotsPerMeterX() == round(300 / 0.0254)
    ink_rows = [y for y in range(0, image.height(), 10)
                if any(image.pixelColor(x, y).lightness() < 128 for x in range(0, image.width(), 8))]
    assert ink_rows and max(ink_rows) > image.height() * 0.4  # the form isn't squeezed into a corner


def test_print_to_uses_images_for_a_real_printer(qapp, monkeypatch, tmp_path):
    """Prints only into a temporary file (never to a real printer or a «Save as» dialog)."""
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

    info = next((p for p in QPrinterInfo.availablePrinters() if "XPS" in p.printerName()), None)
    if info is None:
        pytest.skip("Microsoft XPS Document Writer is not installed")
    painted = []
    real = printing.page_images

    def spy(html_text, width, height, dpi=printing.RASTER_DPI):
        painted.append((width, height))
        return real(html_text, width, height, dpi)

    monkeypatch.setattr(printing, "page_images", spy)
    target = tmp_path / "slip.xps"
    printer = QPrinter(info, QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.NativeFormat)
    printer.setOutputFileName(str(target))
    printing.print_to(printer, document_html(_sheet(), 1, printed_at=WHEN))
    del printer
    assert painted and painted[0][0] > 1000 and target.exists()
