import datetime as dt
import time

import pytest
from sqlalchemy import select

from caspian.core import security
from caspian.db.models import AuditLog, Item, Unit, User
from caspian.services import auth, protected, users
from caspian.services.actor import Actor
from caspian.services.errors import (
    ApprovalError,
    AuthenticationError,
    PermissionDenied,
    ValidationError,
)
from caspian.services.protected import ProtectedAction

ADMIN_PW = "Str0ngPass"
PIN = "4826"


async def _admin(db) -> Actor:
    """Log in as the default admin and complete first-login setup."""
    result = await auth.login(db, "admin", "admin")
    await auth.change_password(db, result.actor, "admin", ADMIN_PW)
    await auth.set_pin(db, result.actor, ADMIN_PW, PIN)
    return result.actor


async def _audit_actions(db) -> list[str]:
    async with db.session() as s:
        return list((await s.scalars(select(AuditLog.action).order_by(AuditLog.id))).all())


# ----- security primitives -----


def test_hash_and_verify():
    h = security.hash_secret("secret1")
    assert security.verify_secret(h, "secret1")
    assert not security.verify_secret(h, "secret2")
    assert not security.verify_secret(None, "x")
    assert not security.verify_secret("garbage", "x")


def test_policies():
    assert security.password_problem("abc") is not None
    assert security.password_problem("admin") is not None
    assert security.password_problem("reza123", "reza123") is not None
    assert security.password_problem("Anbar#2026") is None
    assert security.pin_problem("1234") is not None
    assert security.pin_problem("0000") is not None
    assert security.pin_problem("12a4") is not None
    assert security.pin_problem("۴۸۲۶") is None  # Persian digits accepted


# ----- login -----


async def test_default_admin_must_change_password(db):
    result = await auth.login(db, "  ADMIN ", "admin")
    assert result.actor.is_admin
    assert result.must_change_password
    assert result.needs_pin


async def test_default_admin_created_only_once(db):
    async with db.session() as s:
        assert not await auth.ensure_default_admin(s)
        assert len((await s.scalars(select(User))).all()) == 1


async def test_login_wrong_password_and_unknown_user(db):
    with pytest.raises(AuthenticationError):
        await auth.login(db, "admin", "nope")
    with pytest.raises(AuthenticationError):
        await auth.login(db, "ghost", "admin")
    assert (await _audit_actions(db)).count("auth.login_failed") == 2


async def test_lockout_after_repeated_failures(db):
    for _ in range(auth.MAX_FAILED_LOGINS - 1):
        with pytest.raises(AuthenticationError, match="نادرست"):
            await auth.login(db, "admin", "nope")
    with pytest.raises(AuthenticationError, match="قفل"):
        await auth.login(db, "admin", "nope")
    # Even the right password is refused while locked.
    with pytest.raises(AuthenticationError, match="قفل"):
        await auth.login(db, "admin", "admin")
    async with db.session() as s:
        user = await auth.get_user(s, "admin")
        user.locked_until = dt.datetime.now() - dt.timedelta(seconds=1)
    assert (await auth.login(db, "admin", "admin")).actor.username == "admin"


async def test_change_password_rules(db):
    actor = (await auth.login(db, "admin", "admin")).actor
    with pytest.raises(ValidationError):
        await auth.change_password(db, actor, "wrong", ADMIN_PW)
    with pytest.raises(ValidationError):
        await auth.change_password(db, actor, "admin", "short")
    await auth.change_password(db, actor, "admin", ADMIN_PW)
    result = await auth.login(db, "admin", ADMIN_PW)
    assert not result.must_change_password
    with pytest.raises(AuthenticationError):
        await auth.login(db, "admin", "admin")


async def test_set_pin(db):
    actor = await _admin(db)
    assert not (await auth.login(db, "admin", ADMIN_PW)).needs_pin
    with pytest.raises(ValidationError):
        await auth.set_pin(db, actor, "wrong-password", "4826")
    with pytest.raises(PermissionDenied):
        await auth.set_pin(db, actor.as_ai(), ADMIN_PW, "4826")


# ----- protected actions -----


async def test_approval_flow(db):
    actor = await _admin(db)
    assert await protected.list_approvers(db) == [("admin", "مدیر سیستم")]
    approval = await protected.approve(db, actor, ProtectedAction.CHANGE_ROLE, "admin", PIN)
    assert protected.consume(approval, ProtectedAction.CHANGE_ROLE, actor) == actor.user_id
    with pytest.raises(ApprovalError):  # single use
        protected.consume(approval, ProtectedAction.CHANGE_ROLE, actor)
    assert "approval.granted" in await _audit_actions(db)


async def test_approval_wrong_pin(db):
    actor = await _admin(db)
    with pytest.raises(ApprovalError, match="PIN"):
        await protected.approve(db, actor, ProtectedAction.MERGE_ITEMS, "admin", "9999")
    assert "approval.denied" in await _audit_actions(db)


async def test_approval_bound_to_action_and_actor(db):
    actor = await _admin(db)
    other = Actor(999, "x", "x", "storekeeper", frozenset())
    a1 = await protected.approve(db, actor, ProtectedAction.MERGE_ITEMS, "admin", PIN)
    with pytest.raises(ApprovalError):
        protected.consume(a1, ProtectedAction.CLOSE_FISCAL_YEAR, actor)
    a2 = await protected.approve(db, actor, ProtectedAction.MERGE_ITEMS, "admin", PIN)
    with pytest.raises(ApprovalError):
        protected.consume(a2, ProtectedAction.MERGE_ITEMS, other)


async def test_approval_expires(db, monkeypatch):
    actor = await _admin(db)
    approval = await protected.approve(db, actor, ProtectedAction.MERGE_ITEMS, "admin", PIN)
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + protected.APPROVAL_TTL_SECONDS + 1)
    with pytest.raises(ApprovalError, match="منقضی"):
        protected.consume(approval, ProtectedAction.MERGE_ITEMS, actor)


async def test_forged_approval_rejected(db):
    actor = await _admin(db)
    fake = protected.Approval("forged", ProtectedAction.CHANGE_ROLE, actor.user_id, 1,
                              time.monotonic())
    with pytest.raises(ApprovalError):
        protected.consume(fake, ProtectedAction.CHANGE_ROLE, actor)
    with pytest.raises(ApprovalError):
        protected.consume(None, ProtectedAction.CHANGE_ROLE, actor)


async def test_ai_can_never_get_or_use_approval(db):
    actor = await _admin(db)
    with pytest.raises(ApprovalError, match="دستیار"):
        await protected.approve(db, actor.as_ai(), ProtectedAction.CHANGE_ROLE, "admin", PIN)
    approval = await protected.approve(db, actor, ProtectedAction.CHANGE_ROLE, "admin", PIN)
    with pytest.raises(ApprovalError, match="دستیار"):
        protected.consume(approval, ProtectedAction.CHANGE_ROLE, actor.as_ai())
    assert "approval.refused_ai" in await _audit_actions(db)


async def test_non_admin_cannot_approve(db):
    admin = await _admin(db)
    await users.create_user(db, admin, "ali", "علی", "Ali#2026x", "manager")
    with pytest.raises(ApprovalError):
        await protected.approve(db, admin, ProtectedAction.MERGE_ITEMS, "ali", PIN)


# ----- user administration -----


async def test_create_user_and_first_login(db):
    admin = await _admin(db)
    row = await users.create_user(db, admin, "Reza", "رضا", "Reza#2026", "storekeeper")
    assert row.username == "reza" and row.role_code == "storekeeper"
    result = await auth.login(db, "reza", "Reza#2026")
    assert result.must_change_password and not result.needs_pin
    with pytest.raises(PermissionDenied):
        await users.list_users(db, result.actor)
    with pytest.raises(ValidationError):
        await users.create_user(db, admin, "reza", "", "Other#2026", "viewer")


async def test_creating_admin_requires_approval(db):
    admin = await _admin(db)
    with pytest.raises(ApprovalError):
        await users.create_user(db, admin, "boss", "", "Boss#2026", "admin")
    approval = await protected.approve(db, admin, ProtectedAction.CHANGE_ROLE, "admin", PIN)
    row = await users.create_user(db, admin, "boss", "", "Boss#2026", "admin", approval)
    assert row.role_code == "admin"


async def test_change_role_requires_approval_and_keeps_last_admin(db):
    admin = await _admin(db)
    row = await users.create_user(db, admin, "ali", "", "Ali#2026x", "viewer")
    with pytest.raises(ApprovalError):
        await users.change_role(db, admin, row.id, "manager", None)
    approval = await protected.approve(db, admin, ProtectedAction.CHANGE_ROLE, "admin", PIN)
    await users.change_role(db, admin, row.id, "manager", approval)
    assert (await auth.login(db, "ali", "Ali#2026x")).actor.role_code == "manager"

    approval = await protected.approve(db, admin, ProtectedAction.CHANGE_ROLE, "admin", PIN)
    with pytest.raises(ValidationError, match="حداقل یک مدیر"):
        await users.change_role(db, admin, admin.user_id, "viewer", approval)

    async with db.session() as s:
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "user.role_changed"))
        assert entry.approved_by_id == admin.user_id
        assert entry.details == {"from": "viewer", "to": "manager"}


async def test_deactivate_rules(db):
    admin = await _admin(db)
    row = await users.create_user(db, admin, "ali", "", "Ali#2026x", "viewer")
    with pytest.raises(ValidationError):
        await users.set_active(db, admin, admin.user_id, False)
    await users.set_active(db, admin, row.id, False)
    with pytest.raises(AuthenticationError, match="غیرفعال"):
        await auth.login(db, "ali", "Ali#2026x")


async def test_reset_password_forces_change_and_unlocks(db):
    admin = await _admin(db)
    row = await users.create_user(db, admin, "ali", "", "Ali#2026x", "viewer")
    for _ in range(auth.MAX_FAILED_LOGINS):
        with pytest.raises(AuthenticationError):
            await auth.login(db, "ali", "bad")
    await users.reset_password(db, admin, row.id, "Temp#2026")
    assert (await auth.login(db, "ali", "Temp#2026")).must_change_password


# ----- created_by stamping -----


async def test_created_by_is_stamped_automatically(db):
    admin = await _admin(db)
    async with db.session(admin.user_id) as s:
        unit = await s.scalar(select(Unit))
        s.add(Item(code="C1", name="صندلی", name_normalized="صندلی", base_unit_id=unit.id))
    async with db.session() as s:
        item = await s.scalar(select(Item).where(Item.code == "C1"))
        assert item.created_by_id == admin.user_id
