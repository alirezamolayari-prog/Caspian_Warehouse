"""«دستیار کاسپین» through Caspian Server (POST /ai/chat, GET /ai/limits/me; Caspian-Server
docs/ai-api.md). The AI key stays on the server, which also puts its own system prompt first; this app
sends the conversation and its tool definitions, runs the tool calls itself on its own data and sends
the results back. The signed-in person's account is used (else the old shared account).

Local AI providers (Settings ← هوش مصنوعی) are only an advanced fallback, off by default.
"""

import logging
from collections.abc import Callable
from typing import Any

import httpx

from caspian.db.database import Database
from caspian.db.models import AppSetting
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.ai.gateway import AIUnavailable, ChatResult, Gateway
from caspian.services.errors import PermissionDenied
from caspian.services.server_link import session
from caspian.services.server_link.client import ServerClient
from caspian.services.server_link.errors import (
    ServerAccountProblem,
    ServerError,
    ServerRejected,
)

log = logging.getLogger(__name__)

APP = "warehouse"
CAPABILITY = "assistant"
MAX_MESSAGES = 60
MAX_TOOLS = 32
TIMEOUT_SECONDS = 60.0
LOCAL_FALLBACK_KEY = "ai.local_fallback"
PROVIDER = "سرور کاسپین"

AI_TEXT = {
    "ai_user_daily_limit": "سهم امروز شما از دستیار کاسپین تمام شد؛ فردا دوباره امتحان کنید.",
    "ai_global_daily_limit": ("سهم امروز همهٔ کاربران از دستیار کاسپین تمام شده است؛ فردا دوباره "
                              "امتحان کنید."),
    "ai_input_too_long": "گفتگو خیلی طولانی شده است؛ «گفتگوی تازه» را بزنید و دوباره بپرسید.",
    "ai_input_empty": "متنی برای دستیار نوشته نشده است.",
    "ai_unavailable": ("دستیار کاسپین الان در دسترس نیست؛ کمی بعد دوباره امتحان کنید. بقیه برنامه "
                       "کار می‌کند."),
    "ai_unknown_capability": "این نسخه برنامه با سرور هماهنگ نیست؛ برنامه را به‌روز کنید.",
    "ai_app_not_allowed": "حساب شما در سرور نقش «انبار» ندارد؛ با مدیر سرور تماس بگیرید.",
}
NOT_SIGNED_IN = ("برای استفاده از دستیار کاسپین با حساب سرور (شماره موبایل) وارد شوید، یا حساب مشترک "
                 "در «تنظیمات ← اتصال به سرور» تنظیم شود.")
UNREACHABLE = "سرور کاسپین در دسترس نیست؛ دستیار فعلاً کار نمی‌کند. بقیه برنامه به‌طور کامل کار می‌کند."


def _message(exc: ServerError) -> str:
    if isinstance(exc, ServerRejected):
        if exc.code in AI_TEXT:
            return AI_TEXT[exc.code]
        if exc.status == 429:
            return AI_TEXT["ai_user_daily_limit"]
        if exc.status == 422:
            return AI_TEXT["ai_input_too_long"]
        return AI_TEXT["ai_unavailable"]
    if isinstance(exc, ServerAccountProblem):
        return exc.message
    return UNREACHABLE


async def local_fallback(db: Database) -> bool:
    async with db.session() as s:
        row = await s.get(AppSetting, LOCAL_FALLBACK_KEY)
    return bool(row and isinstance(row.value, dict) and row.value.get("enabled"))


async def set_local_fallback(db: Database, actor: Actor, enabled: bool) -> None:
    """«استفاده از سرویس محلی (پیشرفته)»: only with AI configuration rights, never by the AI."""
    if actor.is_ai:
        raise PermissionDenied("دستیار کاسپین نمی‌تواند تنظیمات خودش را تغییر دهد.")
    from caspian.core.permissions import Perm

    actor.require(Perm.AI_CONFIGURE)
    async with db.session(actor) as s:
        row = await s.get(AppSetting, LOCAL_FALLBACK_KEY)
        if row is None:
            s.add(AppSetting(key=LOCAL_FALLBACK_KEY, value={"enabled": enabled}))
        else:
            row.value = {"enabled": enabled}
        audit.record(s, actor, "ai.local_fallback", details={"enabled": enabled})


class CaspianGateway:
    """Same interface the assistant uses (`chat`, `available`, …) as the local `Gateway`."""

    def __init__(self, db: Database, local: Gateway, actor: Callable[[], Actor],
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = TIMEOUT_SECONDS) -> None:
        self._db, self.local, self._actor = db, local, actor
        self._transport, self._timeout = transport, timeout
        self.limits: dict | None = None  # {"used_today", "limit_today"} after each answer

    async def _client(self) -> ServerClient | None:
        return await session.client_for(self._db, self._actor(), self._transport, self._timeout)

    async def available(self) -> bool:
        if await self._client() is not None:
            return True
        return await local_fallback(self._db) and await self.local.available()

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.2, max_tokens: int = 1500,
                   json_mode: bool = False) -> ChatResult:
        try:
            return await self._server_chat(messages, tools, max_tokens)
        except AIUnavailable as exc:
            if await local_fallback(self._db) and await self.local.available():
                log.info("Caspian Server assistant failed (%s); using the local providers", exc.message)
                return await self.local.chat(messages, tools, temperature, max_tokens, json_mode)
            raise

    async def _server_chat(self, messages: list[dict], tools: list[dict] | None,
                           max_tokens: int) -> ChatResult:
        client = await self._client()
        if client is None:
            raise AIUnavailable(NOT_SIGNED_IN)
        body: dict[str, Any] = {
            "app": APP, "capability": CAPABILITY, "max_tokens": max_tokens,
            # The server puts its own system prompt first and ignores ours: don't send it.
            "messages": [m for m in messages if m.get("role") != "system"][-MAX_MESSAGES:]}
        if tools:
            body["tools"] = tools[:MAX_TOOLS]
        try:
            answer = await client.call("POST", "/ai/chat", json=body)
        except ServerError as exc:
            log.warning("Caspian Server assistant refused: %s",
                        getattr(exc, "code", "") or type(exc).__name__)
            raise AIUnavailable(_message(exc)) from exc
        message = answer.get("message") or {}
        self.limits = {"used_today": answer.get("used_today"), "limit_today": answer.get("limit_today")}
        return ChatResult(message.get("content") or "", message.get("tool_calls") or [], PROVIDER,
                          answer.get("model") or "", message)

    async def limits_me(self) -> dict | None:
        """«سهم امروز شما»: {used_today, daily_limit|None, unlimited, remaining|None, …} or None."""
        client = await self._client()
        if client is None:
            return None
        try:
            return await client.call("GET", "/ai/limits/me")
        except ServerError:
            return None

    # The rest comes from the local providers (voice, the settings tab).
    async def transcribe(self, audio: bytes) -> str:
        return await self.local.transcribe(audio)

    async def is_online(self) -> bool:
        return await self.local.is_online()

