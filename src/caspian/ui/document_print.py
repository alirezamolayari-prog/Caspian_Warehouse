"""Printed receipt / issue forms (A5 or A4) with preview, PDF export and copy tracking."""

import datetime as dt
import html
import logging
from collections.abc import Callable
from decimal import Decimal

from PySide6.QtGui import QImageReader, QPageSize
from PySide6.QtWidgets import QDialog, QMenu, QWidget

from caspian import APP_DISPLAY_NAME, branding
from caspian.core import jalali
from caspian.core.numbers import format_qty
from caspian.core.text import to_persian_digits
from caspian.db.models import DocStatus
from caspian.services import documents as docs
from caspian.services.documents import PrintSheet
from caspian.services.errors import ServiceError
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.file_dialogs import ask_print, ask_save_path
from caspian.ui.fonts import FONT_FAMILY
from caspian.ui.messages import show_error, show_info
from caspian.ui.printing import LOGO_URL, print_to, rtl_cells, save_pdf
from caspian.ui.tasks import callback

log = logging.getLogger(__name__)

RLM = "‏"  # right-to-left mark (bidi, #16); no isolates: Qt ignores them (QA Part A #2)
PAPERS = {"A5": QPageSize.PageSizeId.A5, "A4": QPageSize.PageSizeId.A4}
# Right to left on paper (QA: «مدیریت» added; no owner or approver name is printed).
SIGNATURES = ("مدیریت", "تحویل‌دهنده", "تحویل‌گیرنده", "انباردار")


def _stamp(sheet: PrintSheet, copy_no: int) -> str:
    if sheet.status == DocStatus.DRAFT:
        return "پیش‌نویس — فاقد اعتبار"
    if sheet.status == DocStatus.CANCELLED:
        return "ابطال شده"
    if copy_no > 1:
        return f"کپی / المثنی (نسخه {to_persian_digits(copy_no)})"
    return ""


def unit_totals(sheet: PrintSheet) -> str:
    """«۲۳ عدد», or one total per unit in first-seen order («۲۰ عدد، ۲ کارتن»): quantities in
    different units are never added together."""
    totals: dict[str, Decimal] = {}
    for ln in sheet.lines:
        totals[ln.unit] = totals.get(ln.unit, Decimal(0)) + ln.qty
    return "، ".join(f"{format_qty(qty)} {unit}" for unit, qty in totals.items())


LOGO_HEIGHTS = {"A5": 76, "A4": 96}  # about 20 / 25 mm on paper; the logo already holds the name


def logo_size(height: int) -> tuple[int, int] | None:
    """(width, height) of the printed logo at `height`, keeping the image's own proportions;
    None when there is no (readable) logo file."""
    path = branding.logo_path()
    if path is None:
        return None
    size = QImageReader(str(path)).size()
    if size.isEmpty():
        return None
    return round(height * size.width() / size.height()), height


def printed_stamp(moment: dt.datetime) -> str:
    """«۱۴۰۵/۰۷/۱۲ – ۱۴:۳۵»: Jalali date and 24-hour time, Persian digits."""
    return f"{jalali.format_date(moment.date())} – {to_persian_digits(moment.strftime('%H:%M'))}"


# Logical (right-to-left) order of the table columns and their widths in %. «نام کالا» is the
# widest; numbers and short codes are centred, names and notes are right-aligned and wrap.
_COLUMNS = (("ردیف", 6, "center"), ("کد", 10, "center"), ("نام کالا", 44, "right"),
            ("واحد", 10, "center"), ("مقدار", 10, "center"), ("توضیح", 20, "right"))
_PRICED_COLUMNS = (("ردیف", 5, "center"), ("کد", 9, "center"), ("نام کالا", 34, "right"),
                   ("واحد", 8, "center"), ("مقدار", 8, "center"), ("فی", 10, "center"),
                   ("مبلغ", 12, "center"), ("توضیح", 14, "right"))


def document_html(sheet: PrintSheet, copy_no: int, paper: str = "A5", printed_by: str = "",
                  printed_at: dt.datetime | None = None) -> str:
    """The form for one copy; `copy_no` > 1 marks it as a duplicate. `printed_by`: the signed-in
    person printing it, shown as «صادرکننده» (the creator and poster stay in the database and
    audit log only). No owner or approver name is ever printed."""
    esc = html.escape
    size = 10 if paper == "A5" else 11  # 8pt was too small on A5 (#16)
    priced = any(ln.unit_price is not None for ln in sheet.lines)
    columns = _PRICED_COLUMNS if priced else _COLUMNS

    def cell(text: str, align: str, tag: str = "td") -> str:
        return f'<{tag} align="{align}" valign="middle" dir="rtl">{esc(text)}</{tag}>'

    heads = rtl_cells([f'<th width="{width}%" align="{"center" if align == "center" else "right"}" '
                       f'valign="middle" dir="rtl">{esc(title)}</th>' for title, width, align in columns])
    rows = []
    for no, ln in enumerate(sheet.lines, start=1):
        values = [to_persian_digits(no), to_persian_digits(ln.code), ln.name, ln.unit, format_qty(ln.qty)]
        if priced:
            values += [format_qty(ln.unit_price) if ln.unit_price is not None else "",
                       format_qty(ln.amount) if ln.amount is not None else ""]
        values.append(ln.notes)
        rows.append("<tr>" + rtl_cells([cell(v, align) for v, (_t, _w, align) in zip(values, columns,
                                                                                      strict=True)])
                    + "</tr>")

    # «برچسب: مقدار» kept in right-to-left order with plain RLM marks. Qt's text engine does not
    # support the Unicode isolates (RLI/PDI) and the font has no glyph for them: with them the
    # colon jumped to the start of the label («:محل مصرف») and spaces were lost (QA Part A #2).
    def field(label: str, value: str) -> str:
        label = esc(label).replace(" ", "&nbsp;")  # a label is never split across lines
        return f"{label}:{RLM}&nbsp;<b>{RLM}{esc(value)}{RLM}</b>"

    def para(content: str, extra: str = "") -> str:  # every block explicitly right-to-left
        return f'<p dir="rtl" align="right"{extra}>{content}</p>'

    sep = f"{RLM} — {RLM}"
    parties = [field("انبار", sheet.warehouse)]
    if sheet.dest_warehouse:
        parties = [field("از انبار", sheet.warehouse), field("به انبار", sheet.dest_warehouse)]
    if sheet.person:
        parties.append(field(sheet.person_label, sheet.person))
    info = [para(sep.join(parties))]
    if sheet.person and sheet.person_phone.strip():
        info.append(para(field("شماره تماس", to_persian_digits(sheet.person_phone.strip()))))
    if sheet.place or sheet.usage_note:  # issue vouchers: where the goods are consumed
        place = [field("محل مصرف", sheet.place)] if sheet.place else []
        if sheet.usage_note:
            place.append(field("شرح مصرف", sheet.usage_note))
        info.append(para(sep.join(place)))
    if sheet.description:
        info.append(para(field("توضیحات", sheet.description)))

    summary = [field("تعداد اقلام", to_persian_digits(len(sheet.lines)))]
    if sheet.lines:
        summary.append(field("جمع مقدار", unit_totals(sheet)))
    if sheet.total_amount is not None:
        summary.append(field("جمع مبلغ", f"{format_qty(sheet.total_amount)} ریال"))

    stamp = _stamp(sheet, copy_no)
    # RLM around the stamp keeps «(نسخه ۲)» intact in the centred line (it printed «(نسخه۲(»).
    stamp_html = (f'<p dir="rtl" align="center" class="stamp">{RLM}{esc(stamp)}{RLM}</p>'
                  if stamp else "")
    # One line: who issues the slip (the person printing it) and when.
    footer = sep.join([field("صادرکننده", printed_by or sheet.created_by),
                       field("تاریخ و ساعت چاپ", printed_stamp(printed_at or dt.datetime.now()))])

    doc_date = to_persian_digits(jalali.format_date(sheet.doc_date))
    signature_cells = rtl_cells([f'<td width="25%" align="center" valign="top" dir="rtl">{esc(s)}'
                                 f"<br><br><br><br></td>" for s in SIGNATURES])
    # The logo already contains the company name, so it replaces the text; the name is printed
    # only when there is no logo file.
    logo = logo_size(LOGO_HEIGHTS[paper])
    brand = (f'<img src="{LOGO_URL}" width="{logo[0]}" height="{logo[1]}">' if logo
             else f"<b>{esc(sheet.company or APP_DISPLAY_NAME)}</b>")
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
<td width="45%" style="border:none; white-space: nowrap;" align="left" valign="top">
{brand}</td>
<td width="55%" style="border:none" dir="rtl" align="right" valign="middle"><h1>{esc(sheet.type_name)}</h1>
{field("شماره", sheet.number_text)}{sep}{field("تاریخ", doc_date)}</td>
</tr></table>
{stamp_html}
{"".join(info)}
<p dir="rtl" align="right" style="font-size: {size}pt;">&nbsp;</p>
<table class="grid" width="100%" cellspacing="0" cellpadding="4"><thead><tr>
{heads}</tr></thead>
<tbody>{"".join(rows)}</tbody></table>
{para(sep.join(summary))}
<table width="100%" cellspacing="0" cellpadding="6" border="1"><tr>{signature_cells}</tr></table>
{para(footer, ' class="muted"')}
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
    dialog.paintRequested.connect(lambda p: print_to(p, html_text))  # preview = what prints
    dialog.resize(900, 760)
    return await exec_dialog(dialog) == QDialog.DialogCode.Accepted


LINKED_POST_QUESTION = ("«{title}» از درخواست تأییدشده ساخته شده است. با چاپ، حواله ثبت نهایی می‌شود و "
                        "کالاها از موجودی کم می‌شوند (فقط با ابطال قابل برگشت است). ادامه می‌دهید؟")
DRAFT_WATERMARK = "پیش‌نویس"
PRINT_FAILED = ("حواله ثبت نهایی شد ولی چاپ نشد (چاپگر را بررسی کنید). از «چاپ» دوباره چاپ کنید؛ "
                "این نسخه هنوز اصل حساب می‌شود.")
PREVIEW_DPI = 110


class DraftPreviewDialog(QDialog):
    """See a slip before it is final: pages with a big «پیش‌نویس» across them, and only «بستن»
    (nothing can be printed or finalised from here)."""

    def __init__(self, html_text: str, paper: str, parent: QWidget | None = None) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout

        from caspian.ui.printing import page_images

        super().__init__(parent)
        self.setWindowTitle("‏پیش‌نمایش (پیش‌نویس — هنوز ثبت نشده)")
        size = QPageSize(PAPERS[paper]).sizePixels(PREVIEW_DPI)
        self.pages = page_images(html_text, size.width(), size.height(), PREVIEW_DPI, DRAFT_WATERMARK)
        layout = QVBoxLayout(self)
        note = QLabel("این فقط پیش‌نمایش است و حواله ثبت نهایی نشده. برای ثبت نهایی و چاپ، «چاپ» را "
                      "بزنید.", objectName="Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        pages = QVBoxLayout(holder)
        for image in self.pages:
            label = QLabel()
            label.setPixmap(QPixmap.fromImage(image))
            pages.addWidget(label, 0, Qt.AlignmentFlag.AlignHCenter)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("بستن")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.resize(size.width() + 80, min(size.height() + 140, 900))


def _linked_draft(sheet: PrintSheet) -> bool:
    return bool(sheet.linked_request and sheet.status == DocStatus.DRAFT)


async def _agree_to_post(parent: QWidget, ctx: AppContext, sheet: PrintSheet) -> bool:
    from caspian.core.permissions import Perm
    from caspian.ui.messages import confirm

    if not ctx.actor.can(Perm.DOCUMENTS_POST):
        show_error(parent, "چاپ این حواله آن را ثبت نهایی می‌کند و مجوز «ثبت نهایی سند» لازم است.")
        return False
    return await confirm(parent, LINKED_POST_QUESTION.format(title=f"{sheet.type_name} {sheet.number_text}"),
                         "ثبت نهایی و چاپ")


async def _post_linked(parent: QWidget, ctx: AppContext, doc_id: int) -> PrintSheet:
    """Post a slip made from an approved request — only once the printer or PDF file was chosen."""
    from caspian.ui.documents_page import with_stocktake_override  # that module imports this one

    await with_stocktake_override(ctx, parent, lambda approval: docs.post_document(
        ctx.db, ctx.actor, doc_id, approval=approval, via="goods_request"))
    if ctx.server_link is not None:
        ctx.server_link.wake()  # tell the server «issued» now if it is reachable
    return await docs.print_sheet(ctx.db, ctx.actor, doc_id)


async def preview_document(parent: QWidget, ctx: AppContext, doc_id: int, paper: str = "A5") -> bool:
    """«پیش‌نمایش (بدون ثبت)»: a slip of an approved request is shown with «پیش‌نویس» across it and
    is not finalised; any other document opens the usual preview."""
    try:
        sheet = await docs.print_sheet(ctx.db, ctx.actor, doc_id)  # refuses it before approval
        if not _linked_draft(sheet):
            return await print_document(parent, ctx, doc_id, paper)
        html_text = document_html(sheet, sheet.print_count + 1, paper, ctx.actor.display_name)
    except ServiceError as exc:
        show_error(parent, exc.message)
        return False
    await exec_dialog(DraftPreviewDialog(html_text, paper, parent))
    return False


async def print_document(parent: QWidget, ctx: AppContext, doc_id: int, paper: str = "A5") -> bool:
    """Print. A slip of an approved request becomes final only after the print dialog was accepted
    (cancelled = nothing changes); it is then printed with its final number."""
    try:
        sheet = await docs.print_sheet(ctx.db, ctx.actor, doc_id)  # refuses it before approval
        if _linked_draft(sheet):
            return await _print_linked(parent, ctx, doc_id, sheet, paper)
        html_text = document_html(sheet, sheet.print_count + 1, paper, ctx.actor.display_name)
        if not await preview_and_print(parent, html_text, paper):
            return False
        if sheet.trackable:
            await docs.record_print(ctx.db, ctx.actor, doc_id, "printer")
    except ServiceError as exc:
        show_error(parent, exc.message)
        return False
    return True


async def _print_linked(parent: QWidget, ctx: AppContext, doc_id: int, sheet: PrintSheet,
                        paper: str) -> bool:
    if not await _agree_to_post(parent, ctx, sheet):
        return False
    printer = _printer(paper)
    if not await ask_print(printer, parent):
        return False  # no printer chosen: the slip stays a draft
    sheet = await _post_linked(parent, ctx, doc_id)
    try:
        print_to(printer, document_html(sheet, sheet.print_count + 1, paper, ctx.actor.display_name))
    except Exception:
        log.exception("Printing a just-posted slip failed")
        show_error(parent, PRINT_FAILED)
        return True  # posted: the caller refreshes; the next print is still the original
    if sheet.trackable:
        await docs.record_print(ctx.db, ctx.actor, doc_id, "printer")
    return True


async def pdf_document(parent: QWidget, ctx: AppContext, doc_id: int, paper: str = "A5") -> bool:
    try:
        sheet = await docs.print_sheet(ctx.db, ctx.actor, doc_id)
        linked = _linked_draft(sheet)
        if linked and not await _agree_to_post(parent, ctx, sheet):
            return False
        path = await ask_save_path(parent, "ذخیره PDF", ctx.settings,
                                   f"{sheet.type_name} {sheet.number_text}.pdf", "PDF (*.pdf)")
        if not path:
            return False  # no file chosen: a slip stays a draft
        if linked:
            sheet = await _post_linked(parent, ctx, doc_id)
        save_pdf(document_html(sheet, sheet.print_count + 1, paper, ctx.actor.display_name), path,
                 PAPERS[paper])
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

    menu.addAction("پیش‌نمایش (بدون ثبت)", action(preview_document, "A5"))
    menu.addAction("چاپ (A5)", action(print_document, "A5"))
    menu.addAction("چاپ (A4)", action(print_document, "A4"))
    menu.addAction("ذخیره PDF (A5)", action(pdf_document, "A5"))
    return menu
