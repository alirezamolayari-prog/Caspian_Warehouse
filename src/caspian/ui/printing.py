"""Printable reports: RTL HTML rendered by QTextDocument to PDF or a printer."""

import datetime as dt
import html
from collections.abc import Sequence

from PySide6.QtCore import QMarginsF, QSizeF, Qt
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPdfWriter, QTextDocument, QTextOption
from PySide6.QtWidgets import QFileDialog, QMenu, QWidget

from caspian import APP_DISPLAY_NAME
from caspian.core import jalali
from caspian.core.text import to_persian_digits
from caspian.ui.fonts import FONT_FAMILY
from caspian.ui.messages import show_info
from caspian.ui.tasks import callback


def report_html(title: str, meta: Sequence[str], headers: Sequence[str],
                rows: Sequence[Sequence[str]], widths: Sequence[int] | None = None,
                footer: str = "", blank_columns: Sequence[int] = ()) -> str:
    """A simple bordered table report. `blank_columns` are left empty for handwriting."""
    esc = html.escape
    head = "".join(
        f'<th width="{widths[i]}%">{esc(h)}</th>' if widths else f"<th>{esc(h)}</th>"
        for i, h in enumerate(headers))
    body = []
    for row in rows:
        cells = []
        for i, value in enumerate(row):
            text = "&nbsp;" if i in blank_columns else esc(value)
            cells.append(f"<td>{text}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
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
<td width="72%" style="border:none"><h1>{esc(title)}</h1><div class="meta">{meta_html}</div></td>
<td width="28%" style="border:none" align="left">{esc(APP_DISPLAY_NAME)}<br>تاریخ چاپ: {printed}</td>
</tr></table><br>
<table cellspacing="0" cellpadding="4"><thead><tr>{head}</tr></thead>
<tbody>{''.join(body)}</tbody></table>
<div class="footer">{footer}</div>
</body></html>"""


def build_document(html_text: str) -> QTextDocument:
    doc = QTextDocument()
    font = QFont(FONT_FAMILY)
    font.setPointSizeF(9)
    doc.setDefaultFont(font)
    option = QTextOption()
    option.setTextDirection(Qt.LayoutDirection.RightToLeft)
    doc.setDefaultTextOption(option)
    doc.setHtml(html_text)
    return doc


def save_pdf(html_text: str, path: str) -> None:
    writer = QPdfWriter(path)
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4),
                                     QPageLayout.Orientation.Portrait, QMarginsF(12, 12, 12, 12),
                                     QPageLayout.Unit.Millimeter))
    writer.setResolution(300)
    writer.setTitle(APP_DISPLAY_NAME)
    doc = build_document(html_text)
    doc.setPageSize(QSizeF(writer.pageLayout().paintRectPixels(300).size()))
    doc.print_(writer)


def print_html(html_text: str, parent: QWidget | None = None) -> bool:
    """Show the system print dialog. Returns False if cancelled."""
    from PySide6.QtPrintSupport import QPrintDialog, QPrinter

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    dialog = QPrintDialog(printer, parent)
    if dialog.exec() != QPrintDialog.DialogCode.Accepted:
        return False
    build_document(html_text).print_(printer)
    return True


async def export_pdf(parent: QWidget, html_text: str, default_name: str) -> None:
    path, _ = QFileDialog.getSaveFileName(parent, "ذخیره PDF", default_name, "PDF (*.pdf)")
    if path:
        save_pdf(html_text, path)
        show_info(parent, "فایل PDF ذخیره شد.")


def output_menu(parent: QWidget, make_html, default_name) -> QMenu:
    """Print / Save-PDF menu. make_html is an async callable returning the HTML."""
    menu = QMenu(parent)

    async def do_print() -> None:
        print_html(await make_html(), parent)

    async def do_pdf() -> None:
        await export_pdf(parent, await make_html(), default_name())

    # Closures can't be @asyncSlot (see caspian.ui.tasks).
    menu.addAction("چاپ…", callback(do_print))
    menu.addAction("ذخیره PDF…", callback(do_pdf))
    return menu
