"""Printable reports: RTL HTML rendered by QTextDocument to PDF or a printer."""

import datetime as dt
import html
from collections.abc import Sequence

from PySide6.QtCore import QMarginsF, QRectF, QSizeF, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import QMenu, QWidget

from caspian import APP_DISPLAY_NAME, branding
from caspian.core import jalali
from caspian.core.settings import Settings
from caspian.core.text import to_persian_digits
from caspian.ui.file_dialogs import ask_print, ask_save_path
from caspian.ui.fonts import FONT_FAMILY
from caspian.ui.messages import show_info
from caspian.ui.tasks import callback


def rtl_cells(cells: Sequence[str]) -> str:
    """Join table cells for printing. QTextDocument lays table columns out left-to-right even
    in an RTL document, so the first (logical) column must be emitted last to end up on the
    right. test_printing_ui.test_qt_tables_are_still_left_to_right tells when Qt changes this."""
    return "".join(reversed(cells))


def report_html(title: str, meta: Sequence[str], headers: Sequence[str],
                rows: Sequence[Sequence[str]], widths: Sequence[int] | None = None,
                footer: str = "", blank_columns: Sequence[int] = ()) -> str:
    """A simple bordered table report. `blank_columns` are left empty for handwriting."""
    esc = html.escape
    head = rtl_cells([
        f'<th width="{widths[i]}%">{esc(h)}</th>' if widths else f"<th>{esc(h)}</th>"
        for i, h in enumerate(headers)])
    body = []
    for row in rows:
        cells = []
        for i, value in enumerate(row):
            text = "&nbsp;" if i in blank_columns else esc(value)
            cells.append(f"<td>{text}</td>")
        body.append(f"<tr>{rtl_cells(cells)}</tr>")
    printed = to_persian_digits(jalali.format_date(dt.date.today()))
    meta_html = "<br>".join(esc(m) for m in meta)
    return f"""
<html dir="rtl"><head><style>
body {{ font-family: '{FONT_FAMILY}'; font-size: 9pt; }}
h1 {{ font-size: 15pt; margin: 0; }}
.meta {{ color: #444; }}
table {{ border-collapse: collapse; width: 100%; }}
th {{ background: #e8eef0; border: 1px solid #555; padding: 5px; font-weight: bold; }}
td {{ border: 1px solid #555; padding: 7px 5px; }}
.footer {{ margin-top: 18px; }}
</style></head><body>
<table width="100%" style="border:none"><tr>
<td width="28%" style="border:none" align="left">{esc(APP_DISPLAY_NAME)}<br>تاریخ چاپ: {printed}</td>
<td width="72%" style="border:none"><h1>{esc(title)}</h1><div class="meta">{meta_html}</div></td>
</tr></table><br>
<table width="100%" cellspacing="0" cellpadding="4"><thead><tr>{head}</tr></thead>
<tbody>{''.join(body)}</tbody></table>
<div class="footer">{footer}</div>
</body></html>"""


LOGO_URL = "caspian-branding:logo"  # <img src=…> in printed forms; resolved by build_document


def build_document(html_text: str) -> QTextDocument:
    doc = QTextDocument()
    font = QFont(FONT_FAMILY)
    font.setPointSizeF(9)
    doc.setDefaultFont(font)
    option = QTextOption()
    option.setTextDirection(Qt.LayoutDirection.RightToLeft)
    doc.setDefaultTextOption(option)
    if LOGO_URL in html_text and (logo := branding.logo_path()) is not None:
        doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(LOGO_URL), QImage(str(logo)))
    doc.setHtml(html_text)
    return doc


RASTER_DPI = 300  # printer pages are painted as images at this resolution


def _lay_out(doc: QTextDocument, device, size: QSizeF) -> QTextDocument:
    """Lay the document out for `device` (its resolution, so point sizes are right) on a page of
    `size` device pixels. Without this, Qt used screen units: tiny PDFs or wide default margins."""
    doc.documentLayout().setPaintDevice(device)
    doc.setPageSize(size)
    return doc


def save_pdf(html_text: str, path: str,
             page: QPageSize.PageSizeId = QPageSize.PageSizeId.A4) -> None:
    writer = QPdfWriter(path)
    writer.setPageLayout(QPageLayout(QPageSize(page),
                                     QPageLayout.Orientation.Portrait, QMarginsF(12, 12, 12, 12),
                                     QPageLayout.Unit.Millimeter))
    writer.setResolution(300)
    writer.setTitle(APP_DISPLAY_NAME)
    size = QSizeF(writer.pageLayout().paintRectPixels(writer.resolution()).size())
    _lay_out(build_document(html_text), writer, size).print_(writer)


def paint_watermark(painter: QPainter, width: float, height: float, text: str) -> None:
    """A big diagonal, half-transparent word across the page (e.g. «پیش‌نویس» on a draft)."""
    painter.save()
    font = QFont(FONT_FAMILY)
    font.setBold(True)
    font.setPixelSize(max(12, round(min(width, height) / 4.5)))
    painter.setFont(font)
    painter.setPen(QColor(200, 30, 30, 70))
    painter.translate(width / 2, height / 2)
    painter.rotate(-35)
    box = QRectF(-width, -height / 4, 2 * width, height / 2)
    painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)
    painter.restore()


def page_images(html_text: str, width: int, height: int, dpi: int = RASTER_DPI,
                watermark: str = "") -> list[QImage]:
    """Every page of the document painted onto an image: the same text engine as the screen, which
    keeps right-to-left lines intact. `watermark`: a diagonal word over every page."""
    dots = round(dpi / 0.0254)

    def blank() -> QImage:
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.setDotsPerMeterX(dots)
        image.setDotsPerMeterY(dots)
        image.fill(Qt.GlobalColor.white)
        return image

    layout_device = blank()  # kept alive while the layout refers to it
    doc = _lay_out(build_document(html_text), layout_device, QSizeF(width, height))
    images = []
    for number in range(doc.pageCount()):
        image = blank()
        painter = QPainter(image)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
                               | QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(0, -number * height)
        doc.drawContents(painter, QRectF(0, number * height, width, height))
        if watermark:
            painter.translate(0, number * height)
            paint_watermark(painter, width, height, watermark)
        painter.end()
        images.append(image)
    return images


def print_to(printer, html_text: str) -> None:
    """Print on `printer` (a QPrinter, also the one a print preview paints on).

    Qt's native Windows print engine draws right-to-left text in separate runs and drops spaces
    or swaps weak characters at their edges («(نسخه۲(», «انبار:انبار مرکزی»). Pages for a real
    printer are therefore painted as images first; PDF output stays vector."""
    from PySide6.QtPrintSupport import QPrinter

    rect = printer.pageLayout().paintRectPixels(printer.resolution())
    if printer.outputFormat() == QPrinter.OutputFormat.PdfFormat:
        _lay_out(build_document(html_text), printer, QSizeF(rect.size())).print_(printer)
        return
    scale = RASTER_DPI / printer.resolution()
    images = page_images(html_text, round(rect.width() * scale), round(rect.height() * scale))
    painter = QPainter(printer)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    for number, image in enumerate(images):
        if number:
            printer.newPage()
        painter.drawImage(QRectF(0, 0, rect.width(), rect.height()), image)
    painter.end()


async def print_html(html_text: str, parent: QWidget | None = None) -> bool:
    """Show the system print dialog. Returns False if cancelled."""
    from PySide6.QtPrintSupport import QPrinter

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    if not await ask_print(printer, parent):
        return False
    print_to(printer, html_text)
    return True


async def export_pdf(parent: QWidget, html_text: str, default_name: str,
                     settings: Settings) -> str | None:
    path = await ask_save_path(parent, "ذخیره PDF", settings, default_name, "PDF (*.pdf)")
    if path:
        save_pdf(html_text, path)
        show_info(parent, "فایل PDF ذخیره شد.")
    return path


def output_menu(parent: QWidget, make_html, default_name, settings: Settings) -> QMenu:
    """Print / Save-PDF menu. make_html is an async callable returning the HTML."""
    menu = QMenu(parent)

    async def do_print() -> None:
        await print_html(await make_html(), parent)

    async def do_pdf() -> None:
        await export_pdf(parent, await make_html(), default_name(), settings)

    # Closures can't be @asyncSlot (see caspian.ui.tasks).
    menu.addAction("چاپ…", callback(do_print))
    menu.addAction("ذخیره PDF…", callback(do_pdf))
    return menu
