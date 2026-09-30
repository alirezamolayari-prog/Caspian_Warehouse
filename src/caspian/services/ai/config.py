"""AI provider settings. Definitions are shared (DB); API keys are per-PC (Credential Manager)."""

from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import func, select

from caspian.core.permissions import Perm
from caspian.core.secrets import delete_secret, get_secret, set_secret
from caspian.db.database import Database
from caspian.db.models import AIProvider, ProviderKind
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import NotFound, ValidationError


@dataclass(frozen=True)
class Preset:
    label: str
    base_url: str
    model: str
    needs_key: bool
    stt_model: str = ""


# Base URLs and default models checked against each provider's documentation on 2026-09-30.
# GitHub Models was requested too, but GitHub retired it on 2026-07-30 (no API left to call).
PRESETS: dict[ProviderKind, Preset] = {
    ProviderKind.GEMINI: Preset("Google Gemini (سطح رایگان)",
                                "https://generativelanguage.googleapis.com/v1beta/openai",
                                "gemini-3.8-flash", True),
    ProviderKind.OPENROUTER: Preset("OpenRouter (مدل‌های رایگان :free)", "https://openrouter.ai/api/v1",
                                    "openrouter/free", True),
    ProviderKind.CEREBRAS: Preset("Cerebras (سطح رایگان)", "https://api.cerebras.ai/v1", "gpt-oss-120b",
                                  True),
    ProviderKind.MISTRAL: Preset("Mistral (سطح رایگان محدود)", "https://api.mistral.ai/v1",
                                 "mistral-small-latest", True),
    ProviderKind.OPENAI: Preset("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini", True,
                                "whisper-1"),
    ProviderKind.GROQ: Preset("Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
                              True, "whisper-large-v3"),
    ProviderKind.HUGGINGFACE: Preset("Hugging Face", "https://router.huggingface.co/v1",
                                     "meta-llama/Llama-3.1-8B-Instruct", True),
    ProviderKind.OLLAMA: Preset("مدل محلی (Ollama / GGUF)", "http://localhost:11434/v1",
                                "qwen2.5:7b", False),
    ProviderKind.CUSTOM: Preset("سفارشی (سازگار با OpenAI)", "", "", False),
}


@dataclass(frozen=True)
class ProviderConfig:
    id: int
    name: str
    kind: ProviderKind
    base_url: str
    model: str
    priority: int
    enabled: bool
    timeout_seconds: int
    stt_model: str
    has_key: bool  # on this PC

    @property
    def needs_key(self) -> bool:
        return PRESETS[self.kind].needs_key

    @property
    def is_local(self) -> bool:
        return is_local_url(self.base_url)

    @property
    def usable(self) -> bool:
        return self.enabled and (self.has_key or not self.needs_key)


def is_local_url(url: str) -> bool:
    """This PC or the LAN (works offline, no key sent over the internet)."""
    host = urlparse(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith(("192.168.", "10.")) \
        or host.endswith(".local")


def _key_name(provider_id: int) -> str:
    return f"provider:{provider_id}"


def get_key(provider_id: int) -> str | None:
    return get_secret("ai", _key_name(provider_id))


def _to_config(p: AIProvider) -> ProviderConfig:
    return ProviderConfig(p.id, p.name, p.kind, p.base_url, p.model, p.priority, p.enabled,
                          p.timeout_seconds, p.stt_model, bool(get_key(p.id)))


async def list_providers(db: Database) -> list[ProviderConfig]:
    async with db.session() as s:
        rows = (await s.scalars(select(AIProvider).order_by(AIProvider.priority, AIProvider.id))).all()
        return [_to_config(p) for p in rows]


def _validate(name: str, base_url: str, model: str, timeout: int) -> None:
    if not name.strip():
        raise ValidationError("نام سرویس را وارد کنید.")
    url = urlparse(base_url.strip())
    if url.scheme not in ("http", "https") or not url.netloc:
        raise ValidationError("آدرس سرویس باید با http:// یا https:// شروع شود.")
    if url.scheme == "http" and not is_local_url(base_url):
        raise ValidationError("برای سرویس‌های اینترنتی از https استفاده کنید (کلید API رمزنگاری "
                              "نشده ارسال می‌شود).")
    if not model.strip():
        raise ValidationError("نام مدل را وارد کنید.")
    if not 3 <= timeout <= 300:
        raise ValidationError("زمان انتظار باید بین ۳ تا ۳۰۰ ثانیه باشد.")


async def save_provider(db: Database, actor: Actor, name: str, kind: ProviderKind, base_url: str,
                        model: str, timeout_seconds: int = 30, stt_model: str = "",
                        enabled: bool = True, api_key: str | None = None,
                        provider_id: int | None = None) -> int:
    """api_key: None = leave unchanged; "" = remove from this PC; otherwise store on this PC."""
    actor.require(Perm.AI_CONFIGURE)
    if actor.is_ai:
        raise ValidationError("دستیار هوشمند نمی‌تواند تنظیمات خودش را تغییر دهد.")
    _validate(name, base_url, model, timeout_seconds)
    kind = ProviderKind(kind)  # Qt combo boxes hand back plain strings
    async with db.session(actor.user_id) as s:
        if provider_id is None:
            top = await s.scalar(select(func.max(AIProvider.priority))) or 0
            provider = AIProvider(name=name.strip(), kind=kind, base_url=base_url.strip().rstrip("/"),
                                  model=model.strip(), priority=top + 10, enabled=enabled,
                                  timeout_seconds=timeout_seconds, stt_model=stt_model.strip())
            s.add(provider)
            await s.flush()
            action = "ai.provider_created"
        else:
            provider = await s.get(AIProvider, provider_id)
            if provider is None:
                raise NotFound("سرویس پیدا نشد.")
            provider.name, provider.kind = name.strip(), kind
            provider.base_url, provider.model = base_url.strip().rstrip("/"), model.strip()
            provider.timeout_seconds, provider.stt_model = timeout_seconds, stt_model.strip()
            provider.enabled = enabled
            action = "ai.provider_updated"
        # Never log the key itself.
        audit.record(s, actor, action, "ai_provider", provider.id,
                     {"name": provider.name, "kind": kind.value, "model": provider.model,
                      "key_changed": api_key is not None})
        pid = provider.id
    if api_key is not None:
        if api_key.strip():
            set_secret("ai", _key_name(pid), api_key.strip())
        else:
            delete_secret("ai", _key_name(pid))
    return pid


async def delete_provider(db: Database, actor: Actor, provider_id: int) -> None:
    actor.require(Perm.AI_CONFIGURE)
    async with db.session(actor.user_id) as s:
        provider = await s.get(AIProvider, provider_id)
        if provider is None:
            raise NotFound("سرویس پیدا نشد.")
        audit.record(s, actor, "ai.provider_deleted", "ai_provider", provider.id,
                     {"name": provider.name})
        await s.delete(provider)
    delete_secret("ai", _key_name(provider_id))


async def move_provider(db: Database, actor: Actor, provider_id: int, direction: int) -> None:
    """direction -1 = higher priority (tried earlier), +1 = lower."""
    actor.require(Perm.AI_CONFIGURE)
    async with db.session(actor.user_id) as s:
        rows = list((await s.scalars(select(AIProvider).order_by(AIProvider.priority,
                                                                 AIProvider.id))).all())
        index = next((i for i, p in enumerate(rows) if p.id == provider_id), None)
        if index is None:
            raise NotFound("سرویس پیدا نشد.")
        target = index + direction
        if 0 <= target < len(rows):
            rows[index], rows[target] = rows[target], rows[index]
        for i, p in enumerate(rows):
            p.priority = (i + 1) * 10
