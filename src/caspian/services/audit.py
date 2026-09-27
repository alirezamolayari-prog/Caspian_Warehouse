"""Audit trail. Every security-relevant or data-changing operation writes a row here."""

import socket

from sqlalchemy.ext.asyncio import AsyncSession

from caspian.db.models import AuditLog
from caspian.services.actor import Actor

_MACHINE = socket.gethostname()[:128]


def record(
    session: AsyncSession,
    actor: Actor | None,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: dict | None = None,
    approved_by_id: int | None = None,
    user_id: int | None = None,
) -> AuditLog:
    """Add an audit row to the session (committed with the surrounding unit of work)."""
    if actor is not None and actor.is_ai:
        details = {**(details or {}), "via_ai": True}
    entry = AuditLog(
        user_id=user_id if user_id is not None else (actor.user_id if actor else None),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        approved_by_id=approved_by_id,
        machine=_MACHINE,
    )
    session.add(entry)
    return entry
