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

from caspian.core.text import ltr
from caspian.db.database import Database
from caspian.db.models import ProviderKind
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
    # ok | rate_limited | timeout | unreachable | server_error | auth_error (401) | forbidden (403) | error
    outcome: str
    detail: str = ""  # the server's explanation, key removed (log and settings test only)
    status: int | None = None


@dataclass(frozen=True)
class ModelInfo:
    id: str
    free: bool


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
        return self._http(provider.kind, provider.base_url, get_key(provider.id),
                          timeout or provider.timeout_seconds)

    def _http(self, kind: ProviderKind, base_url: str, key: str | None,
              timeout: float) -> httpx.AsyncClient:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if kind == ProviderKind.OPENROUTER:  # optional attribution, see openrouter.ai/docs
            headers["HTTP-Referer"] = APP_URL
            headers["X-Title"] = "Caspian Warehouse"
        return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout,
                                 transport=self._transport)

    async def list_models(self, kind: ProviderKind, base_url: str, key: str | None) -> list[ModelInfo]:
        """GET {base_url}/models for the settings dialog; free models are flagged (OpenRouter)."""
        try:
            async with self._http(kind, base_url.strip().rstrip("/"), key, 20) as client:
                response = await client.get("/models")
        except httpx.HTTPError as exc:
            raise AIUnavailable(f"دریافت فهرست مدل‌ها ممکن نشد: {_OUTCOME_TEXT['unreachable']}") from exc
        if response.status_code != 200:
            outcome = _status_outcome(response.status_code)
            raise AIUnavailable(_explain(outcome, _server_detail(response, key), response.status_code))
        try:
            rows = response.json().get("data") or response.json().get("models") or []
        except (ValueError, AttributeError):
            rows = []
        models = {}
        for row in rows:
            model_id = str(row.get("id") or row.get("name") or "").removeprefix("models/")
            if model_id:
                pricing = row.get("pricing") or {}
                free = model_id.endswith(":free") or model_id == "openrouter/free" or (
                    bool(pricing) and all(str(pricing.get(k, "1")) in ("0", "0.0")
                                          for k in ("prompt", "completion")))
                models[model_id] = ModelInfo(model_id, free)
        return sorted(models.values(), key=lambda m: m.id)

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
        raise AIUnavailable("هیچ‌یک از سرویس‌های هوش مصنوعی پاسخ نداد:\n" + self.attempts_text() +
                            "\nکمی بعد دوباره تلاش کنید "
                            "یا تنظیمات را بررسی کنید.")

    def attempts_text(self) -> str:
        """One short Persian line per provider tried in the last call (#5)."""
        lines = []
        for attempt in self.last_attempts:
            # Persian reason only; the raw server text (often English JSON) stays in the log (round 2 #3).
            reason = _status_text(attempt.status, attempt.outcome).split("\n")[0]
            lines.append(f"• {ltr(attempt.provider)}: {reason}")
        return "\n".join(lines)

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
        last = self.last_attempts[-1] if self.last_attempts else None
        return False, _explain(outcome, last.detail if last else "", last.status if last else None), elapsed

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
            else:
                outcome = _status_outcome(response.status_code)
                detail = _server_detail(response, get_key(provider.id))
                log.warning("AI provider %s returned %s: %s", provider.name,
                            response.status_code, detail)
                self.last_attempts.append(Attempt(provider.name, outcome, detail, response.status_code))
                log.info("AI provider %s -> %s; trying next", provider.name, outcome)
                return outcome
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


_STATUS_TEXT = {
    500: "خطای داخلی سرور سرویس (500).",
    502: "سرور سرویس موقتاً در دسترس نیست (502).",
    503: "سرور سرویس شلوغ است (503)؛ کمی بعد دوباره امتحان کنید.",
    504: "سرور سرویس دیر پاسخ داد (504).",
}


def _status_text(status: int | None, outcome: str) -> str:
    if status in _STATUS_TEXT:
        return _STATUS_TEXT[status]
    if outcome == "server_error" and status:
        return f"خطای سرور سرویس‌دهنده ({status})."
    return _OUTCOME_TEXT.get(outcome, outcome)


_OUTCOME_TEXT = {
    "rate_limited": "محدودیت تعداد درخواست (429).",
    "timeout": "زمان انتظار به پایان رسید.",
    "unreachable": "سرور در دسترس نیست.",
    "server_error": "خطای سرور سرویس‌دهنده.",
    "auth_error": "کلید API نامعتبر است (401). کلید را دوباره از سایت سرویس کپی کنید.",
    "forbidden": "دسترسی رد شد (403): معمولاً یعنی دسترسی از منطقه شما مسدود است؛ VPN را روشن کنید. "
                 "ممکن است کلید هم مجوز این مدل را نداشته باشد.",
    "not_found": "آدرس یا نام مدل پیدا نشد (404). آدرس API و نام مدل را بررسی کنید.",
    "error": "پاسخ نامعتبر از سرویس.",
}
APP_URL = "https://github.com/alirezamolayari-prog/Caspian_Warehouse"


def _status_outcome(status: int) -> str:
    if status == 401:
        return "auth_error"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    return "server_error" if status >= 500 else "error"


def _message_in(data) -> str | None:
    """The human message inside an error body: {"error": {"message": …}}, [{"error": …}], …"""
    if isinstance(data, list):
        return next((m for m in map(_message_in, data) if m), None)
    if isinstance(data, dict):
        if isinstance(data.get("message"), str):
            return data["message"]
        error = data.get("error")
        return error if isinstance(error, str) else _message_in(error)
    return None


def _server_detail(response: httpx.Response, key: str | None) -> str:
    """The server's own explanation (JSON error message or text), never including the key."""
    try:
        data = response.json()
        text = _message_in(data) or str(data)
    except ValueError:
        text = response.text
    text = " ".join(str(text or "").split())[:300]
    if key:
        text = text.replace(key, "***")
    return text


def _explain(outcome: str, detail: str, status: int | None = None) -> str:
    """For «تست اتصال» / model list: Persian reason plus the server's own short message."""
    text = _status_text(status, outcome) if outcome in _OUTCOME_TEXT or status else "خطا"
    return f"{text}\nپاسخ سرور: {ltr(detail)}" if detail else text  # English text isolated (#20)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return min(float(value), 600.0) if value else None
    except ValueError:
        return None
