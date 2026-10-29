"""The assistant helps with goods requests: it prepares a DRAFT (the person sends it with «بله،
بفرست»), asks instead of guessing, lists statuses — and never sends, approves, prints or posts."""

import json

import httpx
import pytest
from sqlalchemy import func, select

from caspian.core.settings import Settings
from caspian.db.database import DbConfig
from caspian.db.models import ProviderKind, RequestStatus, ServerOutbox
from caspian.services.ai import config
from caspian.services.ai.gateway import Gateway
from caspian.services.ai.tools import ToolContext, run_tool
from caspian.services.errors import PermissionDenied
from caspian.services.server_link import goods
from caspian.ui.app_context import AppContext
from caspian.ui.assistant_page import AssistantPage
from helpers import wait_until
from request_helpers import request_input
from test_assistant import call, scripted


def _args(linked, **changes) -> dict:
    return {"person": "علی رضایی", "location": linked["location"].code,
            "lines": [{"code": "1024", "qty": 20}], "note": "فوری", **changes}


async def _tool(linked, admin, **changes) -> dict:
    ctx = ToolContext(linked["db"], admin.as_ai(), [])
    return json.loads(await run_tool(ctx, "create_goods_request", _args(linked, **changes)))


async def _outbox(db) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(ServerOutbox))


async def test_assistant_only_prepares_a_draft(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    result = await _tool(linked, admin)
    assert result["status"] == "DRAFT" and result["needs_confirmation"]
    assert "علی رضایی" in result["summary"] and "کیسه زباله" in result["summary"]
    [row] = await goods.list_requests(db, admin)
    assert row.status == RequestStatus.DRAFT and row.status_name == "پیش‌نویس دستیار"
    assert await _outbox(db) == 0
    await engine.tick(admin)
    assert not server.requests  # nothing reaches the owners before the person's «بله»

    with pytest.raises(PermissionDenied):
        await goods.confirm_assistant_request(db, admin.as_ai(), row.id)
    await goods.confirm_assistant_request(db, admin, row.id)
    await engine.tick(admin)
    assert len(server.requests) == 1


async def test_assistant_cannot_send_or_chat_on_its_own(linked, admin):
    db = linked["db"]
    request_id = await goods.create_request(db, admin.as_ai(), request_input(linked))  # AI → draft only
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(ServerOutbox)) == 0
    for action in (lambda: goods.discard_assistant_request(db, admin.as_ai(), request_id),
                   lambda: goods.queue_message(db, admin.as_ai(), request_id, "سلام"),
                   lambda: goods.resubmit_request(db, admin.as_ai(), request_id, request_input(linked))):
        with pytest.raises(PermissionDenied):
            await action()
    await goods.discard_assistant_request(db, admin, request_id)
    assert not await goods.list_requests(db, admin)


async def test_assistant_asks_instead_of_guessing(linked, admin):
    assert (await _tool(linked, admin, person="حسن ناشناس"))["needs_choice"] == "person"
    assert (await _tool(linked, admin, location=""))["needs_choice"] == "location"
    assert (await _tool(linked, admin, lines=[{"code": "1024"}]))["needs_choice"] == "qty"
    assert (await _tool(linked, admin, lines=[{"name": "چیزی که نیست", "qty": 1}]))["needs_choice"] == "item"
    assert not await goods.list_requests(linked["db"], admin)


async def test_list_tool_shows_status_never_who_decided(linked, admin):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    await goods.create_request(db, admin, request_input(linked))
    await engine.tick(admin)
    server.reject(1, "زیاد است")
    await engine.tick(admin)
    ctx = ToolContext(db, admin.as_ai(), [])
    [row] = json.loads(await run_tool(ctx, "list_goods_requests", {}))
    assert row["status"] == "رد شد" and row["rejection_reason"] == "زیاد است"
    assert not any("decided" in key or "owner" in key for key in row)


async def test_chat_card_sends_after_yes(qtbot, themes, linked, admin, monkeypatch):
    db, server, engine = linked["db"], linked["server"], linked["engine"]
    monkeypatch.setattr(config, "get_secret", lambda k, n: None)
    await config.save_provider(db, admin, "Local", ProviderKind.OLLAMA, "http://localhost:11434/v1", "q")
    handler, _seen = scripted(
        {"content": None, "tool_calls": [call("create_goods_request", **_args(linked))]},
        {"content": "پیش‌نویس درخواست آماده است؛ برای ارسال، دکمه را بزنید."})
    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    ctx.ai = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    ctx.assistant_ai = ctx.ai
    page = AssistantPage(ctx)
    qtbot.addWidget(page)
    page.resize(900, 700)
    page.show()
    await page.send("برای علی رضایی ۲۰ تا کیسه زباله درخواست بده")
    [card] = page.pending_cards()
    assert card.yes.text() == "بله، بفرست" and "کیسه زباله" in card.text.text()
    card.yes.click()
    assert await wait_until(lambda: not page.pending_cards())
    [row] = await goods.list_requests(db, admin)
    assert row.status == RequestStatus.QUEUED
    await engine.tick(admin)
    assert len(server.requests) == 1
    assert "صف ارسال" in page.transcript.toPlainText()
