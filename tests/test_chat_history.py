"""QA round 2, feature B: assistant chat history with 30-day retention."""

import asyncio
import datetime as dt

import httpx
import pytest
import time_machine
from sqlalchemy import func, select

from caspian.db.models import AssistantMessage, ProviderKind
from caspian.services import auth, users
from caspian.services.ai import config, history
from caspian.services.ai.assistant import Assistant
from caspian.services.ai.gateway import Gateway
from caspian.services.errors import PermissionDenied, ValidationError


def _answer(text="۱۰ عدد موجود است."):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


@pytest.fixture
async def setup(db, admin):
    await config.save_provider(db, admin, "Local", ProviderKind.OLLAMA, "http://localhost:11434/v1", "q")
    await users.create_user(db, admin, "neda", "ندا", "Neda#2026", "storekeeper")
    neda = (await auth.login(db, "neda", "Neda#2026")).actor
    gw = Gateway(db, httpx.MockTransport(lambda r: _answer()), online_check=lambda: True)
    return {"neda": neda, "gw": gw}


async def _count(db) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(AssistantMessage))


async def test_each_exchange_is_saved_compactly(db, admin, setup):
    assistant = Assistant(db, setup["gw"], admin)
    reply = await assistant.send("از دریل چقدر داریم؟")
    for _ in range(100):
        if await _count(db) == 2:
            break
        await asyncio.sleep(0.02)
    rows = await history.list_history(db, admin)
    assert [(r.role, r.text) for r in rows] == [("assistant", reply.text), ("user", "از دریل چقدر داریم؟")]
    assert rows[0].provider == "Local" and rows[0].conversation == rows[1].conversation
    await history.record(db, admin.user_id, "c", "assistant", "x" * 10_000)
    assert len((await history.list_history(db, admin, limit=1))[0].text) == history.MAX_TEXT


async def test_send_does_not_wait_for_the_history_write(db, admin, setup, monkeypatch):
    gate = asyncio.Event()

    async def slow_record(*a, **k):
        await gate.wait()  # the history write hangs…

    monkeypatch.setattr(history, "record", slow_record)
    reply = await asyncio.wait_for(Assistant(db, setup["gw"], admin).send("سلام"), 5)
    assert reply.text  # …and the reply came back anyway
    gate.set()


async def test_users_see_only_their_own_admins_see_all(db, admin, setup):
    neda = setup["neda"]
    await history.record(db, admin.user_id, "a", "user", "دستور مدیر")
    await history.record(db, neda.user_id, "b", "user", "دستور ندا")
    assert [r.text for r in await history.list_history(db, neda)] == ["دستور ندا"]
    with pytest.raises(PermissionDenied):
        await history.list_history(db, neda, user_id=admin.user_id)
    assert {r.text for r in await history.list_history(db, admin)} == {"دستور مدیر", "دستور ندا"}
    assert [r.text for r in await history.list_history(db, admin, user_id=neda.user_id)] == ["دستور ندا"]
    assert [r.user for r in await history.list_history(db, admin, query="ندا")] == ["ندا"]
    with pytest.raises(PermissionDenied):  # the assistant never reads history
        await history.list_history(db, admin.as_ai())
    with pytest.raises(PermissionDenied):
        await history.delete_own(db, neda.as_ai())
    assert await history.delete_own(db, neda) == 1
    assert [r.text for r in await history.list_history(db, admin)] == ["دستور مدیر"]


async def test_lazy_paging(db, admin):
    for i in range(7):
        await history.record(db, admin.user_id, "c", "user", f"دستور {i}")
    first = await history.list_history(db, admin, limit=3)
    second = await history.list_history(db, admin, before_id=first[-1].id, limit=3)
    assert [r.text for r in first + second] == [f"دستور {i}" for i in range(6, 0, -1)]


async def test_old_rows_are_purged_after_the_retention(db, admin):
    with time_machine.travel(dt.datetime(2026, 9, 1, 10), tick=False):
        await history.record(db, admin.user_id, "old", "user", "قدیمی")
    with time_machine.travel(dt.datetime(2026, 9, 25, 10), tick=False):
        await history.record(db, admin.user_id, "new", "user", "جدید")
    with time_machine.travel(dt.datetime(2026, 10, 3, 10), tick=False):
        assert await history.purge(db) == 1
        assert [r.text for r in await history.list_history(db, admin)] == ["جدید"]
        await history.set_retention_days(db, admin, 7)
        assert await history.retention_days(db) == 7
        assert await history.purge(db) == 1
        assert await history.list_history(db, admin) == []
    with pytest.raises(ValidationError):
        await history.set_retention_days(db, admin, 0)
    with pytest.raises(PermissionDenied):
        await history.set_retention_days(db, admin.as_ai(), 10)


async def test_purge_works_in_batches(db, admin, monkeypatch):
    monkeypatch.setattr(history, "PURGE_BATCH", 3)
    with time_machine.travel(dt.datetime(2026, 1, 1, 10), tick=False):
        for i in range(8):
            await history.record(db, admin.user_id, "c", "user", str(i))
    assert await history.purge(db) == 8 and await _count(db) == 0


async def test_scheduler_runs_housekeeping(db, admin):
    from caspian.services.messaging import Messenger
    from caspian.services.scheduler import SchedulerRunner

    with time_machine.travel(dt.datetime(2026, 1, 1, 10), tick=False):
        await history.record(db, admin.user_id, "c", "user", "قدیمی")
    runner = SchedulerRunner(db, Messenger(db), interval=0.05)
    runner.start()
    try:
        for _ in range(200):
            if await _count(db) == 0:
                break
            await asyncio.sleep(0.02)
    finally:
        await runner.stop()
    assert await _count(db) == 0
