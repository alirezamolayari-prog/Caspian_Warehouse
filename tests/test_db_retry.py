"""Phase 1.2: MariaDB 1020 «Record has changed since last read» (innodb_snapshot_isolation, on by
default since 11.6) and deadlocks are retried; the sync and the UI writing the same request at once
never surface as an error."""

import pytest
from sqlalchemy.exc import OperationalError

from caspian.db import retry
from caspian.db.models import GoodsRequest
from caspian.services.errors import ValidationError
from caspian.services.server_link import goods
from request_helpers import request_input


def _conflict(code: int = 1020) -> OperationalError:
    return OperationalError("UPDATE goods_requests SET unseen=%s", {}, Exception(code, "Record has changed"))


async def test_conflicts_are_retried_then_succeed():
    calls = []

    async def attempt():
        calls.append(1)
        if len(calls) < 3:
            raise _conflict(1020 if len(calls) == 1 else 1213)
        return "ok"

    assert await retry.with_retries(attempt, tries=3) == "ok" and len(calls) == 3


async def test_other_errors_are_not_retried_and_giving_up_is_persian():
    async def broken():
        raise _conflict(1146)  # table doesn't exist: not a collision

    with pytest.raises(OperationalError):
        await retry.with_retries(broken)

    async def always():
        raise _conflict()

    with pytest.raises(ValidationError) as exc:
        await retry.with_retries(always, tries=2)
    assert exc.value.message == retry.BUSY_MESSAGE


async def test_mark_seen_survives_a_concurrent_update(linked, admin, monkeypatch):
    """The owner approved while the details were open: the sync wrote the row in between."""
    db = linked["db"]
    request_id = await goods.create_request(db, admin, request_input(linked))
    async with db.session() as s:
        (await s.get(GoodsRequest, request_id)).unseen = True
    real_session = db.session
    raised = []

    def session(*args, **kwargs):
        cm = real_session(*args, **kwargs)
        if raised:
            return cm

        class Conflicting:
            async def __aenter__(self):
                self.s = await cm.__aenter__()
                return self.s

            async def __aexit__(self, *exc):
                if exc[0] is None:
                    raised.append(1)
                    err = _conflict()
                    await cm.__aexit__(type(err), err, None)
                    raise err
                return await cm.__aexit__(*exc)

        return Conflicting()

    monkeypatch.setattr(db, "session", session)
    await goods.mark_seen(db, admin, request_id)
    monkeypatch.setattr(db, "session", real_session)
    assert raised
    async with db.session() as s:
        assert not (await s.get(GoodsRequest, request_id)).unseen
