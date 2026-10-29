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
from caspian.services.server_link.client import TIMEOUT_SECONDS, ServerClient
from caspian.services.server_link.errors import ServerAccountProblem

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


async def get_url(db: Database) -> str | None:
    """The saved server address (also without a shared account)."""
    async with db.session() as s:
        row = await s.get(AppSetting, KEY)
    value = row.value if row and isinstance(row.value, dict) else {}
    return str(value["url"]) if value.get("url") else None


def get_password(connection: ServerConnection) -> str | None:
    return get_secret(SECRET_KIND, connection.secret_name)


async def save_connection(db: Database, actor: Actor, url: str, username: str,
                          password: str | None) -> ServerConnection:
    """`password` None keeps the stored one (it must exist for this username and host). An empty
    `username` saves only the address: no shared account, everyone signs in with their own."""
    if actor.is_ai:
        raise PermissionDenied("دستیار کاسپین اجازه تغییر اتصال سرور را ندارد.")
    if not actor.is_admin:
        raise PermissionDenied("فقط مدیر سیستم می‌تواند اتصال به سرور را تنظیم کند.")
    connection = ServerConnection(clean_url(url), username.strip())
    if connection.username and password is None and not get_password(connection):
        raise ValidationError("رمز عبور حساب سرور را وارد کنید.")
    async with db.session(actor) as s:
        row = await s.get(AppSetting, KEY)
        value = {"url": connection.url, "username": connection.username}  # "" = no shared account
        if row is None:
            s.add(AppSetting(key=KEY, value=value))
        else:
            row.value = value
        audit.record(s, actor, "server.connection_saved",
                     details={**value, "password_changed": password is not None})
    if password is not None and connection.username:
        set_secret(SECRET_KIND, connection.secret_name, password)
    return connection


async def test_connection(url: str, username: str, password: str,
                          transport: httpx.AsyncBaseTransport | None = None,
                          timeout: float = TIMEOUT_SECONDS) -> str:
    """Log in *and* make one authorised call («who am I»), so a password that must be changed or an
    account that isn't approved is reported here and not first by the sync. Returns a Persian
    confirmation or raises a ServerError with the specific reason."""
    client = ServerClient(clean_url(url), username.strip(), password, transport, timeout)
    if not username.strip():  # no shared account: only check that the server answers
        await client.health()
        return "سرور در دسترس است. هر کارمند با شماره موبایل و رمز خودش وارد می‌شود."
    me = await client.call("GET", "/auth/me")
    if client.must_change_password or me.get("must_change_password"):
        raise ServerAccountProblem("password_change_required", status=403)
    if me.get("role") not in ("warehouse", "admin"):
        raise ValidationError("این حساب در سرور نقش «انبار» ندارد. از مدیر سرور بخواهید نقش «انبار» را "
                              "به آن بدهد.")
    name = me.get("full_name") or username.strip()
    return f"اتصال برقرار شد و سرور حساب «{name}» را پذیرفت."
