"""User administration."""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.core import security
from caspian.core.permissions import Perm
from caspian.db.database import Database
from caspian.db.models import Role, User
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.auth import normalize_username
from caspian.services.errors import NotFound, ValidationError
from caspian.services.protected import Approval, ProtectedAction, consume


@dataclass(frozen=True)
class UserRow:
    id: int
    username: str
    full_name: str
    role_code: str
    role_name: str
    is_active: bool
    has_pin: bool
    last_login_at: dt.datetime | None


@dataclass(frozen=True)
class RoleRow:
    code: str
    name: str


def _row(u: User) -> UserRow:
    return UserRow(u.id, u.username, u.full_name, u.role.code, u.role.name, u.is_active,
                   bool(u.pin_hash), u.last_login_at)


async def list_users(db: Database, actor: Actor) -> list[UserRow]:
    actor.require(Perm.USERS_MANAGE)
    async with db.session() as s:
        users = (await s.scalars(select(User).order_by(User.username))).all()
        return [_row(u) for u in users]


async def list_roles(db: Database) -> list[RoleRow]:
    async with db.session() as s:
        roles = (await s.scalars(select(Role).order_by(Role.id))).all()
        return [RoleRow(r.code, r.name) for r in roles]


async def _get_user(s: AsyncSession, user_id: int) -> User:
    user = await s.get(User, user_id)
    if user is None:
        raise NotFound("کاربر پیدا نشد.")
    return user


async def _get_role(s: AsyncSession, code: str) -> Role:
    role = await s.scalar(select(Role).where(Role.code == code))
    if role is None:
        raise ValidationError("نقش نامعتبر است.")
    return role


async def _active_admin_count(s: AsyncSession) -> int:
    return await s.scalar(
        select(func.count()).select_from(User).join(Role, User.role_id == Role.id)
        .where(Role.code == "admin", User.is_active)
    )


def _validate_username(username: str) -> str:
    username = normalize_username(username)
    if not (2 <= len(username) <= 64) or not all(c.isalnum() or c in "._-" for c in username):
        raise ValidationError("نام کاربری باید ۲ تا ۶۴ کاراکتر و فقط شامل حروف، عدد، . _ - باشد.")
    return username


TAKEN = "این نام کاربری قبلاً ثبت شده است."


async def username_taken(db: Database, username: str, except_user_id: int | None = None) -> bool:
    stmt = select(User.id).where(User.username == normalize_username(username))
    if except_user_id is not None:
        stmt = stmt.where(User.id != except_user_id)
    async with db.session() as s:
        return await s.scalar(stmt) is not None


async def create_user(
    db: Database, actor: Actor, username: str, full_name: str, password: str, role_code: str,
    approval: Approval | None = None,
) -> UserRow:
    """New users must change their password at first login.

    Creating an admin is a role grant, so it needs a CHANGE_ROLE approval.
    """
    actor.require(Perm.USERS_MANAGE)
    username = _validate_username(username)
    # A taken name is reported first (before password rules), and never burns an approval.
    if await username_taken(db, username):
        raise ValidationError(TAKEN)
    if problem := security.password_problem(password, username):
        raise ValidationError(problem)
    approver_id = consume(approval, ProtectedAction.CHANGE_ROLE, actor) if role_code == "admin" \
        else None
    async with db.session(actor.user_id) as s:
        if await s.scalar(select(User.id).where(User.username == username)):
            raise ValidationError(TAKEN)
        role = await _get_role(s, role_code)
        user = User(
            username=username, full_name=full_name.strip(),
            password_hash=security.hash_secret(password), role_id=role.id,
            must_change_password=True, created_by_id=actor.user_id,
        )
        s.add(user)
        await s.flush()
        await s.refresh(user, ["role"])
        audit.record(s, actor, "user.created", "user", user.id,
                     {"username": username, "role": role_code}, approved_by_id=approver_id)
        return _row(user)


async def update_user(db: Database, actor: Actor, user_id: int, full_name: str) -> None:
    actor.require(Perm.USERS_MANAGE)
    async with db.session(actor.user_id) as s:
        user = await _get_user(s, user_id)
        user.full_name = full_name.strip()
        audit.record(s, actor, "user.updated", "user", user.id, {"full_name": user.full_name})


async def rename_user(db: Database, actor: Actor, user_id: int, new_username: str) -> None:
    """Change the login name (unique, audited). The password and everything else stay."""
    actor.require(Perm.USERS_MANAGE)
    username = _validate_username(new_username)
    async with db.session(actor.user_id) as s:
        user = await _get_user(s, user_id)
        if user.username == username:
            return
        if await s.scalar(select(User.id).where(User.username == username, User.id != user_id)):
            raise ValidationError(TAKEN)
        old, user.username = user.username, username
        audit.record(s, actor, "user.renamed", "user", user.id, {"from": old, "to": username})


async def set_active(db: Database, actor: Actor, user_id: int, active: bool) -> None:
    actor.require(Perm.USERS_MANAGE)
    if not active and user_id == actor.user_id:
        raise ValidationError("نمی‌توانید حساب خودتان را غیرفعال کنید.")
    async with db.session(actor.user_id) as s:
        user = await _get_user(s, user_id)
        if not active and user.role.code == "admin" and user.is_active \
                and await _active_admin_count(s) <= 1:
            raise ValidationError("حداقل یک مدیر سیستم فعال باید باقی بماند.")
        user.is_active = active
        audit.record(s, actor, "user.activated" if active else "user.deactivated", "user", user.id)


async def reset_password(db: Database, actor: Actor, user_id: int, new_password: str) -> None:
    """Admin sets a temporary password; the user must change it at next login."""
    actor.require(Perm.USERS_MANAGE)
    async with db.session(actor.user_id) as s:
        user = await _get_user(s, user_id)
        if problem := security.password_problem(new_password, user.username):
            raise ValidationError(problem)
        user.password_hash = security.hash_secret(new_password)
        user.must_change_password = True
        user.failed_logins = 0
        user.locked_until = None
        audit.record(s, actor, "user.password_reset", "user", user.id)


async def change_role(
    db: Database, actor: Actor, user_id: int, role_code: str, approval: Approval | None
) -> None:
    actor.require(Perm.USERS_MANAGE)
    approver_id = consume(approval, ProtectedAction.CHANGE_ROLE, actor)
    async with db.session(actor.user_id) as s:
        user = await _get_user(s, user_id)
        old = user.role.code
        if old == role_code:
            return
        if old == "admin" and user.is_active and await _active_admin_count(s) <= 1:
            raise ValidationError("حداقل یک مدیر سیستم فعال باید باقی بماند.")
        role = await _get_role(s, role_code)
        user.role_id = role.id
        audit.record(s, actor, "user.role_changed", "user", user.id,
                     {"from": old, "to": role_code}, approved_by_id=approver_id)
