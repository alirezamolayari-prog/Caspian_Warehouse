"""Live access check (test report issues 1 + 2).

An `Actor` is a snapshot taken at login. Every unit of work opened for a user
(`db.session(actor)`) first looks the user up again: an account deactivated meanwhile, or whose role
was changed or lost permissions, is refused at once, so an open window on another PC can't keep
posting. The UI listens (`add_listener`) and sends such a session back to the login screen.
"""

import logging
import types
import weakref
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.db.database import Database
from caspian.db.models import Role, RolePermission, User
from caspian.services.errors import ServiceError

log = logging.getLogger(__name__)


class ReloginRequired(ServiceError):
    """The signed-in session is no longer valid; the user has to sign in again."""


class AccountDisabled(ReloginRequired):
    def __init__(self) -> None:
        super().__init__("حساب کاربری شما غیرفعال شده است و این کار انجام نشد. برای ادامه با مدیر سیستم "
                         "تماس بگیرید.")


class SessionOutdated(ReloginRequired):
    def __init__(self) -> None:
        super().__init__("نقش یا مجوزهای شما تغییر کرده است و این کار انجام نشد. لطفاً دوباره وارد شوید.")


_listeners: list[Callable[[], Callable | None]] = []  # each returns the callback (None once gone)


def add_listener(callback: Callable[[ReloginRequired], None]) -> None:
    """`callback(error)` runs whenever a session is refused (held weakly: windows come and go)."""
    if isinstance(callback, types.MethodType):  # a window's method: don't keep the window alive
        _listeners.append(weakref.WeakMethod(callback))
    else:
        _listeners.append(lambda: callback)


def remove_listener(callback: Callable[[ReloginRequired], None]) -> None:
    _listeners[:] = [r for r in _listeners if r() is not None and r() != callback]


def _notify(error: ReloginRequired) -> ReloginRequired:
    for ref in list(_listeners):
        callback = ref()
        if callback is None:
            _listeners.remove(ref)
            continue
        try:
            callback(error)
        except Exception:
            log.exception("Relogin listener failed")
    return error


async def problem_in(s: AsyncSession, actor) -> ReloginRequired | None:
    """What is wrong with this signed-in actor now (None = still valid). No user = the system."""
    if actor.user_id is None:
        return None
    row = (await s.execute(select(User.is_active, User.role_id, Role.code)
                           .join(Role, Role.id == User.role_id).where(User.id == actor.user_id))).first()
    if row is None or not row.is_active:
        return AccountDisabled()
    live = set((await s.scalars(select(RolePermission.permission)
                                .where(RolePermission.role_id == row.role_id))).all())
    if row.code != actor.role_code or not set(actor.permissions) <= live:
        return SessionOutdated()
    return None


async def check_in(s: AsyncSession, actor) -> None:
    """Database hook: run at the start of every `db.session(actor)`."""
    error = await problem_in(s, actor)
    if error is not None:
        log.warning("Refused stale session of user %s: %s", actor.user_id, type(error).__name__)
        raise _notify(error)


async def ensure(db: Database, actor) -> None:
    """The same check for work that doesn't run inside one `db.session(actor)` (backup, restore)."""
    async with db.session() as s:
        await check_in(s, actor)


async def validate(db: Database, actor) -> ReloginRequired | None:
    """Read-only check for the UI's periodic test of an idle session."""
    async with db.session() as s:
        return await problem_in(s, actor)


Database.actor_check = staticmethod(check_in)
