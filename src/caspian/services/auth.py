"""Login, password and PIN management."""

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.core import security
from caspian.db.database import Database
from caspian.db.models import Role, User
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import AuthenticationError, PermissionDenied, ValidationError

log = logging.getLogger(__name__)

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 5
BAD_CREDENTIALS = "نام کاربری یا رمز عبور نادرست است."


@dataclass(frozen=True)
class LoginResult:
    actor: Actor
    must_change_password: bool
    needs_pin: bool  # admin without a PIN can't approve protected actions yet


def normalize_username(username: str) -> str:
    return username.strip().lower()


def _now() -> dt.datetime:
    return dt.datetime.now()


async def get_user(session: AsyncSession, username: str) -> User | None:
    return await session.scalar(
        select(User).where(User.username == normalize_username(username))
    )


async def ensure_default_admin(session: AsyncSession) -> bool:
    """Create admin/admin (forced to change on first login) if there are no users."""
    if await session.scalar(select(User.id).limit(1)) is not None:
        return False
    role = await session.scalar(select(Role).where(Role.code == "admin"))
    session.add(User(
        username=security.DEFAULT_ADMIN_USERNAME,
        full_name="مدیر سیستم",
        password_hash=security.hash_secret(security.DEFAULT_ADMIN_PASSWORD),
        role_id=role.id,
        must_change_password=True,
    ))
    await session.flush()
    log.info("Created default admin account")
    return True


LOCKED_MESSAGE = f"به‌دلیل تلاش‌های ناموفق، حساب برای {LOCK_MINUTES} دقیقه قفل شد."


async def register_failure(session: AsyncSession, user: User) -> bool:
    """Count a failed password/PIN attempt; lock the account after too many.

    Returns True if this attempt locked the account.
    """
    user.failed_logins += 1
    if user.failed_logins >= MAX_FAILED_LOGINS:
        user.failed_logins = 0
        user.locked_until = _now() + dt.timedelta(minutes=LOCK_MINUTES)
        audit.record(session, None, "auth.locked", "user", user.id, user_id=user.id)
        return True
    return False


def check_not_locked(user: User) -> None:
    if user.locked_until and user.locked_until > _now():
        minutes = max(1, int((user.locked_until - _now()).total_seconds() // 60) + 1)
        raise AuthenticationError(f"حساب قفل است. حدود {minutes} دقیقه دیگر دوباره تلاش کنید.")


async def login(db: Database, username: str, password: str) -> LoginResult:
    error: str | None = None
    result: LoginResult | None = None
    # Failures must be committed (attempt counter, audit) before raising.
    async with db.session() as s:
        user = await get_user(s, username)
        if user is None:
            audit.record(s, None, "auth.login_failed", details={"username": username[:64]})
            error = BAD_CREDENTIALS
        else:
            check_not_locked(user)
            if not security.verify_secret(user.password_hash, password):
                locked = await register_failure(s, user)
                error = LOCKED_MESSAGE if locked else BAD_CREDENTIALS
                audit.record(s, None, "auth.login_failed", "user", user.id, user_id=user.id)
            elif not user.is_active:
                error = "این حساب کاربری غیرفعال است."
            else:
                if security.needs_rehash(user.password_hash):
                    user.password_hash = security.hash_secret(password)
                user.failed_logins = 0
                user.locked_until = None
                user.last_login_at = _now()
                actor = Actor.from_user(user)
                audit.record(s, actor, "auth.login", "user", user.id)
                result = LoginResult(
                    actor=actor,
                    must_change_password=user.must_change_password,
                    needs_pin=actor.is_admin and not user.pin_hash,
                )
    if error:
        raise AuthenticationError(error)
    return result


async def logout(db: Database, actor: Actor) -> None:
    async with db.session() as s:
        audit.record(s, actor, "auth.logout", "user", actor.user_id)


async def change_password(db: Database, actor: Actor, current: str, new: str) -> None:
    async with db.session(actor.user_id) as s:
        user = await s.get(User, actor.user_id)
        if not security.verify_secret(user.password_hash, current):
            raise ValidationError("رمز عبور فعلی نادرست است.")
        if problem := security.password_problem(new, user.username):
            raise ValidationError(problem)
        if new == current:
            raise ValidationError("رمز عبور جدید باید با رمز فعلی متفاوت باشد.")
        user.password_hash = security.hash_secret(new)
        user.must_change_password = False
        audit.record(s, actor, "auth.password_changed", "user", user.id)


async def set_pin(db: Database, actor: Actor, password: str, pin: str) -> None:
    """Admins set their own PIN, confirming with their password."""
    if actor.is_ai:
        raise PermissionDenied("دستیار هوشمند مجاز به این کار نیست.")
    if not actor.is_admin:
        raise PermissionDenied("فقط مدیران سیستم PIN دارند.")
    if problem := security.pin_problem(pin):
        raise ValidationError(problem)
    async with db.session(actor.user_id) as s:
        user = await s.get(User, actor.user_id)
        if not security.verify_secret(user.password_hash, password):
            raise ValidationError("رمز عبور نادرست است.")
        user.pin_hash = security.hash_secret(security.normalize_pin(pin))
        audit.record(s, actor, "auth.pin_set", "user", user.id)
