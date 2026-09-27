"""Chat assistant: a tool-calling loop over the gateway, with an offline fallback."""

import datetime as dt
from dataclasses import dataclass, field

from caspian.core import jalali
from caspian.db.database import Database
from caspian.db.models import DocType, ImportKind, ImportSource
from caspian.services import imports, master
from caspian.services.actor import Actor
from caspian.services.ai.gateway import AIUnavailable, Gateway
from caspian.services.ai.text_parser import parse_text
from caspian.services.ai.tools import ToolContext, available_tools, run_tool

MAX_TOOL_ROUNDS = 5
HISTORY_LIMIT = 30

SYSTEM_PROMPT = """تو دستیار هوشمند نرم‌افزار انبارداری «انبار کاسپین» هستی. امروز {today} است.
کاربر فعلی: {user}.
قوانین:
- همیشه به فارسی، کوتاه و دقیق پاسخ بده.
- برای هر عدد یا اطلاعات انبار حتماً از ابزارها استفاده کن و هرگز عدد از خودت نساز.
- تو فقط «پیش‌نویس» می‌سازی (ابزار create_stock_draft). نمی‌توانی سندی را ثبت نهایی، کالایی را حذف
  یا غیرفعال، ادغام، بازیابی پشتیبان، تغییر نقش یا بستن سال مالی انجام دهی و PIN مدیر را دور بزنی.
  اگر کاربر چنین چیزی خواست، توضیح بده که باید خودش از بخش مربوط در برنامه انجام دهد.
- وقتی کاربر فهرستی از کالا و تعداد را برای ورود یا خروج گفت، با create_stock_draft پیش‌نویس بساز
  و بگو که در «ورود اطلاعات» بررسی و اعمال کند.
- برای پیشنهاد سفارش از reorder_analysis استفاده کن و نتیجه را مثل «۵۰ عدد سفارش دهید؛ حدود ۲٫۵ ماه
  مصرف را پوشش می‌دهد» بیان کن. اگر پیام درخواست خرید برای تأمین‌کننده خواستند، متن مؤدبانه و آماده
  ارسال بنویس.
"""


@dataclass
class AssistantReply:
    text: str
    provider: str = ""
    created_batches: list[int] = field(default_factory=list)
    offline: bool = False


class Assistant:
    def __init__(self, db: Database, gateway: Gateway, actor: Actor) -> None:
        self._db = db
        self._gateway = gateway
        self.actor = actor.as_ai()  # the AI never acts with full human authority
        self._display_name = actor.display_name
        self.history: list[dict] = []

    def reset(self) -> None:
        self.history = []

    def _system(self) -> dict:
        return {"role": "system", "content": SYSTEM_PROMPT.format(
            today=jalali.format_long(dt.date.today()), user=self._display_name)}

    async def send(self, text: str) -> AssistantReply:
        self.history.append({"role": "user", "content": text})
        self.history = self.history[-HISTORY_LIMIT:]
        ctx = ToolContext(self._db, self.actor, [])
        tools = [t.schema() for t in available_tools(self.actor)]
        messages = [self._system(), *self.history]
        provider = ""
        for _ in range(MAX_TOOL_ROUNDS):
            result = await self._gateway.chat(messages, tools=tools)
            provider = result.provider
            if not result.tool_calls:
                self.history.append({"role": "assistant", "content": result.content})
                return AssistantReply(result.content, provider, ctx.created_batches)
            messages.append({"role": "assistant", "content": result.content or None,
                             "tool_calls": result.tool_calls})
            for call in result.tool_calls:
                function = call.get("function", {})
                output = await run_tool(ctx, function.get("name", ""), function.get("arguments", "{}"))
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})
        text = "پاسخ کامل آماده نشد. لطفاً سؤال را ساده‌تر بپرسید."
        self.history.append({"role": "assistant", "content": text})
        return AssistantReply(text, provider, ctx.created_batches)


async def text_to_draft(db: Database, gateway: Gateway | None, actor: Actor, text: str,
                        doc_type: DocType, warehouse_id: int, title: str = "") -> tuple[int, bool]:
    """Typed item list -> import draft. Uses the AI when reachable, else the offline parser.

    Returns (batch id, used_ai).
    """
    title = title or f"متن تایپ‌شده {jalali.format_date(dt.date.today(), False)}"
    if gateway is not None and await gateway.available():
        assistant = Assistant(db, gateway, actor)
        try:
            reply = await assistant.send(
                f"از متن زیر یک پیش‌نویس {'رسید ورود' if doc_type == DocType.RECEIPT else 'حواله خروج'}"
                f" بساز (doc_type={doc_type.value}) و فقط تأیید کوتاه بده:\n{text}")
            if reply.created_batches:
                return reply.created_batches[-1], True
        except AIUnavailable:
            pass
    units = {u.name for u in await master.list_units(db)}
    rows = parse_text(text, units)
    batch_id = await imports.create_batch(db, actor.as_ai(), ImportKind.STOCK, ImportSource.TEXT,
                                          rows, title, doc_type, warehouse_id)
    return batch_id, False
