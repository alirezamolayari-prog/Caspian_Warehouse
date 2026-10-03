import json
from decimal import Decimal

import httpx
import pytest

from caspian.db.models import DocType, LineStatus, ProviderKind
from caspian.services import imports, items, master
from caspian.services.ai import config
from caspian.services.ai.assistant import Assistant, text_to_draft
from caspian.services.ai.gateway import AIUnavailable, Gateway
from caspian.services.ai.tools import TOOLS, ToolContext, available_tools, run_tool
from caspian.services.items import ItemInput


@pytest.fixture
async def env(db, admin, monkeypatch):
    monkeypatch.setattr(config, "get_secret", lambda k, n: None)
    u = {x.name: x.id for x in await master.list_units(db)}
    await items.create_item(db, admin, ItemInput("1001", "دریل بوش", u["عدد"],
                                                 reorder_point=Decimal(2)))
    await config.save_provider(db, admin, "Local", ProviderKind.OLLAMA, "http://localhost:11434/v1", "q")
    return u


def scripted(*responses):
    """A fake OpenAI-compatible server replaying assistant messages in order."""
    queue = list(responses)
    seen: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        message = queue.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", **message}}]})

    return handler, seen


def call(name, **args):
    return {"id": f"c_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def test_no_destructive_tools_exist():
    """QA round 1: the assistant may create and post documents (like a user), nothing destructive."""
    names = {t.name for t in TOOLS}
    assert names == {"search_items", "item_stock", "reorder_analysis", "recent_documents",
                     "open_loans", "suppliers", "create_document", "post_document",
                     "create_stock_draft", "send_report", "propose_tasks"}
    for forbidden in ("delete", "approve", "apply", "cancel", "merge", "restore", "role", "deactivate"):
        assert not any(forbidden in n for n in names)


async def test_tool_loop_answers_with_data(db, admin, env):
    handler, seen = scripted(
        {"content": None, "tool_calls": [call("search_items", query="دریل")]},
        {"content": "۰ عدد دریل بوش موجود است."},
    )
    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    assistant = Assistant(db, gw, admin)
    reply = await assistant.send("از دریل چقدر داریم؟")
    assert reply.text == "۰ عدد دریل بوش موجود است." and reply.provider == "Local"
    tool_msg = seen[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "دریل بوش" in tool_msg["content"]
    assert seen[0]["messages"][0]["role"] == "system"
    assert assistant.history[-1]["role"] == "assistant"


async def test_assistant_creates_draft_but_cannot_apply(db, admin, env):
    handler, _ = scripted(
        {"content": None, "tool_calls": [call(
            "create_stock_draft", doc_type="RECEIPT",
            lines=[{"name": "دریل بوش", "qty": 5}, {"name": "اره عمود بر", "qty": 1}])]},
        {"content": "پیش‌نویس ساخته شد."},
    )
    gw = Gateway(db, httpx.MockTransport(handler), online_check=lambda: True)
    reply = await Assistant(db, gw, admin).send("۵ دریل بوش و یک اره عمود بر رسید بزن")
    [batch_id] = reply.created_batches
    detail = await imports.get_batch(db, admin, batch_id)
    assert [ln.status for ln in detail.lines] == [LineStatus.EXISTING_MATCH, LineStatus.NEW]
    ctx = ToolContext(db, admin.as_ai(), [])
    # Even a hand-crafted tool call can't reach apply/post: no such tool exists.
    out = json.loads(await run_tool(ctx, "apply_batch", {"batch_id": batch_id}))
    assert "در دسترس نیست" in out["error"]
    with pytest.raises(Exception):  # noqa: B017 - service refuses AI actors
        await imports.apply_batch(db, admin.as_ai(), batch_id)


async def test_tools_respect_user_permissions(db, admin, env):
    from caspian.services import auth, users

    await users.create_user(db, admin, "shomar", "", "Count#2026", "counter")
    counter = (await auth.login(db, "shomar", "Count#2026")).actor
    names = {t.name for t in available_tools(counter.as_ai())}
    assert "item_stock" not in names and "reorder_analysis" not in names
    ctx = ToolContext(db, counter.as_ai(), [])
    out = json.loads(await run_tool(ctx, "item_stock", {"code": "1001"}))
    assert "error" in out
    found = json.loads(await run_tool(ctx, "search_items", {"query": "دریل"}))
    assert found[0]["on_hand"] is None  # blind counters stay blind through the AI too


async def test_bad_tool_arguments_do_not_crash(db, admin, env):
    ctx = ToolContext(db, admin.as_ai(), [])
    out = json.loads(await run_tool(ctx, "recent_documents", "{not json"))
    assert "error" in out


async def test_text_to_draft_offline_fallback(db, admin, env):
    wh = (await master.list_warehouses(db))[0].id
    gw = Gateway(db, httpx.MockTransport(lambda r: httpx.Response(500)), online_check=lambda: False)
    await config.delete_provider(db, admin, (await config.list_providers(db))[0].id)
    batch_id, used_ai = await text_to_draft(db, gw, admin, "۵ عدد دریل بوش\nمیز ۲ تا",
                                            DocType.RECEIPT, wh)
    assert not used_ai
    lines = (await imports.get_batch(db, admin, batch_id)).lines
    assert [(ln.name, ln.qty, ln.status) for ln in lines] == [
        ("دریل بوش", Decimal(5), LineStatus.EXISTING_MATCH), ("میز", Decimal(2), LineStatus.NEW)]


async def test_unavailable_is_reported(db, admin, env):
    await config.delete_provider(db, admin, (await config.list_providers(db))[0].id)
    gw = Gateway(db, httpx.MockTransport(lambda r: httpx.Response(200)), online_check=lambda: True)
    with pytest.raises(AIUnavailable):
        await Assistant(db, gw, admin).send("سلام")
