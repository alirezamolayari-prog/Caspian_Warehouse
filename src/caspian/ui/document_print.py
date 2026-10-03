"""Printed receipt / issue forms (A5 or A4) with preview, PDF export and copy tracking."""

import datetime as dt
import html
from collections.abc import Callable

from PySide6.QtGui import QPageSize
from PySide6.QtWidgets import QDialog, QMenu, QWidget

from caspian import APP_DISPLAY_NAME
from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.text import to_persian_digits
from caspian.db.models import DocStatus
from caspian.services import documents as docs
from caspian.services.documents import PrintSheet
from caspian.services.errors import ServiceError
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.file_dialogs import ask_save_path
from caspian.ui.fonts import FONT_FAMILY
from caspian.ui.messages import show_error, show_info
from caspian.ui.printing import build_document, rtl_cells, save_pdf
from caspian.ui.tasks import callback

RLM, RLI, PDI = "‏", "⁧", "⁩"  # right-to-left mark / isolate (bidi, #16)
PAPERS = {"A5": QPageSize.PageSizeId.A5, "A4": QPageSize.PageSizeId.A4}
SIGNATURES = ("تحویل‌دهنده", "تحویل‌گیرنده", "انباردار")


def _stamp(sheet: PrintSheet, copy_no: int) -> str:
    if sheet.status == DocStatus.DRAFT:
        return "پیش‌نویس — فاقد اعتبار"
    if sheet.status == DocStatus.CANCELLED:
        return "ابطال شده"
    if copy_no > 1:
        return f"کپی / المثنی (نسخه {to_persian_digits(copy_no)})"
    return ""


def document_html(sheet: PrintSheet, copy_no: int, paper: str = "A5") -> str:
    """The form for one copy; `copy_no` > 1 marks it as a duplicate."""
    esc = html.escape
    size = 10 if paper == "A5" else 11  # 8pt was too small on A5 (#16)
    priced = any(ln.unit_price is not None for ln in sheet.lines)
    heads = ["ردیف", "کد", "نام کالا", "واحد", "مقدار"] + (["فی", "مبلغ"] if priced else []) + ["توضیح"]
    rows = []
    for no, ln in enumerate(sheet.lines, start=1):
        cells = [to_persian_digits(no), to_persian_digits(ln.code), ln.name, ln.unit, format_qty(ln.qty)]
        if priced:
            cells += [format_qty(ln.unit_price) if ln.unit_price is not None else "",
                      format_qty(ln.amount) if ln.amount is not None else ""]
        cells.append(ln.notes)
        rows.append("<tr>" + rtl_cells([f"<td>{esc(c)}</td>" for c in cells]) + "</tr>")
    # Label and value in separate RTL-isolated pieces so colons and numbers stay in place (#16).
    def field(label: str, value: str) -> str:
        return f"{RLI}{esc(label)}:{PDI}{RLM} {RLI}<b>{esc(value)}</b>{PDI}"

    parties = [field("انبار", sheet.warehouse)]
    if sheet.dest_warehouse:
        parties = [field("از انبار", sheet.warehouse), field("به انبار", sheet.dest_warehouse)]
    if sheet.person:
        parties.append(field(sheet.person_label, sheet.person))
    summary = [field("تعداد اقلام", to_persian_digits(len(sheet.lines)))]
    if sheet.total_amount is not None:
        summary.append(field("جمع مبلغ", f"{format_qty(sheet.total_amount)} ریال"))
    stamp = _stamp(sheet, copy_no)
    stamp_html = f'<p align="center" class="stamp">{esc(stamp)}</p>' if stamp else ""
    description = f"<p>{field('توضیحات', sheet.description)}</p>" if sheet.description else ""
    printed = to_persian_digits(jalali.format_date(dt.date.today()))
    footer = [field("صادرکننده", sheet.created_by)]
    if sheet.posted_by:
        footer.append(field("ثبت نهایی", sheet.posted_by))
    footer.append(field("تاریخ چاپ", printed))
    sep = f"{RLM} — {RLM}"
    doc_date = to_persian_digits(jalali.format_date(sheet.doc_date))
    signature_cells = rtl_cells([f'<td width="33%" align="center">{s}<br><br><br><br></td>'
                                 for s in SIGNATURES])
    return f"""
<html dir="rtl"><head><style>
body {{ font-family: '{FONT_FAMILY}'; font-size: {size}pt; }}
h1 {{ font-size: {size + 6}pt; margin: 0; }}
.stamp {{ color: #b00020; font-size: {size + 4}pt; font-weight: bold; }}
.grid {{ border-collapse: collapse; width: 100%; }}
.grid th {{ background: #e8eef0; border: 1px solid #555; padding: 4px; font-weight: bold; }}
.grid td {{ border: 1px solid #555; padding: 5px 4px; }}
.muted {{ color: #444; }}
</style></head><body>
<table width="100%" style="border:none"><tr>
<td width="40%" style="border:none" align="left"><b>{esc(sheet.company or APP_DISPLAY_NAME)}</b><br>
<span class="muted">{esc(APP_DISPLAY_NAME)}</span></td>
<td width="60%" style="border:none"><h1>{esc(sheet.type_name)}</h1>
{field("شماره", sheet.number_text)}{sep}{field("تاریخ", doc_date)}</td>
</tr></table>
{stamp_html}
<p>{sep.join(parties)}</p>
{description}
<table class="grid" width="100%" cellspacing="0" cellpadding="4"><thead><tr>
{rtl_cells([f"<th>{h}</th>" for h in heads])}</tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<p>{sep.join(summary)}</p>
<table width="100%" cellspacing="0" cellpadding="6" border="1"><tr>{signature_cells}</tr></table>
<p class="muted">{sep.join(footer)}</p>
</body></html>"""


def _printer(paper: str):
    from PySide6.QtPrintSupport import QPrinter

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setPageSize(QPageSize(PAPERS[paper]))
    return printer


async def preview_and_print(parent: QWidget | None, html_text: str, paper: str) -> bool:
    """Print preview; True if the user printed from it."""
    from PySide6.QtPrintSupport import QPrintPreviewDialog

    printer = _printer(paper)
    dialog = QPrintPreviewDialog(printer, parent)
    dialog.setWindowTitle("پیش‌نمایش چاپ")
    dialog.paintRequested.connect(lambda p: build_document(html_text).print_(p))
    dialog.resize(900, 760)
    return await exec_dialog(dialog) == QDialog.DialogCode.Accepted


async def print_document(parent: QWidget, ctx: AppContext, doc_id: int, paper: str = "A5") -> bool:
    try:
        sheet = await docs.print_sheet(ctx.db, ctx.actor, doc_id)
        if not await preview_and_print(parent, document_html(sheet, sheet.print_count + 1, paper), paper):
            return False
        if sheet.trackable:
            await docs.record_print(ctx.db, ctx.actor, doc_id, "printer")
    except ServiceError as exc:
        show_error(parent, exc.message)
        return False
    return True


async def pdf_document(parent: QWidget, ctx: AppContext, doc_id: int, paper: str = "A5") -> bool:
    try:
        sheet = await docs.print_sheet(ctx.db, ctx.actor, doc_id)
        path = await ask_save_path(parent, "ذخیره PDF", ctx.settings,
                                   f"{sheet.type_name} {sheet.number_text}.pdf", "PDF (*.pdf)")
        if not path:
            return False
        save_pdf(document_html(sheet, sheet.print_count + 1, paper), path, PAPERS[paper])
        if sheet.trackable:
            await docs.record_print(ctx.db, ctx.actor, doc_id, "pdf")
    except ServiceError as exc:
        show_error(parent, exc.message)
        return False
    show_info(parent, "فایل PDF ذخیره شد.")
    return True


def document_print_menu(parent: QWidget, ctx: AppContext, current_id: Callable[[], int | None],
                        after: Callable[[], None] | None = None) -> QMenu:
    """«چاپ / پیش‌نمایش» menu for the selected/open document; `after` runs once a copy was issued."""
    menu = QMenu(parent)

    def action(fn, paper: str):
        async def run() -> None:
            doc_id = current_id()
            if doc_id is not None and await fn(parent, ctx, doc_id, paper) and after:
                after()
        return callback(run)

    menu.addAction("پیش‌نمایش و چاپ (A5)", action(print_document, "A5"))
    menu.addAction("پیش‌نمایش و چاپ (A4)", action(print_document, "A4"))
    menu.addAction("ذخیره PDF (A5)", action(pdf_document, "A5"))
    return menu
