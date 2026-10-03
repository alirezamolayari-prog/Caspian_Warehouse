"""Assistant chat history (QA round 2, feature B).

Compact rows (text truncated, never audio), kept for `ai_history_days` (default 30) and purged in
small batches. Each user sees only their own history; administrators (users.manage) may see
everyone's. The assistant itself never reads or deletes history.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select

from caspian.core.permissions import Perm
from caspian.core.text import normalize
from caspian.db.database import Database
from caspian.db.models import AppSetting, AssistantMessage, User
from caspian.services import audit
from caspian.services.actor import Actor
from caspian.services.errors import PermissionDenied, ValidationError

log = logging.getLogger(__name__)

RETENTION_KEY = "ai_history_days"
DEFAULT_DAYS = 30
MAX_TEXT = 2000
PURGE_BATCH = 500


@dataclass(frozen=True)
class HistoryRow:
    id: int
    user_id: int
    user: str
    conversation: str
    created_at: dt.datetime
    role: str
    text: str
    provider: str
    document_ids: list[int]
    batch_ids: list[int]


async def record(db: Database, user_id: int | None, conversation: str, role: str, text: str,
                 provider: str = "", document_ids: list[int] | None = None,
                 batch_ids: list[int] | None = None) -> None:
    """Store one line. Called by the app after a reply (never by the AI's tools)."""
    if user_id is None or not text:
        return
    async with db.session() as s:
        s.add(AssistantMessage(user_id=user_id, conversation=conversation, role=role,
                               created_at=dt.datetime.now(),  # local time, like the purge cutoff
                               text=text[:MAX_TEXT], provider=provider[:100],
                               document_ids=document_ids or None, batch_ids=batch_ids or None))


def _can_see_all(actor: Actor) -> bool:
    return actor.can(Perm.USERS_MANAGE)


async def list_history(db: Database, actor: Actor, user_id: int | None = None, query: str = "",
                       before_id: int | None = None, limit: int = 50) -> list[HistoryRow]:
    """Newest first, `limit` rows older than `before_id` (lazy loading). `user_id` None = own
    history, or everyone's for administrators."""
    actor.require_human("خواندن سوابق گفتگو")
    if not _can_see_all(actor):
        if user_id not in (None, actor.user_id):
            raise PermissionDenied("فقط سوابق گفتگوی خودتان را می‌توانید ببینید.")
        user_id = actor.user_id
    stmt = (select(AssistantMessage, func.coalesce(User.full_name, User.username), User.username)
            .join(User, User.id == AssistantMessage.user_id)
            .order_by(AssistantMessage.id.desc()).limit(min(limit, 200)))
    if user_id is not None:
        stmt = stmt.where(AssistantMessage.user_id == user_id)
    if before_id is not None:
        stmt = stmt.where(AssistantMessage.id < before_id)
    if query.strip():
        q = query.strip()
        stmt = stmt.where(or_(AssistantMessage.text.contains(q, autoescape=True),
                              AssistantMessage.text.contains(normalize(q), autoescape=True)))
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    return [HistoryRow(m.id, m.user_id, name or username, m.conversation, m.created_at, m.role, m.text,
                       m.provider, list(m.document_ids or []), list(m.batch_ids or []))
            for m, name, username in rows]


async def delete_own(db: Database, actor: Actor) -> int:
    actor.require_human("حذف سوابق گفتگو")
    async with db.session(actor.user_id) as s:
        count = (await s.execute(delete(AssistantMessage)
                                 .where(AssistantMessage.user_id == actor.user_id))).rowcount or 0
        audit.record(s, actor, "ai.history_deleted", details={"rows": count})
    return count


async def retention_days(db: Database) -> int:
    async with db.session() as s:
        row = await s.get(AppSetting, RETENTION_KEY)
    try:
        return max(1, int(row.value)) if row and row.value is not None else DEFAULT_DAYS
    except (TypeError, ValueError):
        return DEFAULT_DAYS


async def set_retention_days(db: Database, actor: Actor, days: int) -> None:
    actor.require(Perm.AI_CONFIGURE)
    actor.require_human("تغییر مدت نگهداری سوابق گفتگو")
    if not 1 <= days <= 365:
        raise ValidationError("مدت نگهداری باید بین ۱ تا ۳۶۵ روز باشد.")
    async with db.session(actor.user_id) as s:
        row = await s.get(AppSetting, RETENTION_KEY)
        if row is None:
            s.add(AppSetting(key=RETENTION_KEY, value=days))
        else:
            row.value = days
        audit.record(s, actor, "ai.history_retention", details={"days": days})


async def purge(db: Database, now: dt.datetime | None = None) -> int:
    """Delete rows older than the retention, in batches so a large backlog never blocks the app."""
    cutoff = (now or dt.datetime.now()) - dt.timedelta(days=await retention_days(db))
    total = 0
    while True:
        async with db.session() as s:
            ids = (await s.scalars(select(AssistantMessage.id).where(AssistantMessage.created_at < cutoff)
                                   .limit(PURGE_BATCH))).all()
            if not ids:
                break
            await s.execute(delete(AssistantMessage).where(AssistantMessage.id.in_(ids)))
        total += len(ids)
    if total:
        log.info("Purged %d assistant history rows older than %s", total, cutoff)
    return total
