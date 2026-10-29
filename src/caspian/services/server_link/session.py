"""Signing in with a personal Caspian Server account (mobile + own password).

- Online: the server checks the password; «who am I» gives the account id and Persian full name; the
  local user for that account is found (or made, with the minimal role «کاربر تازه» — the local admin
  gives permissions on «کاربران»). The access token (7 days) is kept in Windows Credential Manager so
  this person's queued requests and messages are sent under their own name even after they log out.
  A strong argon2 hash of the password is kept in the existing `password_hash` column for offline
  login. «مرا به خاطر بسپار» also keeps the password in Credential Manager (to renew the token).
- Server unreachable: someone who signed in on this PC before may sign in with the same password
  (checked against the hash); the status bar says «آفلاین» and requests queue as usual.
- Local usernames (not a mobile number) are the emergency fallback: admins only, once the server is
  set up. Nothing about passwords or tokens is ever logged or stored in the database in clear.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from caspian.core import security
from caspian.core.persian import normalize_mobile
from caspian.core.secrets import delete_secret, get_secret, set_secret
from caspian.db.database import Database
from caspian.db.models import AppSetting, Role, User
from caspian.services import audit, auth
from caspian.services.actor import Actor
from caspian.services.errors import AuthenticationError
from caspian.services.server_link import connection
from caspian.services.server_link.accounts import DEFAULT_URL, AuthApi, AuthError
from caspian.services.server_link.client import ServerClient
from caspian.services.server_link.errors import ServerUnreachable

log = logging.getLogger(__name__)

NEW_STAFF_ROLE = "new_staff"
TOKEN_KIND = "server-token"
PASSWORD_KIND = "server"
WRONG_ROLE = "این حساب در سرور نقش «انبار» ندارد. از مدیر سرور بخواهید نقش «انبار» را به آن بدهد."
LOCAL_DISABLED = "حساب شما در این برنامه غیرفعال شده است؛ با مدیر تماس بگیرید."
USE_MOBILE = "با شماره موبایل و رمز حساب سرور وارد شوید. (ورود با نام کاربری فقط برای مدیر است.)"
OFFLINE_UNKNOWN = ("سرور در دسترس نیست و این شماره تا حالا روی این رایانه وارد نشده است؛ "
                   "پس از وصل شدن اینترنت دوباره امتحان کنید.")


@dataclass(frozen=True)
class PersonLogin:
    actor: Actor
    offline: bool = False
    needs_pin: bool = False
    must_change_local: bool = False  # a local admin's first login (admin/admin): change it here


@dataclass(frozen=True)
class MustChange:
    """The server wants a new password before anything else (`/auth/change-password`)."""

    mobile: str
    token: str


async def server_url(db: Database) -> str:
    async with db.session() as s:
        row = await s.get(AppSetting, connection.KEY)
    value = row.value if row and isinstance(row.value, dict) else {}
    return str(value.get("url") or DEFAULT_URL).rstrip("/")


async def server_mode(db: Database) -> bool:
    """Personal accounts are the normal sign-in once a Caspian Server is set up on this database."""
    async with db.session() as s:
        row = await s.get(AppSetting, connection.KEY)
    return bool(row and isinstance(row.value, dict) and row.value.get("url"))


def _host(url: str) -> str:
    return urlsplit(url).netloc


def token_name(server_user_id: int, url: str) -> str:
    return f"{server_user_id}@{_host(url)}"


def get_token(server_user_id: int, url: str) -> str | None:
    return get_secret(TOKEN_KIND, token_name(server_user_id, url))


def save_token(server_user_id: int, url: str, token: str) -> None:
    set_secret(TOKEN_KIND, token_name(server_user_id, url), token)


def forget_token(server_user_id: int, url: str) -> None:
    delete_secret(TOKEN_KIND, token_name(server_user_id, url))


def remembered_password(mobile: str, url: str) -> str | None:
    return get_secret(PASSWORD_KIND, f"{mobile}@{_host(url)}")


async def is_server_user(db: Database, actor: Actor) -> bool:
    async with db.session() as s:
        user = await s.get(User, actor.user_id) if actor.user_id else None
    return bool(user and user.server_user_id)


async def sign_in(db: Database, login: str, password: str, remember: bool = False,
                  transport: httpx.AsyncBaseTransport | None = None) -> PersonLogin | MustChange:
    """The login window's «ورود». `login` is a mobile number (personal account) or, for the
    emergency fallback, a local admin username."""
    mobile = normalize_mobile(login)
    if mobile is None:
        return await _local_admin(db, login, password)
    url = await server_url(db)
    api = AuthApi(url, transport)
    try:
        answer = await api.login(mobile, password)
    except ServerUnreachable:
        return await _offline(db, mobile, password)
    if answer.must_change_password:
        return MustChange(mobile, answer.access_token)
    return await _finish(db, api, url, mobile, password, answer.access_token, remember)


async def change_and_sign_in(db: Database, pending: MustChange, current: str, new: str,
                             remember: bool = False,
                             transport: httpx.AsyncBaseTransport | None = None) -> PersonLogin:
    url = await server_url(db)
    api = AuthApi(url, transport)
    token = await api.change_password(pending.token, current, new)
    return await _finish(db, api, url, pending.mobile, new, token, remember)


async def change_password(db: Database, actor: Actor, current: str, new: str,
                          transport: httpx.AsyncBaseTransport | None = None) -> None:
    """«تغییر رمز» from the user menu, for a person with a server account."""
    url = await server_url(db)
    async with db.session() as s:
        user = await s.get(User, actor.user_id)
        server_user_id, mobile = user.server_user_id, user.mobile
    token = get_token(server_user_id, url) if server_user_id else None
    api = AuthApi(url, transport)
    if token is None:  # e.g. signed in offline: get a fresh token with the current password
        token = (await api.login(mobile, current)).access_token
    new_token = await api.change_password(token, current, new)
    save_token(server_user_id, url, new_token)
    if remembered_password(mobile, url) is not None:
        set_secret(PASSWORD_KIND, f"{mobile}@{_host(url)}", new)
    async with db.session(actor) as s:
        user = await s.get(User, actor.user_id)
        user.password_hash = security.hash_secret(new)
        audit.record(s, actor, "auth.password_changed", "user", user.id, {"via": "server"})


async def _finish(db: Database, api: AuthApi, url: str, mobile: str, password: str, token: str,
                  remember: bool) -> PersonLogin:
    me = await api.me(token)
    if me.get("role") not in ("warehouse", "admin"):
        raise AuthError(WRONG_ROLE, "forbidden")
    server_user_id = int(me["id"])
    full_name = (me.get("full_name") or "").strip()[:150]
    async with db.session() as s:
        user = await s.scalar(select(User).where(User.server_user_id == server_user_id))
        if user is None:
            user = await s.scalar(select(User).where(User.mobile == mobile))
        if user is None:
            role = await s.scalar(select(Role).where(Role.code == NEW_STAFF_ROLE))
            user = User(username=await _free_username(s, f"m{mobile}"), full_name=full_name,
                        password_hash=security.hash_secret(password), role_id=role.id, role=role)
            s.add(user)
        elif not user.is_active:
            raise AuthenticationError(LOCAL_DISABLED)
        user.server_user_id, user.mobile = server_user_id, mobile
        user.server_full_name = full_name
        if full_name:
            user.full_name = full_name
        user.password_hash = security.hash_secret(password)
        user.failed_logins, user.locked_until = 0, None
        user.must_change_password = False
        user.last_login_at = dt.datetime.now()
        await s.flush()
        actor = Actor.from_user(user)
        audit.record(s, actor, "auth.login", "user", user.id, {"via": "server"})
        needs_pin = actor.is_admin and not user.pin_hash
    save_token(server_user_id, url, token)
    if remember:
        set_secret(PASSWORD_KIND, f"{mobile}@{_host(url)}", password)
    else:
        delete_secret(PASSWORD_KIND, f"{mobile}@{_host(url)}")
    return PersonLogin(actor, needs_pin=needs_pin)


async def _free_username(s, base: str) -> str:
    name, n = base, 1
    while await s.scalar(select(User.id).where(User.username == name)):
        n += 1
        name = f"{base}-{n}"
    return name


async def _offline(db: Database, mobile: str, password: str) -> PersonLogin:
    error = None
    async with db.session() as s:
        user = await s.scalar(select(User).where(User.mobile == mobile, User.server_user_id.is_not(None)))
        if user is None:
            raise ServerUnreachable(OFFLINE_UNKNOWN)
        auth.check_not_locked(user)
        if not security.verify_secret(user.password_hash, password):
            locked = await auth.register_failure(s, user)
            error = auth.LOCKED_MESSAGE if locked else "شماره موبایل یا رمز عبور اشتباه است."
        elif not user.is_active:
            error = LOCAL_DISABLED
        else:
            user.failed_logins, user.locked_until = 0, None
            user.last_login_at = dt.datetime.now()
            actor = Actor.from_user(user)
            audit.record(s, actor, "auth.login", "user", user.id, {"via": "offline"})
    if error:
        raise AuthenticationError(error)
    return PersonLogin(actor, offline=True, needs_pin=actor.is_admin and not user.pin_hash)


async def _local_admin(db: Database, username: str, password: str) -> PersonLogin:
    result = await auth.login(db, username, password)
    if await server_mode(db) and not result.actor.is_admin:
        raise AuthenticationError(USE_MOBILE)
    return PersonLogin(result.actor, needs_pin=result.needs_pin,
                       must_change_local=result.must_change_password)


async def client_for(db: Database, actor: Actor, transport: httpx.AsyncBaseTransport | None = None,
                     timeout: float = 60.0) -> ServerClient | None:
    """A client for calls made *for* this person (the assistant): their own saved login, else — for
    a local-only user — the old shared account. None: nobody can call the server for them."""
    if actor.is_ai or actor.user_id is None:
        return None
    async with db.session() as s:
        user = await s.get(User, actor.user_id)
    if user is not None and user.server_user_id is not None:
        url = await server_url(db)
        token = get_token(user.server_user_id, url)
        password = (remembered_password(user.mobile, url) or "") if user.mobile else ""
        if not token and not password:
            return None
        return ServerClient(url, user.mobile or "", password, transport, timeout, token,
                            lambda new, uid=user.server_user_id: save_token(uid, url, new))
    conn = await connection.get_connection(db)
    if conn is None:
        return None
    return ServerClient(conn.url, conn.username, connection.get_password(conn) or "", transport, timeout)
