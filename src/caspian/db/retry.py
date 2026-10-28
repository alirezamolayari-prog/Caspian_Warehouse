"""Retry a short write that collided with another PC or the background sync.

MariaDB answers such a collision with a deadlock (1213), a lock wait timeout (1205) or — since 11.6,
where `innodb_snapshot_isolation` is on by default — «Record has changed since last read» (1020).
Each is solved by running the whole transaction again, so `attempt` must open its own session.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError

from caspian.services.errors import ValidationError

log = logging.getLogger(__name__)

RETRIES = 10
CONFLICT_CODES = (1213, 1205, 1020)
BUSY_MESSAGE = "هم‌زمان کار دیگری روی همین اطلاعات انجام می‌شد و این کار انجام نشد؛ لطفاً دوباره تلاش کنید."


def is_conflict(exc: Exception, numbering: bool = False) -> bool:
    """A collision that is solved by simply trying again (QA round 2 #1)."""
    if numbering and isinstance(exc, IntegrityError):
        return True
    if isinstance(exc, StaleDataError):  # another PC changed the same row a moment earlier
        return True
    if isinstance(exc, OperationalError):
        orig = getattr(exc, "orig", None)
        code = orig.args[0] if orig is not None and getattr(orig, "args", None) else None
        return code in CONFLICT_CODES or "database is locked" in str(exc)
    return False


async def with_retries[T](attempt: Callable[[], Awaitable[T]], numbering: bool = False,
                          tries: int = RETRIES, message: str = BUSY_MESSAGE) -> T:
    for n in range(tries):
        try:
            return await attempt()
        except (IntegrityError, OperationalError, StaleDataError) as exc:
            if not is_conflict(exc, numbering):
                raise
            if n == tries - 1:
                log.warning("Giving up after %d conflicts: %s", tries, exc)
                raise ValidationError(message) from exc
            log.info("Concurrent write; retrying (%d): %s", n + 1, exc)
            await asyncio.sleep(random.uniform(0.01, 0.05) * (n + 1))
    raise AssertionError("unreachable")
