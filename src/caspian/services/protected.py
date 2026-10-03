"""Protected actions: confirmation + Admin PIN, enforced in the service layer.

Flow: the UI shows a confirmation and asks an admin for their PIN, calling
`approve()`. That returns a single-use `Approval` bound to the action and the
requesting user. The service that performs the action calls `consume()` first;
without a fresh, matching approval it refuses. AI actors can never obtain one.
"""

import enum
import secrets
import time
from dataclasses import dataclass

from sqlalchemy import select

from caspian.core import security
from caspian.core.permissions import Perm
from caspian.db.database import Database
from caspian.db.models import Role, User
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.auth import (
    LOCKED_MESSAGE,
    check_not_locked,
    normalize_username,
    register_failure,
)
from caspian.services.errors import ApprovalError

APPROVAL_TTL_SECONDS = 120


class ProtectedAction(enum.StrEnum):
    DEACTIVATE_ITEM = "deactivate_item"
    DELETE_ITEM = "delete_item"
    IMPORT_OVERWRITE = "import_overwrite"
    MERGE_ITEMS = "merge_items"
    RESTORE_BACKUP = "restore_backup"
    CHANGE_ROLE = "change_role"
    CLOSE_FISCAL_YEAR = "close_fiscal_year"
    STOCKTAKE_OVERRIDE = "stocktake_override"
    CREATE_SIMILAR_ITEM = "create_similar_item"


# action -> (Persian title, permission the approving admin must hold)
ACTION_INFO: dict[ProtectedAction, tuple[str, Perm]] = {
    ProtectedAction.DEACTIVATE_ITEM: ("غیرفعال‌سازی کالا", Perm.ITEMS_DEACTIVATE),
    ProtectedAction.DELETE_ITEM: ("حذف کالا", Perm.ITEMS_DEACTIVATE),
    ProtectedAction.IMPORT_OVERWRITE: ("بازنویسی کالاها هنگام ورود اطلاعات", Perm.IMPORT_OVERWRITE),
    ProtectedAction.MERGE_ITEMS: ("ادغام کالاهای تکراری", Perm.ITEMS_MERGE),
    ProtectedAction.RESTORE_BACKUP: ("بازیابی نسخه پشتیبان", Perm.BACKUP_RESTORE),
    ProtectedAction.CHANGE_ROLE: ("تغییر نقش کاربر", Perm.ROLES_CHANGE),
    ProtectedAction.CLOSE_FISCAL_YEAR: ("بستن سال مالی", Perm.YEAR_CLOSE),
    ProtectedAction.CREATE_SIMILAR_ITEM: ("ایجاد کالای مشابه", Perm.ITEMS_EDIT),
    ProtectedAction.STOCKTAKE_OVERRIDE: ("تغییر موجودی کالای در حال انبارگردانی", Perm.STOCKTAKE_APPROVE),
}


@dataclass(frozen=True)
class Approval:
    token: str
    action: ProtectedAction
    actor_id: int | None
    approver_id: int
    issued_at: float


_outstanding: dict[str, Approval] = {}


def _purge_expired() -> None:
    cutoff = time.monotonic() - APPROVAL_TTL_SECONDS
    for token in [t for t, a in _outstanding.items() if a.issued_at < cutoff]:
        del _outstanding[token]


async def list_approvers(db: Database) -> list[tuple[str, str]]:
    """(username, display name) of active admins who have set a PIN."""
    async with db.session() as s:
        rows = await s.execute(
            select(User.username, User.full_name)
            .join(Role, User.role_id == Role.id)
            .where(Role.code == "admin", User.is_active, User.pin_hash.is_not(None))
            .order_by(User.username)
        )
        return [(u, n or u) for u, n in rows.all()]


async def approve(
    db: Database, actor: Actor, action: ProtectedAction, approver_username: str, pin: str,
    details: dict | None = None,
) -> Approval:
    title, perm = ACTION_INFO[action]
    if actor.is_ai:
        async with db.session() as s:
            audit.record(s, actor, "approval.refused_ai", details={"action": action.value})
        raise ApprovalError("دستیار هوشمند مجاز به انجام عملیات حساس نیست.")

    error: str | None = None
    approval: Approval | None = None
    async with db.session() as s:
        approver = await s.scalar(
            select(User).where(User.username == normalize_username(approver_username))
        )
        base = {"action": action.value, **(details or {})}
        if approver is None or not approver.is_active or approver.role.code != "admin":
            error = "تأییدکننده باید یک مدیر سیستم فعال باشد."
        elif perm.value not in {p.permission for p in approver.role.permissions}:
            error = "این مدیر مجوز تأیید این عملیات را ندارد."
        elif not approver.pin_hash:
            error = "این مدیر هنوز PIN تنظیم نکرده است."
        else:
            check_not_locked(approver)
            if not security.verify_secret(approver.pin_hash, security.normalize_pin(pin)):
                locked = await register_failure(s, approver)
                error = LOCKED_MESSAGE if locked else "کد PIN نادرست است."
            else:
                approver.failed_logins = 0
                approval = Approval(
                    token=secrets.token_urlsafe(24),
                    action=action,
                    actor_id=actor.user_id,
                    approver_id=approver.id,
                    issued_at=time.monotonic(),
                )
        audit.record(
            s, actor, "approval.denied" if error else "approval.granted",
            details={**base, "title": title},
            approved_by_id=None if error else approver.id,
        )
    if error:
        raise ApprovalError(error)
    _purge_expired()
    _outstanding[approval.token] = approval
    return approval


def consume(approval: Approval | None, action: ProtectedAction, actor: Actor) -> int:
    """Validate and burn an approval. Returns the approving admin's user id."""
    if actor.is_ai:
        raise ApprovalError("دستیار هوشمند مجاز به انجام عملیات حساس نیست.")
    if approval is None:
        raise ApprovalError("این عملیات نیاز به تأیید مدیر با PIN دارد.")
    _purge_expired()
    registered = _outstanding.pop(approval.token, None)
    if registered is None or registered != approval:
        raise ApprovalError("تأیید نامعتبر یا منقضی شده است. دوباره تأیید کنید.")
    if registered.action != action or registered.actor_id != actor.user_id:
        raise ApprovalError("این تأیید برای عملیات دیگری صادر شده است.")
    return registered.approver_id
