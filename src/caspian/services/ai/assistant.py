"""Chat assistant: a tool-calling loop over the gateway, with an offline fallback."""

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

from caspian.core import jalali
from caspian.core.permissions import Perm
from caspian.core.text import to_persian_digits
from caspian.db.database import Database
from caspian.db.models import DocType, ImportKind, ImportSource
from caspian.services import imports, master
from caspian.services.actor import Actor
from caspian.services.ai import history as chat_history
from caspian.services.ai.config import ai_may_post
from caspian.services.ai.gateway import AIUnavailable, Gateway
from caspian.services.ai.text_parser import parse_text
from caspian.services.ai.tools import ToolContext, available_tools, run_tool

log = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 6
HISTORY_LIMIT = 30
AI_TOTAL_TIMEOUT = 60  # seconds for one whole request, all tool rounds included (#8)

MENUS = ("داشبورد، کالاها، اطلاعات پایه، اسناد انبار، ورود اطلاعات، انبارگردانی، گزارش‌ها، "
         "دستیار هوشمند، کاربران، تنظیمات")

SYSTEM_PROMPT = """تو دستیار هوشمند نرم‌افزار انبارداری «انبار کاسپین» هستی. امروز {today} است.
کاربر فعلی: {user}. بخش‌های برنامه دقیقاً این‌ها هستند: {menus}. نام بخش دیگری را از خودت نساز.
قوانین:
- همیشه به فارسی، کوتاه و دقیق پاسخ بده.
- برای هر عدد یا اطلاعات انبار حتماً از ابزارها استفاده کن و هرگز عدد از خودت نساز.
- وقتی کاربر کاری مثل «حواله خروج بزن برای آقای X که ۲ عدد جارو برده» خواست، مثل یک کاربر عمل کن:
  با create_document سند را بساز (نام شخص در person، کالاها در lines). {post_rule}
- هر سند را فقط یک بار بساز. اگر ابزار needs_choice برگرداند (چند کالا یا شخص مشابه، یا پیدا نشد)،
  هرگز حدس نزن: گزینه‌ها را با کد و موجودی نشان بده و از کاربر بپرس، بعد دوباره بساز.
- برای فهرست‌های طولانی که کاربر باید بررسی کند، create_stock_draft (بخش «ورود اطلاعات») مناسب است.
- تو هرگز سندی را ابطال یا حذف نمی‌کنی، کالا/شخص/کاربر را غیرفعال یا ادغام نمی‌کنی، نسخه پشتیبان را
  بازیابی نمی‌کنی، نقش تغییر نمی‌دهی، سال مالی نمی‌بندی و PIN مدیر را دور نمی‌زنی. اگر خواستند،
  بگو باید خودشان از بخش مربوط انجام دهند.
- برای پیشنهاد سفارش از reorder_analysis استفاده کن و نتیجه را مثل «۵۰ عدد سفارش دهید؛ حدود ۲٫۵ ماه
  مصرف را پوشش می‌دهد» بیان کن. اگر پیام درخواست خرید برای تأمین‌کننده خواستند، متن مؤدبانه و آماده
  ارسال بنویس.
"""
POST_ALLOWED = ("اگر کاربر صریحاً یا ضمنی خواست کار تمام شود (مثلاً «حواله بزن»)، post=true بگذار تا ثبت "
                "نهایی شود؛ اگر ثبت نهایی خطا داد (مثلاً موجودی کافی نیست)، توضیح بده که پیش‌نویس ماند.")
POST_FORBIDDEN = ("ثبت نهایی توسط دستیار در تنظیمات غیرفعال است: فقط پیش‌نویس بساز و بگو کاربر در «اسناد "
                  "انبار» آن را ثبت نهایی کند.")


@dataclass
class AssistantReply:
    text: str
    provider: str = ""
    created_batches: list[int] = field(default_factory=list)
    offline: bool = False
    # [{document_id, number, type, status}] — shown as links so nothing done goes unnoticed (#4)
    created_documents: list[dict] = field(default_factory=list)


class Assistant:
    def __init__(self, db: Database, gateway: Gateway, actor: Actor, messenger=None) -> None:
        self._db = db
        self._gateway = gateway
        self._messenger = messenger
        self.actor = actor.as_ai()  # the AI never acts with full human authority
        self._display_name = actor.display_name
        self._user_id = actor.user_id
        self.history: list[dict] = []
        self.conversation = uuid.uuid4().hex
        self.record_history = True
        self._pending: set[asyncio.Task] = set()

    def reset(self) -> None:
        self.history = []
        self.conversation = uuid.uuid4().hex

    def _remember(self, text: str, reply: "AssistantReply") -> None:
        """Save the exchange in the chat history *after* replying, in the background (round 2, B)."""
        if not self.record_history:
            return

        async def save() -> None:
            try:
                await chat_history.record(self._db, self._user_id, self.conversation, "user", text)
                await chat_history.record(
                    self._db, self._user_id, self.conversation, "assistant", reply.text, reply.provider,
                    [d["document_id"] for d in reply.created_documents], reply.created_batches)
            except Exception:
                log.exception("Saving assistant history failed")

        task = asyncio.ensure_future(save())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _system(self, allow_post: bool) -> dict:
        return {"role": "system", "content": SYSTEM_PROMPT.format(
            today=jalali.format_long(dt.date.today()), user=self._display_name, menus=MENUS,
            post_rule=POST_ALLOWED if allow_post else POST_FORBIDDEN)}

    async def send(self, text: str, timeout: float | None = None) -> AssistantReply:
        self.history.append({"role": "user", "content": text})
        self.history = self.history[-HISTORY_LIMIT:]
        allow_post = self.actor.can(Perm.DOCUMENTS_POST) and await ai_may_post(self._db)
        ctx = ToolContext(self._db, self.actor, [], self._messenger, allow_post=allow_post)
        try:
            reply = await asyncio.wait_for(self._run(ctx, allow_post), timeout or AI_TOTAL_TIMEOUT)
            self._remember(text, reply)
            return reply
        except (AIUnavailable, TimeoutError) as exc:
            reason = exc.message if isinstance(exc, AIUnavailable) else \
                "پاسخ دستیار بیش از حد طول کشید و متوقف شد."
            if not (ctx.created_documents or ctx.created_batches):
                if isinstance(exc, AIUnavailable):
                    raise
                raise AIUnavailable(reason) from exc
            # Something was already created: say so instead of only showing an error (#4).
            text = f"{reason}\n\nپیش از این خطا این موارد انجام شد:\n" + _created_summary(ctx)
            self.history.append({"role": "assistant", "content": text})
            reply = AssistantReply(text, "", ctx.created_batches, created_documents=ctx.created_documents)
            self._remember(self.history[-2]["content"] if len(self.history) > 1 else "", reply)
            return reply

    async def _run(self, ctx: ToolContext, allow_post: bool) -> AssistantReply:
        tools = [t.schema() for t in available_tools(self.actor, allow_post)]
        messages = [self._system(allow_post), *self.history]
        provider = ""
        for _ in range(MAX_TOOL_ROUNDS):
            result = await self._gateway.chat(messages, tools=tools)
            provider = result.provider
            if not result.tool_calls:
                self.history.append({"role": "assistant", "content": result.content})
                return AssistantReply(result.content, provider, ctx.created_batches,
                                      created_documents=ctx.created_documents)
            messages.append({"role": "assistant", "content": result.content or None,
                             "tool_calls": result.tool_calls})
            for call in result.tool_calls:
                function = call.get("function", {})
                output = await run_tool(ctx, function.get("name", ""), function.get("arguments", "{}"))
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})
        text = "پاسخ کامل آماده نشد. لطفاً سؤال را ساده‌تر بپرسید."
        if ctx.created_documents or ctx.created_batches:
            text += "\n" + _created_summary(ctx)
        self.history.append({"role": "assistant", "content": text})
        return AssistantReply(text, provider, ctx.created_batches, created_documents=ctx.created_documents)


def _created_summary(ctx: ToolContext) -> str:
    lines = [f"• {d['type']} {d['number']} — {'ثبت نهایی' if d.get('status') == 'POSTED' else 'پیش‌نویس'}"
             for d in ctx.created_documents]
    lines += [f"• پیش‌نویس ورود اطلاعات شماره {to_persian_digits(b)}" for b in ctx.created_batches]
    return "\n".join(lines)


async def text_to_draft(db: Database, gateway: Gateway | None, actor: Actor, text: str,
                        doc_type: DocType, warehouse_id: int, title: str = "",
                        timeout: float | None = None) -> tuple[int, bool]:
    """Typed item list -> import draft. Uses the AI when reachable, else the offline parser.

    Returns (batch id, used_ai).
    """
    title = title or f"متن تایپ‌شده {jalali.format_date(dt.date.today())}"
    if gateway is not None and await gateway.available():
        assistant = Assistant(db, gateway, actor)
        assistant.record_history = False  # a typed list for «ورود اطلاعات», not a conversation
        try:
            kind = "رسید ورود" if doc_type == DocType.RECEIPT else "حواله خروج"
            reply = await assistant.send(
                f"فقط با ابزار create_stock_draft از متن زیر یک پیش‌نویس {kind} بساز "
                f"(doc_type={doc_type.value}، title=«{title}») و فقط تأیید کوتاه بده:\n{text}",
                timeout=timeout)
            if reply.created_batches:
                return reply.created_batches[-1], True
        except AIUnavailable:
            pass
    units = {u.name for u in await master.list_units(db)}
    rows = parse_text(text, units)
    batch_id = await imports.create_batch(db, actor.as_ai(), ImportKind.STOCK, ImportSource.TEXT,
                                          rows, title, doc_type, warehouse_id)
    return batch_id, False
