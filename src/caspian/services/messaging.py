"""Outgoing messages: Telegram bot and SMTP email.

Recipients and servers are configured by an admin (shared in the DB); the bot token and
SMTP password are kept per PC in Windows Credential Manager. The AI and scheduled tasks
can only send to these pre-configured recipients — never to arbitrary addresses.
"""

import asyncio
import logging
import smtplib
import ssl
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage

import httpx

from caspian.core.permissions import Perm
from caspian.core.secrets import get_secret, set_secret
from caspian.db.database import Database
from caspian.db.models import AppSetting
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import ServiceError, ValidationError

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class SendError(ServiceError):
    pass


@dataclass
class TelegramConfig:
    enabled: bool = False
    chat_ids: list[str] = field(default_factory=list)


@dataclass
class EmailConfig:
    enabled: bool = False
    host: str = ""
    port: int = 587
    security: str = "starttls"  # starttls | ssl | none
    username: str = ""
    sender: str = ""
    recipients: list[str] = field(default_factory=list)


@dataclass
class MessagingConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    email: EmailConfig = field(default_factory=EmailConfig)

    @property
    def channels(self) -> list[str]:
        out = []
        if self.telegram.enabled and self.telegram.chat_ids:
            out.append("telegram")
        if self.email.enabled and self.email.recipients:
            out.append("email")
        return out


async def load_config(db: Database) -> MessagingConfig:
    async with db.session() as s:
        row = await s.get(AppSetting, "messaging")
    data = row.value if row and isinstance(row.value, dict) else {}
    return MessagingConfig(TelegramConfig(**data.get("telegram", {})),
                           EmailConfig(**data.get("email", {})))


async def save_config(db: Database, actor: Actor, config: MessagingConfig,
                      telegram_token: str | None = None, smtp_password: str | None = None) -> None:
    actor.require(Perm.SETTINGS_EDIT)
    if actor.is_ai:
        raise ValidationError("دستیار هوشمند نمی‌تواند گیرندگان پیام را تغییر دهد.")
    if config.email.security not in ("starttls", "ssl", "none"):
        raise ValidationError("نوع امنیت ایمیل نامعتبر است.")
    config.telegram.chat_ids = [c.strip() for c in config.telegram.chat_ids if c.strip()]
    config.email.recipients = [r.strip() for r in config.email.recipients if r.strip()]
    for r in config.email.recipients:
        if "@" not in r:
            raise ValidationError(f"نشانی ایمیل نامعتبر: {r}")
    async with db.session(actor.user_id) as s:
        row = await s.get(AppSetting, "messaging")
        value = asdict(config)
        if row is None:
            s.add(AppSetting(key="messaging", value=value))
        else:
            row.value = value
        audit.record(s, actor, "settings.messaging_updated", details={
            "telegram_chats": len(config.telegram.chat_ids),
            "email_recipients": len(config.email.recipients)})
    if telegram_token is not None:
        set_secret("msg", "telegram_token", telegram_token.strip())
    if smtp_password is not None:
        set_secret("msg", "smtp_password", smtp_password)


# ----- transports -----


class Messenger:
    def __init__(self, db: Database, transport: httpx.AsyncBaseTransport | None = None,
                 smtp_factory=None) -> None:
        self._db = db
        self._transport = transport
        self._smtp_factory = smtp_factory  # for tests

    async def send(self, text: str, attachment: tuple[str, bytes, str] | None = None,
                   channels: list[str] | None = None) -> list[str]:
        """Send to all configured recipients of the given channels. Returns channels used."""
        config = await load_config(self._db)
        wanted = [c for c in (channels or config.channels) if c in config.channels]
        if not wanted:
            raise SendError("هیچ کانال پیام‌رسانی (تلگرام یا ایمیل) پیکربندی و فعال نشده است.")
        errors = []
        for channel in wanted:
            try:
                if channel == "telegram":
                    await self._telegram(config.telegram, text, attachment)
                else:
                    await self._email(config.email, text, attachment)
            except SendError as exc:
                errors.append(exc.message)
        if len(errors) == len(wanted):
            raise SendError(" | ".join(errors))
        return wanted

    async def _telegram(self, config: TelegramConfig, text: str,
                        attachment: tuple[str, bytes, str] | None) -> None:
        token = get_secret("msg", "telegram_token")
        if not token:
            raise SendError("توکن ربات تلگرام روی این رایانه وارد نشده است.")
        async with httpx.AsyncClient(base_url=f"{TELEGRAM_API}/bot{token}", timeout=60,
                                     transport=self._transport) as client:
            for chat_id in config.chat_ids:
                try:
                    if attachment:
                        name, data, mime = attachment
                        response = await client.post("/sendDocument", data={
                            "chat_id": chat_id, "caption": text[:1000]},
                            files={"document": (name, data, mime)})
                    else:
                        response = await client.post("/sendMessage", json={
                            "chat_id": chat_id, "text": text[:4000]})
                except httpx.HTTPError as exc:
                    raise SendError(f"ارسال تلگرام ممکن نشد: {type(exc).__name__}") from exc
                if response.status_code != 200:
                    raise SendError(f"تلگرام خطا داد ({response.status_code}).")

    async def _email(self, config: EmailConfig, text: str,
                     attachment: tuple[str, bytes, str] | None) -> None:
        password = get_secret("msg", "smtp_password") or ""
        message = EmailMessage()
        message["Subject"] = text.splitlines()[0][:150] if text else "انبار کاسپین"
        message["From"] = config.sender or config.username
        message["To"] = ", ".join(config.recipients)
        message.set_content(text)
        if attachment:
            name, data, mime = attachment
            maintype, subtype = mime.split("/", 1)
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

        def deliver() -> None:
            factory = self._smtp_factory
            context = ssl.create_default_context()
            if factory is None:
                factory = smtplib.SMTP_SSL if config.security == "ssl" else smtplib.SMTP
            kwargs = {"context": context} if config.security == "ssl" and self._smtp_factory is None \
                else {}
            with factory(config.host, config.port, timeout=30, **kwargs) as smtp:
                if config.security == "starttls":
                    smtp.starttls(context=context)
                if config.username:
                    smtp.login(config.username, password)
                smtp.send_message(message)

        try:
            await asyncio.to_thread(deliver)
        except (OSError, smtplib.SMTPException) as exc:
            raise SendError(f"ارسال ایمیل ممکن نشد: {exc}") from exc
