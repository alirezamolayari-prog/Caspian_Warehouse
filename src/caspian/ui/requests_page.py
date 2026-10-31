"""«درخواست کالا»: issue / loan-out slips sent to the owners through Caspian Server, their status
and chat.

There is one way to ask for goods: the slip itself («ارسال درخواست» in the document editor). This page
only lists the requests — by default the ones still waiting or rejected — opens their slip (to fix and
resend a rejected one) and holds the chat with the owners. Sending happens in the background
(`ui.server_status.ServerLink`), so nothing waits for the network.
"""

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core import jalali
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.db.models import DocStatus, RequestStatus
from caspian.services.errors import ServiceError
from caspian.services.server_link import goods
from caspian.services.server_link.goods import STATUS_NAMES, RequestRow
from caspian.services.server_link.sync import SyncState
from caspian.ui.app_context import AppContext, exec_dialog
from caspian.ui.document_print import document_print_menu
from caspian.ui.messages import show_error
from caspian.ui.widgets import Card, DataTable, FilterChip

SAVED_HERE = ("درخواست‌ها روی همین رایانه ذخیره می‌شوند و بعداً خودکار ارسال می‌شوند؛ "
              "کارهای انبار (رسید، حواله، چاپ، پشتیبان) مثل همیشه انجام می‌شود.")
OFFLINE_TEXT = {
    SyncState.NOT_CONFIGURED: "اتصال به سرور کاسپین هنوز تنظیم نشده است (تنظیمات ← اتصال به سرور). "
                              + SAVED_HERE,
    SyncState.DISCONNECTED: "سرور کاسپین الان در دسترس نیست. " + SAVED_HERE,
    SyncState.ACCOUNT_PROBLEM: "سرور حساب انبار را نپذیرفت. " + SAVED_HERE,
}
COLUMNS = ("شماره", "درخواست‌کننده", "ثبت‌کننده", "محل مصرف", "اقلام", "وضعیت", "وضعیت ارسال", "تاریخ",
           "توضیحات")
STATUS_COL, SEND_COL = 5, 6
OPEN = "open"  # the default filter: waiting or rejected


class RequestChatDialog(QDialog):
    """One request: its details and the conversation with the owners (newest at the bottom)."""

    def __init__(self, ctx: AppContext, row: RequestRow, parent=None) -> None:
        super().__init__(parent)
        self._ctx, self._row = ctx, row
        self.setWindowTitle(f"‏گفتگو — {row.title}")
        self.setMinimumSize(620, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        self.heading = QLabel(row.title, objectName="PageTitle")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.details = QLabel(objectName="Muted")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.transcript = QTextBrowser(objectName="Transcript")
        self.transcript.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout.addWidget(self.transcript, 1)
        row_layout = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("پیام به مالکان…")
        self.input.returnPressed.connect(self.on_send)
        row_layout.addWidget(self.input, 1)
        self.send_button = QPushButton("ارسال")
        self.send_button.setProperty("variant", "primary")
        self.send_button.clicked.connect(self.on_send)
        row_layout.addWidget(self.send_button)
        close = QPushButton("بستن")
        close.clicked.connect(self.accept)
        row_layout.addWidget(close)
        layout.addLayout(row_layout)
        self.status = QLabel(objectName="StatusText")
        self.status.hide()
        layout.addWidget(self.status)
        self._show_details(row)
        if ctx.server_link is not None:
            ctx.server_link.result.connect(self._on_sync)
        ctx.themes.theme_changed.connect(self._render)

    def _show_details(self, row: RequestRow) -> None:
        self.heading.setText(row.title)
        parts = [f"وضعیت: {row.status_name}", f"وضعیت ارسال: {row.send_status}",
                 f"محل مصرف: {row.location}", f"اقلام: {row.items_text}"]
        if row.created_by:
            parts.append(f"ثبت‌کننده: {row.created_by}")
        if row.note:
            parts.append(f"توضیحات: {row.note}")
        if row.rejection_reason:
            parts.append(f"دلیل رد: {row.rejection_reason}")
        if row.issue_number:
            parts.append(f"حواله: {to_persian_digits(row.issue_number)}")
        self.details.setText("\n".join(parts))
        can_chat = row.status != RequestStatus.DRAFT
        self.input.setEnabled(can_chat)
        self.send_button.setEnabled(can_chat)

    def _on_sync(self, result) -> None:
        if result.changed or result.events:
            self.reload()

    @asyncSlot()
    async def reload(self) -> None:
        db, actor = self._ctx.db, self._ctx.actor
        try:
            detail = await goods.get_request(db, actor, self._row.id)
            messages = await goods.list_messages(db, actor, self._row.id)
            await goods.mark_seen(db, actor, self._row.id)
        except ServiceError as exc:
            self.status.setText(exc.message)
            self.status.show()
            return
        self._row = detail.row
        self._show_details(detail.row)
        self._messages = messages
        self._render()

    def _render(self, *_args) -> None:
        """The conversation in the current theme's colours (redrawn when the theme changes)."""
        messages = getattr(self, "_messages", [])
        theme = self._ctx.themes.current
        self.transcript.clear()
        if not messages:
            self.transcript.append(f'<p style="color:{theme.text_muted}">هنوز پیامی رد و بدل نشده است.</p>')
        for m in messages:
            bg = theme.primary_soft if m.mine else theme.surface_alt
            when = (f"{jalali.format_date(m.created_at.date())} ساعت "
                    f"{to_persian_digits(m.created_at.strftime('%H:%M'))}") if m.created_at else ""
            note = " · در صف ارسال" if m.queued else (" · ارسال شد" if m.mine else "")
            who = f"{html.escape(m.sender)} · {when}{note}"
            self.transcript.append(
                f'<table width="100%" cellpadding="10" style="margin:6px 0"><tr><td '
                f'style="background:{bg}; color:{theme.text}; border-radius:10px">'
                f'<span style="color:{theme.text_muted}; font-size:8pt">{who}</span><br>'
                f'{html.escape(m.text).replace(chr(10), "<br>")}</td></tr></table>')
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())  # always opens at the latest message

    @asyncSlot()
    async def on_send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.send_button.setEnabled(False)
        try:
            await goods.queue_message(self._ctx.db, self._ctx.actor, self._row.id, text)
        except ServiceError as exc:
            self.status.setText(exc.message)
            self.status.show()
            return
        finally:
            self.send_button.setEnabled(True)
        self.input.clear()
        self.status.hide()
        if self._ctx.server_link is not None:
            self._ctx.server_link.wake()
        await self.reload()

    def done(self, result: int) -> None:
        if self._ctx.server_link is not None:
            self._ctx.server_link.result.disconnect(self._on_sync)
        super().done(result)


# ----- page -----


class RequestsPage(QWidget):
    refreshed = Signal()  # the list was reloaded (e.g. a request was opened: the badge may change)
    setup_requested = Signal()  # «ورود / تنظیم الان» on the connection bar

    def __init__(self, ctx: AppContext, open_document=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._open_document = open_document  # async (doc_id) -> None, from the main window
        self._rows: dict[int, RequestRow] = {}
        self._seq = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem("در انتظار و ردشده", OPEN)
        self.status_filter.addItem("همه وضعیت‌ها", None)
        for status, name in STATUS_NAMES.items():
            if status != RequestStatus.DRAFT:
                self.status_filter.addItem(name, status)
        self.status_filter.currentIndexChanged.connect(lambda _: self.refresh())
        toolbar.addWidget(self.status_filter)
        self.mine_only = QCheckBox("درخواست‌های من")
        self.mine_only.setToolTip("فقط درخواست‌هایی که خودتان ثبت کرده‌اید")
        self.mine_only.toggled.connect(lambda _: self.refresh())
        toolbar.addWidget(self.mine_only)
        self.filter_chip = FilterChip()
        self.filter_chip.clicked.connect(self.clear_filters)
        toolbar.addWidget(self.filter_chip)
        self.status_filter.currentIndexChanged.connect(lambda _: self._show_filter())
        self.mine_only.toggled.connect(lambda _: self._show_filter())
        self.count_label = QLabel(objectName="Muted")
        toolbar.addWidget(self.count_label)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        # Working without Caspian Server is normal (go-live before the server is ready): say so plainly.
        self.notice = QFrame(objectName="NoticeBar")
        notice = QHBoxLayout(self.notice)
        notice.setContentsMargins(12, 8, 12, 8)
        self.offline_banner = QLabel()
        self.offline_banner.setWordWrap(True)
        notice.addWidget(self.offline_banner, 1)
        self.setup_button = QPushButton("ورود / تنظیم الان")
        self.setup_button.setProperty("variant", "primary")
        self.setup_button.clicked.connect(self.setup_requested.emit)
        notice.addWidget(self.setup_button)
        self.notice.hide()
        layout.addWidget(self.notice)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.chat_button = QPushButton("جزئیات و گفتگو")
        self.chat_button.clicked.connect(self.on_chat)
        self.retry_button = QPushButton("ارسال مجدد")
        self.retry_button.setToolTip("سرور این درخواست را نپذیرفته بود؛ دوباره و بدون تغییر فرستاده می‌شود.")
        self.retry_button.clicked.connect(self.on_retry)
        self.open_button = QPushButton("باز کردن حواله")
        self.open_button.setToolTip("حواله ردشده را اصلاح و دوباره ارسال کنید.")
        self.open_button.clicked.connect(self.on_open_document)
        self.print_button = QPushButton("چاپ حواله")
        self.print_button.setToolTip("فقط پس از تأیید مالک و ثبت نهایی حواله.")
        self.print_button.setMenu(document_print_menu(self, ctx, self._document_id, self.refresh))
        for b in (self.chat_button, self.retry_button, self.open_button, self.print_button):
            actions.addWidget(b)
        layout.addLayout(actions)

        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(COLUMNS)
        self.table.set_empty_text("درخواستی در انتظار یا ردشده نیست. حواله خروج یا خروج امانی را در "
                                  "«اسناد انبار» بسازید و «ارسال درخواست» را بزنید.")
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self.on_chat())
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        if ctx.server_link is not None:
            ctx.server_link.result.connect(self._on_sync)
        self._update_buttons()

    def _show_filter(self) -> None:
        choice = self.status_filter.currentData()
        parts = [self.status_filter.currentText()] if choice not in (None, OPEN) else []
        if self.mine_only.isChecked():
            parts.append("درخواست‌های من")
        self.filter_chip.show_filter("، ".join(parts))

    def clear_filters(self) -> None:
        self.mine_only.setChecked(False)
        self.status_filter.setCurrentIndex(self.status_filter.findData(None))

    def selected(self) -> RequestRow | None:
        row_id = self.table.selected_id()
        return self._rows.get(row_id) if row_id is not None else None

    def _document_id(self) -> int | None:
        row = self.selected()
        return row.document_id if row else None

    def _update_buttons(self, *_args) -> None:
        row = self.selected()
        self.chat_button.setEnabled(row is not None)
        self.retry_button.setEnabled(row is not None and row.failed)
        self.open_button.setEnabled(row is not None and row.document_id is not None)
        printable = row is not None and row.document_id is not None and (
            row.document_status == DocStatus.POSTED
            or (not row.slip and row.status == RequestStatus.APPROVED))
        self.print_button.setEnabled(printable and self._ctx.actor.can(Perm.DOCUMENTS_VIEW))

    def _on_sync(self, result) -> None:
        self._show_server_state(result)
        if (result.changed or result.events) and self.isVisible():
            self.refresh()

    def _show_server_state(self, result) -> None:
        text = OFFLINE_TEXT.get(result.state, "")
        if result.state == SyncState.ACCOUNT_PROBLEM and result.message:
            text = f"{result.message} {SAVED_HERE}"
        self.offline_banner.setText(text)
        self.offline_banner.setVisible(bool(text))
        self.notice.setVisible(bool(text))
        # Nothing to set up while the internet is simply down: then the bar only informs.
        self.setup_button.setVisible(result.state != SyncState.DISCONNECTED)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    @asyncSlot()
    async def refresh(self) -> None:
        self._seq += 1
        seq = self._seq
        try:
            choice = self.status_filter.currentData()
            rows = await goods.list_requests(
                self._ctx.db, self._ctx.actor, None if choice == OPEN else choice,
                mine=self.mine_only.isChecked(), open_only=choice == OPEN)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        if seq != self._seq:
            return
        self._rows = {r.id: r for r in rows}
        theme = self._ctx.themes.current
        tones = {RequestStatus.APPROVED: "ok", RequestStatus.ISSUED: "ok", RequestStatus.REJECTED: "bad",
                 RequestStatus.PENDING: "pending", RequestStatus.QUEUED: "pending",
                 RequestStatus.DRAFT: "draft", RequestStatus.WITHDRAWN: "draft"}
        self.table.set_rows(
            [(r.id, (r.number_text, r.requester, r.created_by, r.location, r.items_text,
                     ("● " if r.unseen else "") + r.status_name, r.send_status,
                     jalali.format_date(r.created_at.date()) if r.created_at else "", r.note))
             for r in rows],
            muted=[r.origin == "remote" for r in rows],
            highlight={(i, SEND_COL): theme.danger for i, r in enumerate(rows) if r.failed},
            badges={(i, STATUS_COL): tones[r.status] for i, r in enumerate(rows) if r.status in tones})
        self.count_label.setText(f"{to_persian_digits(len(rows))} درخواست")
        self._update_buttons()
        self.refreshed.emit()

    @asyncSlot()
    async def on_retry(self) -> None:
        row = self.selected()
        if row is None or not row.failed:
            return
        try:
            await goods.retry_failed(self._ctx.db, self._ctx.actor, row.id)
        except ServiceError as exc:
            show_error(self, exc.message)
            return
        if self._ctx.server_link is not None:
            self._ctx.server_link.wake()
        await self.refresh()

    @asyncSlot()
    async def on_chat(self) -> None:
        row = self.selected()
        if row is None:
            return
        dialog = RequestChatDialog(self._ctx, row, self)
        await dialog.reload()
        await exec_dialog(dialog)
        await self.refresh()

    @asyncSlot()
    async def on_open_document(self) -> None:
        row = self.selected()
        if row is not None and row.document_id is not None and self._open_document is not None:
            await self._open_document(row.document_id)
