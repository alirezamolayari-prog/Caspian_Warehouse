"""The assistant and goods requests: it lists statuses and never sends, approves, prints or posts.
(It asks for goods only by preparing an issue slip, see test_issue_approval.py.)"""

import json

import pytest
from sqlalchemy import func, select

from caspian.db.models import ServerOutbox
from caspian.services.ai.tools import ToolContext, run_tool
from caspian.services.errors import PermissionDenied
from caspian.services.server_link import goods
from request_helpers import request_input


async def _outbox(db) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(ServerOutbox))



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
