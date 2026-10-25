"""Where Caspian Server is and which account this app uses (admin only).

URL and username are shared settings (`app_settings`); the password stays in this PC's Windows
Credential Manager (`core.secrets`, kind "server"), never in the database or a file.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from caspian.core.secrets import get_secret, set_secret
from caspian.db.database import Database
from caspian.db.models import AppSetting
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import PermissionDenied, ValidationError
from caspian.services.server_link.client import ServerClient

KEY = "server.connection"
SECRET_KIND = "server"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class ServerConnection:
    url: str
    username: str

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc

    @property
    def secret_name(self) -> str:
        return f"{self.username}@{self.host}"

    @property
    def insecure(self) -> bool:
        """Plain http to another machine: the password would cross the network unencrypted."""
        parts = urlsplit(self.url)
        return parts.scheme == "http" and (parts.hostname or "") not in LOCAL_HOSTS


def clean_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValidationError("نشانی سرور باید با http:// یا https:// شروع شود (مثلاً https://caspian.example.ir).")
    return url


async def get_connection(db: Database) -> ServerConnection | None:
    async with db.session() as s:
        row = await s.get(AppSetting, KEY)
    value = row.value if row and isinstance(row.value, dict) else {}
    if not value.get("url") or not value.get("username"):
        return None
    return ServerConnection(str(value["url"]), str(value["username"]))


def get_password(connection: ServerConnection) -> str | None:
    return get_secret(SECRET_KIND, connection.secret_name)


async def save_connection(db: Database, actor: Actor, url: str, username: str,
                          password: str | None) -> ServerConnection:
    """`password` None keeps the stored one (it must exist for this username and host)."""
    if actor.is_ai:
        raise PermissionDenied("دستیار هوشمند اجازه تغییر اتصال سرور را ندارد.")
    if not actor.is_admin:
        raise PermissionDenied("فقط مدیر سیستم می‌تواند اتصال به سرور را تنظیم کند.")
    connection = ServerConnection(clean_url(url), username.strip())
    if not connection.username:
        raise ValidationError("نام کاربری سرور را وارد کنید.")
    if password is None and not get_password(connection):
        raise ValidationError("رمز عبور حساب سرور را وارد کنید.")
    async with db.session(actor) as s:
        row = await s.get(AppSetting, KEY)
        value = {"url": connection.url, "username": connection.username}
        if row is None:
            s.add(AppSetting(key=KEY, value=value))
        else:
            row.value = value
        audit.record(s, actor, "server.connection_saved",
                     details={**value, "password_changed": password is not None})
    if password is not None:
        set_secret(SECRET_KIND, connection.secret_name, password)
    return connection


async def test_connection(url: str, username: str, password: str,
                          transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Log in and ask «who am I»; returns a Persian confirmation or raises a ServerError."""
    client = ServerClient(clean_url(url), username.strip(), password, transport)
    await client.health()
    me = await client.call("GET", "/auth/me")
    if me.get("role") not in ("warehouse", "admin"):
        raise ValidationError("این حساب در سرور نقش «انبار» ندارد؛ حساب warehouse را وارد کنید.")
    return f"اتصال برقرار شد: {me.get('full_name') or me.get('username')} ({me.get('role')})"
