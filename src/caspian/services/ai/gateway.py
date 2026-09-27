"""Provider-agnostic AI gateway with silent fallback.

All supported providers (OpenAI-compatible, Groq, Hugging Face router, Ollama) speak the
OpenAI chat-completions protocol, so one client covers them. Providers are tried in
priority order; on rate limits (429), timeouts, connection errors or server errors the
next one is tried without bothering the user. A rate-limited provider is skipped for a
cooldown period. When the internet is unreachable only local providers are tried.
"""

import asyncio
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from caspian.db.database import Database
from caspian.services.ai.config import ProviderConfig, get_key, list_providers
from caspian.services.errors import ServiceError

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN = 60.0
ONLINE_CACHE_SECONDS = 30.0


class AIUnavailable(ServiceError):
    """No provider could answer. The rest of the app keeps working."""


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict]
    provider: str
    model: str
    message: dict  # raw assistant message, for appending to the conversation


@dataclass
class Attempt:
    provider: str
    outcome: str  # ok | rate_limited | timeout | unreachable | server_error | auth_error | error


@dataclass
class _State:
    cooldown_until: dict[int, float] = field(default_factory=dict)
    online: tuple[float, bool] | None = None


def _default_online_check() -> bool:
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 443), timeout=2):
                return True
        except OSError:
            continue
    return False


class Gateway:
    def __init__(self, db: Database, transport: httpx.AsyncBaseTransport | None = None,
                 online_check: Callable[[], bool] = _default_online_check,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._db = db
        self._transport = transport
        self._online_check = online_check
        self._clock = clock
        self._state = _State()
        self.last_attempts: list[Attempt] = []

    # ----- availability -----

    async def is_online(self) -> bool:
        cached = self._state.online
        if cached and self._clock() - cached[0] < ONLINE_CACHE_SECONDS:
            return cached[1]
        online = await asyncio.to_thread(self._online_check)
        self._state.online = (self._clock(), online)
        return online

    async def candidates(self) -> list[ProviderConfig]:
        providers = [p for p in await list_providers(self._db) if p.usable]
        if not await self.is_online():
            providers = [p for p in providers if p.is_local]
        now = self._clock()
        return [p for p in providers if self._state.cooldown_until.get(p.id, 0) <= now]

    async def available(self) -> bool:
        return bool(await self.candidates())

    # ----- chat -----

    def _client(self, provider: ProviderConfig, timeout: float | None = None) -> httpx.AsyncClient:
        headers = {"Content-Type": "application/json"}
        key = get_key(provider.id)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return httpx.AsyncClient(base_url=provider.base_url, headers=headers,
                                 timeout=timeout or provider.timeout_seconds,
                                 transport=self._transport)

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.2, max_tokens: int = 1500,
                   json_mode: bool = False) -> ChatResult:
        self.last_attempts = []
        providers = await self.candidates()
        if not providers:
            raise AIUnavailable(await self._unavailable_reason())
        for provider in providers:
            body: dict = {"model": provider.model, "messages": messages,
                          "temperature": temperature, "max_tokens": max_tokens}
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            outcome = await self._try(provider, "/chat/completions", json=body)
            if isinstance(outcome, dict):
                message = outcome["choices"][0]["message"]
                return ChatResult(message.get("content") or "", message.get("tool_calls") or [],
                                  provider.name, provider.model, message)
        raise AIUnavailable("هیچ‌یک از سرویس‌های هوش مصنوعی پاسخ نداد. کمی بعد دوباره تلاش کنید "
                            "یا تنظیمات را بررسی کنید.")

    async def transcribe(self, audio: bytes, filename: str = "voice.wav",
                         language: str = "fa") -> str:
        """Speech-to-text via the first provider that has an STT model configured."""
        self.last_attempts = []
        providers = [p for p in await self.candidates() if p.stt_model]
        if not providers:
            raise AIUnavailable("هیچ سرویسی برای تبدیل گفتار به متن تنظیم نشده است "
                                "(مدل گفتار را در تنظیمات هوش مصنوعی وارد کنید).")
        for provider in providers:
            outcome = await self._try(
                provider, "/audio/transcriptions",
                files={"file": (filename, audio, "audio/wav")},
                data={"model": provider.stt_model, "language": language},
                json_headers=False)
            if isinstance(outcome, dict):
                return (outcome.get("text") or "").strip()
        raise AIUnavailable("تبدیل گفتار به متن انجام نشد. از جعبه گفتگو استفاده کنید.")

    async def test_provider(self, provider: ProviderConfig) -> tuple[bool, str, float]:
        """(ok, message, seconds) for the settings page."""
        started = self._clock()
        outcome = await self._try(provider, "/chat/completions", json={
            "model": provider.model, "max_tokens": 5,
            "messages": [{"role": "user", "content": "ping"}]}, record_cooldown=False)
        elapsed = self._clock() - started
        if isinstance(outcome, dict):
            return True, "اتصال برقرار است.", elapsed
        return False, _OUTCOME_TEXT.get(outcome, "خطا"), elapsed

    async def _try(self, provider: ProviderConfig, path: str, json_headers: bool = True,
                   record_cooldown: bool = True, **kwargs) -> dict | str:
        """Returns the decoded JSON on success, otherwise an outcome code."""
        try:
            async with self._client(provider) as client:
                if not json_headers:
                    client.headers.pop("Content-Type", None)
                response = await client.post(path, **kwargs)
        except httpx.TimeoutException:
            outcome = "timeout"
        except (httpx.ConnectError, httpx.NetworkError):
            outcome = "unreachable"
        except httpx.HTTPError as exc:
            log.warning("AI provider %s failed: %s", provider.name, exc)
            outcome = "error"
        else:
            if response.status_code == 200:
                try:
                    data = response.json()
                    self.last_attempts.append(Attempt(provider.name, "ok"))
                    return data
                except ValueError:
                    outcome = "error"
            elif response.status_code == 429:
                outcome = "rate_limited"
                if record_cooldown:
                    wait = _retry_after(response) or DEFAULT_COOLDOWN
                    self._state.cooldown_until[provider.id] = self._clock() + wait
            elif response.status_code in (401, 403):
                outcome = "auth_error"
            elif response.status_code >= 500:
                outcome = "server_error"
            else:
                outcome = "error"
                log.warning("AI provider %s returned %s: %s", provider.name,
                            response.status_code, response.text[:300])
        self.last_attempts.append(Attempt(provider.name, outcome))
        log.info("AI provider %s -> %s; trying next", provider.name, outcome)
        return outcome

    async def _unavailable_reason(self) -> str:
        providers = await list_providers(self._db)
        if not providers:
            return "هیچ سرویس هوش مصنوعی تنظیم نشده است (تنظیمات ← هوش مصنوعی)."
        if not any(p.usable for p in providers):
            return "کلید API سرویس‌ها روی این رایانه وارد نشده است."
        if not await self.is_online():
            return "اینترنت در دسترس نیست و مدل محلی (Ollama) تنظیم نشده است."
        return "همه سرویس‌ها موقتاً محدود شده‌اند. کمی بعد دوباره تلاش کنید."


_OUTCOME_TEXT = {
    "rate_limited": "محدودیت تعداد درخواست (429).",
    "timeout": "زمان انتظار به پایان رسید.",
    "unreachable": "سرور در دسترس نیست.",
    "server_error": "خطای سرور سرویس‌دهنده.",
    "auth_error": "کلید API نامعتبر است یا دسترسی ندارد.",
    "error": "پاسخ نامعتبر از سرویس.",
}


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return min(float(value), 600.0) if value else None
    except ValueError:
        return None
