"""AI assistant: chat box (always available) + in-app voice button."""

import html
import logging
import re

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from caspian.core.recorder import Recorder, RecorderError, has_microphone
from caspian.core.text import to_persian_digits
from caspian.services.ai.assistant import Assistant
from caspian.services.ai.gateway import AIUnavailable
from caspian.services.errors import ServiceError
from caspian.ui.app_context import AppContext
from caspian.ui.icons import icon
from caspian.ui.widgets import Card

log = logging.getLogger(__name__)

SUGGESTIONS = (
    ("کالاهای نیازمند سفارش", "کدام کالاها نیاز به سفارش دارند؟ برای هرکدام مقدار پیشنهادی را بگو."),
    ("امانی‌های باز", "چه کالاهایی امانی بیرون است و دست چه کسی؟"),
    ("آخرین اسناد", "آخرین ۵ سند انبار را خلاصه کن."),
    ("پیام درخواست خرید", "برای کالاهای نیازمند سفارش یک پیام درخواست خرید مؤدبانه برای تأمین‌کننده بنویس."),
)


def _to_html(text: str) -> str:
    """Tiny markdown subset: **bold**, bullet lines, line breaks."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?m)^\s*[-*•]\s+", "• ", out)
    return out.replace("\n", "<br>")


class AssistantPage(QWidget):
    def __init__(self, ctx: AppContext, open_batch=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._open_batch = open_batch  # callable(batch_id) supplied by the main window
        self._assistant = Assistant(ctx.db, ctx.ai, ctx.actor)
        self._recorder = Recorder()
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = QHBoxLayout()
        self.status = QLabel(objectName="Muted")
        top.addWidget(self.status, 1)
        self.new_button = QPushButton("گفتگوی جدید")
        self.new_button.clicked.connect(self.on_new_chat)
        top.addWidget(self.new_button)
        layout.addLayout(top)

        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        self.transcript = QTextBrowser(objectName="Transcript")
        self.transcript.setOpenLinks(False)
        self.transcript.anchorClicked.connect(self.on_link)
        self.transcript.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        card.body.addWidget(self.transcript)
        layout.addWidget(card, 1)

        chips = QHBoxLayout()
        for label, prompt in SUGGESTIONS:
            chip = QPushButton(label, objectName="Chip")
            chip.clicked.connect(lambda _=False, p=prompt: self.send(p))
            chips.addWidget(chip)
        chips.addStretch(1)
        layout.addLayout(chips)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("پیام خود را بنویسید… مثلاً «۵ دریل بوش و ۲ کارتن پیچ رسید بزن»")
        self.input.returnPressed.connect(lambda: self.send(self.input.text()))
        row.addWidget(self.input, 1)
        self.mic = QToolButton(objectName="IconButton")
        self.mic.setIconSize(QSize(22, 22))
        self.mic.setToolTip("فرمان صوتی: یک بار بزنید تا ضبط شروع شود، دوباره بزنید تا ارسال شود")
        self.mic.clicked.connect(self.on_mic)
        self.mic.setVisible(has_microphone())
        row.addWidget(self.mic)
        self.send_button = QPushButton("ارسال")
        self.send_button.setProperty("variant", "primary")
        self.send_button.clicked.connect(lambda: self.send(self.input.text()))
        row.addWidget(self.send_button)
        layout.addLayout(row)

        ctx.user_changed.connect(self._on_user_changed)
        ctx.themes.theme_changed.connect(self._refresh_icons)
        self._refresh_icons()
        self._welcome()

    # ----- helpers -----

    def _refresh_icons(self, *_args) -> None:
        theme = self._ctx.themes.current
        color = theme.danger if self._recorder.recording else theme.text_muted
        self.mic.setIcon(icon("mic", color))

    def _append(self, role: str, text: str, extra_html: str = "") -> None:
        theme = self._ctx.themes.current
        if role == "user":
            bg, fg, who = theme.primary_soft, theme.text, "شما"
        elif role == "error":
            bg, fg, who = theme.danger_soft, theme.text, "دستیار"
        else:
            bg, fg, who = theme.surface_alt, theme.text, "دستیار"
        self.transcript.append(
            f'<table width="100%" cellpadding="10" style="margin:6px 0"><tr><td '
            f'style="background:{bg}; color:{fg}; border-radius:10px">'
            f'<span style="color:{theme.text_muted}; font-size:8pt">{who}</span><br>'
            f"{_to_html(text)}{extra_html}</td></tr></table>")
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _welcome(self) -> None:
        self.transcript.clear()
        self._append("assistant",
                     "سلام! می‌توانم موجودی و اسناد را برایتان جستجو کنم، کالاهای نیازمند سفارش را "
                     "پیشنهاد بدهم، پیام درخواست خرید بنویسم یا از فهرستی که می‌گویید «پیش‌نویس» "
                     "رسید/حواله بسازم.\nمن فقط پیش‌نویس می‌سازم؛ ثبت نهایی و عملیات حساس همیشه با "
                     "خود شماست.")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        self.send_button.setEnabled(not busy)
        self.input.setEnabled(not busy)
        for chip in self.findChildren(QPushButton, "Chip"):
            chip.setEnabled(not busy)
        if text:
            self.status.setText(text)

    def _on_user_changed(self, actor) -> None:
        self._assistant = Assistant(self._ctx.db, self._ctx.ai, actor)
        self._welcome()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.update_status()
        self.input.setFocus()

    @asyncSlot()
    async def update_status(self) -> None:
        online = await self._ctx.ai.is_online()
        ready = len(await self._ctx.ai.candidates())
        if ready:
            self.status.setText(f"دستیار آماده است — {to_persian_digits(ready)} سرویس در دسترس"
                                + ("" if online else " (آفلاین: فقط مدل محلی)"))
        else:
            self.status.setText("دستیار در دسترس نیست — سرویس هوش مصنوعی تنظیم نشده یا اینترنت قطع است. "
                                "بقیه برنامه به‌طور کامل کار می‌کند.")

    # ----- actions -----

    @asyncSlot()
    async def send(self, text: str) -> None:
        text = text.strip()
        if not text or self._busy:
            return
        self.input.clear()
        self._append("user", text)
        self._set_busy(True, "در حال فکر کردن…")
        try:
            reply = await self._assistant.send(text)
        except AIUnavailable as exc:
            self._append("error", f"{exc.message}\nبرای ساخت پیش‌نویس از فهرست تایپ‌شده، بدون "
                                  "اینترنت هم می‌توانید از «ورود اطلاعات ← از متن» استفاده کنید.")
            self._set_busy(False)
            await self.update_status()
            return
        except ServiceError as exc:
            self._append("error", exc.message)
            self._set_busy(False)
            return
        except Exception:
            log.exception("Assistant failed")
            self._append("error", "خطای غیرمنتظره در دستیار. جزئیات در فایل گزارش ثبت شد.")
            self._set_busy(False)
            return
        links = "".join(
            f'<br><a href="batch:{b}">بررسی پیش‌نویس شماره {to_persian_digits(b)} ←</a>'
            for b in reply.created_batches)
        self._append("assistant", reply.text, links)
        self._set_busy(False, f"پاسخ از «{reply.provider}»" if reply.provider else "")

    @asyncSlot()
    async def on_mic(self) -> None:
        if self._busy:
            return
        if not self._recorder.recording:
            try:
                self._recorder.start()
            except RecorderError as exc:
                self._append("error", str(exc))
                self.mic.setVisible(has_microphone())
                return
            self.status.setText("در حال ضبط… برای پایان، دوباره روی میکروفون بزنید.")
            self._refresh_icons()
            return
        try:
            audio = self._recorder.stop()
        except RecorderError as exc:
            self._refresh_icons()
            self._append("error", str(exc))
            return
        self._refresh_icons()
        self._set_busy(True, "در حال تبدیل گفتار به متن…")
        try:
            text = await self._ctx.ai.transcribe(audio)
        except AIUnavailable as exc:
            self._set_busy(False)
            self._append("error", exc.message)
            return
        finally:
            del audio  # voice isn't kept anywhere
        self._set_busy(False)
        if text:
            await self.send(text)
        else:
            self.status.setText("صدایی تشخیص داده نشد.")

    def on_new_chat(self) -> None:
        self._assistant.reset()
        self._welcome()

    def on_link(self, url: QUrl) -> None:
        target = url.toString()
        if target.startswith("batch:") and self._open_batch:
            self._open_batch(int(target.split(":", 1)[1]))
