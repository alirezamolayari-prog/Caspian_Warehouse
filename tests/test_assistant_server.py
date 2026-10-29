"""Phase 5: «دستیار کاسپین» through Caspian Server (/ai/chat, /ai/limits/me): the app runs the tools
itself, sends no system prompt, uses the signed-in person's account, shows every error code in
Persian, and only uses local AI providers when the admin turned that fallback on."""

import json

import pytest

from caspian.services.ai.assistant import MAX_TOOL_ROUNDS, Assistant
from caspian.services.ai.gateway import AIUnavailable, Gateway
from caspian.services.ai.server_gateway import (
    AI_TEXT,
    NOT_SIGNED_IN,
    UNREACHABLE,
    CaspianGateway,
    local_fallback,
    set_local_fallback,
)
from caspian.services.errors import PermissionDenied
from caspian.services.server_link import connection
from request_helpers import SERVER_URL


@pytest.fixture
async def shared(db, admin, fake_server):
    """The admin (no personal account) uses the shared «warehouse» account."""
    await connection.save_connection(db, admin, SERVER_URL, "warehouse", "warehouse123")
    return CaspianGateway(db, Gateway(db), lambda: admin, fake_server.transport())


async def test_tool_loop_runs_tools_here_and_sends_no_system_prompt(db, admin, fake_server, shared):
    fake_server.ai_tool = ("search_items", {"query": "دریل"})
    fake_server.ai_answer = "موجودی دریل را بررسی کردم."
    reply = await Assistant(db, shared, admin).send("دریل چقدر داریم؟")
    assert reply.text == "موجودی دریل را بررسی کردم." and reply.provider == "سرور کاسپین"
    first, second = fake_server.ai_requests
    assert first["app"] == "warehouse" and first["capability"] == "assistant"
    assert all(m["role"] != "system" for m in first["messages"])
    assert len(first["tools"]) <= 32 and any(t["function"]["name"] == "search_items" for t in first["tools"])
    tool_message = second["messages"][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_1"
    json.loads(tool_message["content"])  # the result of the tool, run on this PC
    assert shared.limits == {"used_today": 200, "limit_today": 20000}
    assert (await shared.limits_me())["remaining"] == 19800


@pytest.mark.parametrize(("status", "code"), [
    (429, "ai_user_daily_limit"), (429, "ai_global_daily_limit"), (422, "ai_input_too_long"),
    (422, "ai_input_empty"), (503, "ai_unavailable"), (400, "ai_unknown_capability"),
    (403, "ai_app_not_allowed"),
])
async def test_every_error_code_is_a_persian_sentence(db, admin, fake_server, shared, status, code):
    fake_server.ai_refuse = (status, code)
    with pytest.raises(AIUnavailable) as exc:
        await shared.chat([{"role": "user", "content": "سلام"}])
    expected = UNREACHABLE if status >= 500 else None  # any 5xx: «not reachable now»
    if code == "ai_app_not_allowed":
        assert "اجازه" in exc.value.message or "نقش" in exc.value.message
    else:
        assert exc.value.message == (expected or AI_TEXT[code])
    assert not any("a" <= ch.lower() <= "z" for ch in exc.value.message)


async def test_password_change_required_and_not_signed_in(db, admin, fake_server, shared):
    fake_server.users["warehouse"]["must_change"] = True
    with pytest.raises(AIUnavailable) as exc:
        await shared.chat([{"role": "user", "content": "سلام"}])
    assert "رمز" in exc.value.message
    nobody = CaspianGateway(db, Gateway(db), lambda: admin.as_ai(), fake_server.transport())
    await connection.save_connection(db, admin, SERVER_URL, "", None)  # no shared account any more
    with pytest.raises(AIUnavailable) as exc:
        await nobody.chat([{"role": "user", "content": "سلام"}])
    assert exc.value.message == NOT_SIGNED_IN


async def test_personal_account_is_used(db, fake_server):
    from caspian.services.server_link import session

    fake_server.add_person("09121234567", "anbar-1403", "رضا انباردار")
    reza = (await session.sign_in(db, "09121234567", "anbar-1403", transport=fake_server.transport())).actor
    gateway = CaspianGateway(db, Gateway(db), lambda: reza, fake_server.transport())
    await gateway.chat([{"role": "user", "content": "سلام"}])
    assert fake_server.ai_used == 100 and "m09121234567" in fake_server.logins


async def test_local_providers_only_when_the_admin_turns_them_on(db, admin, fake_server, shared):
    class Local:
        called = 0

        async def available(self):
            return True

        async def chat(self, *args, **kwargs):
            Local.called += 1
            from caspian.services.ai.gateway import ChatResult

            return ChatResult("محلی", [], "local", "m", {"role": "assistant", "content": "محلی"})

    gateway = CaspianGateway(db, Local(), lambda: admin, fake_server.transport())
    fake_server.online = False
    assert not await local_fallback(db)  # off by default
    with pytest.raises(AIUnavailable):
        await gateway.chat([{"role": "user", "content": "سلام"}])
    assert Local.called == 0
    await set_local_fallback(db, admin, True)
    assert (await gateway.chat([{"role": "user", "content": "سلام"}])).content == "محلی"
    with pytest.raises(PermissionDenied):
        await set_local_fallback(db, admin.as_ai(), False)


def test_round_limit():
    assert MAX_TOOL_ROUNDS == 5


async def test_page_shows_todays_quota(qtbot, themes, db, admin, fake_server, shared):
    from caspian.core.settings import Settings
    from caspian.db.database import DbConfig
    from caspian.ui.app_context import AppContext
    from caspian.ui.assistant_page import AssistantPage
    from helpers import wait_until

    ctx = AppContext(db, DbConfig(), Settings(), themes, admin)
    ctx.assistant_ai = shared
    fake_server.ai_used = 1540
    page = AssistantPage(ctx)
    qtbot.addWidget(page)
    page.update_status()
    assert await wait_until(lambda: page.status.text() == "سهم امروز شما: ۱٬۵۴۰ از ۲۰٬۰۰۰")
